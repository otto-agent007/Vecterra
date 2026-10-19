"""Layered rules: one local authority per parcel, state law wins conflicts, overlays stack,
dated rules switch on by `as_of`, and unsettled law is reported as unsettled."""

from datetime import date

from agents.compliance import find_violations, normalize, screen_text
from agents.dossier_generator import render_markdown
from agents.report_html import render_report_html, visible_text
from agents.rule_layers import (
    AB956_EFFECTIVE,
    LOCAL_AUTHORITIES,
    homeowner_groups,
    resolve_numbers,
)
from agents.zoning_rules import evaluate_zoning

BEFORE = date(2026, 10, 19)
AFTER = date(2027, 1, 2)

COUNTY = {
    "apn": "999-001-01-00", "jurisdiction": "san_diego_county", "zoning_code": "RR",
    "lot_area_sqft": 130680, "living_area_sqft": 2140, "year_built": 1978,
    "fire_hazard_zone": "Very High", "fire_responsibility": "SRA",
    "fire_agency": "Rancho Santa Fe Fire Protection District", "sewer_service": "septic", "hoa": False,
}
CITY = {
    "apn": "999-003-11-00", "jurisdiction": "city_of_escondido", "zoning_code": "R-1-20",
    "lot_area_sqft": 22000, "living_area_sqft": 2380, "year_built": 1989,
    "fire_hazard_zone": "High", "fire_responsibility": "LRA", "fire_agency": "Escondido Fire Department",
    "sewer_service": "sewer", "hoa": "Example Glen Association",
}


def _rules(z):
    return {r["key"]: r for r in z["rule_layers"]["rules"]}


def test_each_parcel_has_exactly_one_local_authority():
    c = evaluate_zoning(COUNTY, as_of=BEFORE)["rule_layers"]
    e = evaluate_zoning(CITY, as_of=BEFORE)["rule_layers"]
    assert c["land_use_authority"] == "County of San Diego"
    assert e["land_use_authority"] == "City of Escondido"
    for layers, other in ((c, "City of Escondido"), (e, "County of San Diego")):
        assert all(r["authority"] != other for r in layers["rules"])


def test_ab956_switches_on_jan_1_2027_and_overrides_unupdated_local_rules():
    before = evaluate_zoning(COUNTY, as_of=BEFORE)
    after = evaluate_zoning(COUNTY, as_of=AFTER)
    assert before["adu"]["max_detached_units"] == 1
    assert _rules(before)["two_detached"]["status"] == "upcoming"
    assert after["adu"]["max_detached_units"] == 2
    assert _rules(after)["two_detached"]["status"] == "verify"  # final text not yet reviewed
    conflicts = {c["key"]: c for c in after["rule_layers"]["conflicts"]}
    assert conflicts["detached_units"]["status"] == "resolved"
    assert "State law applies" in conflicts["detached_units"]["resolution"]
    assert "local_update_pending" in _rules(after)
    # Exactly on the effective date the new rule is in force.
    assert evaluate_zoning(COUNTY, as_of=AB956_EFFECTIVE)["adu"]["max_detached_units"] == 2


def test_upcoming_rules_only_show_within_the_window():
    far = evaluate_zoning(COUNTY, as_of=date(2025, 1, 1))
    assert "two_detached" not in _rules(far)


def test_state_floor_supersedes_a_local_cap_that_is_too_low():
    bad_local = {**LOCAL_AUTHORITIES["san_diego_county"], "name": "Hypothetical Town", "detached_cap_sqft": 600}
    numbers, conflicts = resolve_numbers(bad_local, BEFORE)
    assert numbers["detached_cap_sqft"] == 1000
    assert conflicts[0].key == "detached_cap"
    assert conflicts[0].status == "resolved" and "State minimum applies" in conflicts[0].resolution


def test_disagreeing_sources_are_flagged_until_confirmed():
    unsure = {**LOCAL_AUTHORITIES["city_of_escondido"], "detached_cap_status": "verify",
              "detached_cap_note": "City says 1,000 sq ft; a builder says 1,200"}
    numbers, conflicts = resolve_numbers(unsure, BEFORE)
    assert numbers["detached_cap_sqft"] == 1000 and numbers["detached_cap_status"] == "verify"
    assert conflicts[0].key == "detached_cap_sources" and conflicts[0].status == "sources_disagree"


def test_verified_local_caps_come_from_the_published_codes():
    city = _rules(evaluate_zoning(CITY, as_of=BEFORE))["detached_cap"]  # 22,000 sq ft lot
    assert city["status"] == "settled" and "§ 33-1474" in city["source"]
    assert city["statement"] == "Caps a detached ADU at 1,000 sq ft."
    small = _rules(evaluate_zoning({**CITY, "lot_area_sqft": 7500}, as_of=BEFORE))["detached_cap"]
    assert "850 sq ft for one bedroom or less" in small["statement"]
    county = _rules(evaluate_zoning(COUNTY, as_of=BEFORE))["detached_cap"]
    assert county["status"] == "settled" and "PDS-611" in county["source"]
    assert county["statement"] == "Caps a detached ADU at 1,200 sq ft and allows it up to 24 ft tall."
    assert not evaluate_zoning(CITY, as_of=BEFORE)["rule_layers"]["conflicts"]


def test_overlays_stack_and_state_voids_hoa_bans():
    z = evaluate_zoning(CITY, as_of=BEFORE)
    r = _rules(z)
    assert r["fire_construction"]["layer"] == "fire"
    assert r["fire_access"]["authority"] == "Escondido Fire Department"
    assert r["sewer"]["layer"] == "septic"
    assert r["hoa_review"]["authority"] == "Example Glen Association"
    assert "can't ban" in r["hoa_review"]["statement"]
    assert "Civil Code § 4751" in r["hoa_review"]["source"]
    assert r["sprinklers"]["layer"] == "state"  # state voids an extra local requirement
    assert z["rule_layers"]["authorities"][:2] == ["State law", "City of Escondido"]


def test_county_specific_rules_and_unsettled_items():
    r = _rules(evaluate_zoning(COUNTY, as_of=BEFORE))
    assert r["septic"]["authority"] == "County Environmental Health"
    assert r["adu_condo_sale"]["status"] == "unsettled"
    assert r["sb9_fire"]["status"] == "unsettled"
    assert r["zone0"]["status"] == "unsettled"
    assert "withdrawn" in r["zone0"]["statement"]
    assert "adu_short_term_rental" not in r  # Escondido rule must not leak into County parcels


def test_city_specific_rules():
    r = _rules(evaluate_zoning(CITY, as_of=BEFORE))
    assert "under 30 days" in r["adu_short_term_rental"]["statement"]
    assert "484, 644, 851 and 1,000" in r["preapproved_plans"]["statement"]
    assert "sb9_fire" not in r
    assert "zone0" not in r  # High zone, LRA


def test_missing_facts_are_reported_not_guessed():
    bare = {"jurisdiction": "unknown", "zoning_code": None, "lot_area_sqft": 50000}
    z = evaluate_zoning(bare, as_of=BEFORE)
    r = _rules(z)
    assert z["rule_layers"]["land_use_authority"] == "Local authority not yet resolved"
    assert r["local_authority"]["status"] == "not_checked"
    assert r["fire_zone"]["status"] == "not_checked"
    assert r["septic"]["status"] == "not_checked" and "likely on septic" in r["septic"]["statement"]
    assert r["hoa_review"]["status"] == "not_checked"
    assert r["adu_guaranteed"]["layer"] == "state"


def test_non_residential_zoning_skips_adu_rules():
    z = evaluate_zoning({**COUNTY, "zoning_code": "M54"}, as_of=BEFORE)
    keys = set(_rules(z))
    assert z["feasibility"] == "unlikely"
    assert not keys & {"adu_guaranteed", "jadu", "detached_cap", "septic", "hoa_review"}


def test_groups_follow_precedence_order():
    groups = homeowner_groups(evaluate_zoning(COUNTY, as_of=BEFORE)["rule_layers"]["rules"])
    titles = [g["title"] for g in groups]
    assert titles[0] == "State law"
    assert titles[1] == "County of San Diego"
    assert titles.index("Also applies") < titles.index("Coming up") < titles.index("Not settled yet")


def test_every_rule_statement_passes_the_firewall():
    for parcel in (COUNTY, CITY, {"jurisdiction": "unknown", "lot_area_sqft": 50000}):
        for when in (BEFORE, AFTER):
            for r in evaluate_zoning(parcel, as_of=when)["rule_layers"]["rules"]:
                blocks = [f for f in find_violations(normalize(r["statement"])) if f["severity"] == "block"]
                assert not blocks, (r["key"], blocks)


def test_report_and_markdown_show_who_sets_the_rules():
    z = evaluate_zoning(CITY, as_of=AFTER)
    html = render_report_html(CITY, {}, z, {})
    text = visible_text(html)
    assert "Who sets the rules" in text
    assert "State law overrides City of Escondido rules where they conflict" in text
    assert "To confirm with City of Escondido" in text
    assert "Example Glen Association:" in text
    assert not [f for f in find_violations(normalize(text)) if f["severity"] == "block"]
    md = render_markdown(CITY, {}, z, {})
    assert "**Who sets the rules**" in md
    assert "Example Glen Association: Can review" in md
    assert screen_text(md).passed


def test_hostile_overlay_names_are_neutralized():
    hostile = {**CITY, "hoa": "<script>x()</script> [click](javascript:alert(1))", "fire_agency": "<b>FD</b>"}
    z = evaluate_zoning(hostile, as_of=BEFORE)
    html = render_report_html(hostile, {}, z, {})
    assert "<script>x()" not in html and "<b>FD</b>" not in html
    md = render_markdown(hostile, {}, z, {})
    assert "[click](javascript" not in md
