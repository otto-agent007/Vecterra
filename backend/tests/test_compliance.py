"""Firewall regression tests — run with `pytest` from backend/."""

from agents.compliance import ALLOWED_RATIONALE_KEYS, find_violations, screen_text, validate_lead_rationale
from agents.dossier_generator import build_lead_rationale, render_markdown
from agents.zoning_rules import evaluate_zoning


def rule_ids(text):
    return {f["rule_id"] for f in find_violations(text)}


def test_paint_colors_are_not_race():
    clean = "White trim is peeling; the black asphalt roof shows granule loss."
    assert rule_ids(clean) == set()


def test_race_tied_to_people_is_blocked():
    assert "FHA_RACE_COLOR" in rule_ids("This is a mostly white neighborhood.")


def test_familial_status_blocked():
    assert "FHA_FAMILIAL_STATUS" in rule_ids("A perfect home for a growing family.")


def test_steering_blocked():
    assert "STEERING_NEIGHBORHOOD" in rule_ids("Located in a safe neighborhood with great schools.")


def test_age_rule_ignores_roof_age():
    assert "FEHA_AGE" not in rule_ids("The roof is over 20 years old and near end of life.")
    assert "FEHA_AGE" in rule_ids("Ideal for seniors looking to downsize.")


def test_redaction_and_disclaimers():
    res = screen_text("Great for young families. Roof needs work.")
    assert not res.passed
    assert "[REDACTED:FHA_FAMILIAL_STATUS]" in res.text
    assert "Equal Housing Opportunity" in res.text
    assert "not appraisals" in res.text
    assert set(res.disclaimers_added) == {"equal_housing", "estimate_disclaimer"}


def test_rationale_whitelist():
    assert validate_lead_rationale({"estimated_equity": 1}) == []
    bad = validate_lead_rationale({"owner_age": 70})
    assert bad and bad[0]["rule_id"] == "RATIONALE_NON_PROPERTY_FACTOR"


def test_full_template_passes_firewall():
    parcel = {
        "apn": "999-001-01-00", "situs_address": "100 Sample Grove Rd", "jurisdiction": "san_diego_county",
        "zoning_code": "RR", "lot_area_sqft": 83635, "year_built": 1978, "living_area_sqft": 2140,
        "owner_occupied": True, "assessed_total_value": 390000, "tax_year": 2026,
        "last_sale_date": "2004-06-15", "last_sale_price": 520000, "estimated_market_value": 1375000,
        "estimated_equity": 1130000, "equity_confidence": 0.55,
    }
    scores = {
        "roof": {"score": 42, "confidence": 0.7, "condition": "poor", "observations": ["curling shingles on south slope"]},
        "paint": {"score": 55, "confidence": 0.6, "condition": "fair", "observations": ["peeling white paint on fascia"]},
        "structure": {"score": 80, "confidence": 0.5, "condition": "good", "observations": []},
        "overall": {"score": 58, "confidence": 0.6, "condition": "fair", "observations": []},
    }
    zoning = evaluate_zoning(parcel)
    rationale = build_lead_rationale(parcel, scores, zoning)
    assert set(rationale) <= ALLOWED_RATIONALE_KEYS
    draft = render_markdown(parcel, scores, zoning, rationale)
    res = screen_text(draft, rationale=rationale)
    assert res.passed, [f for f in res.findings if f["severity"] == "block"]
    assert zoning["feasibility"] in {"strong", "good"}
