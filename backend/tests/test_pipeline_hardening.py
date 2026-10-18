"""Tests for the hardening pass: HTML sanitization, observation screening,
dossier status routing, API fail-closed auth, and equity-model edge cases."""

import importlib
import os
import re

import pytest

from agents.dossier_generator import build_dossier_record, dossier_status, render_html, render_markdown, sanitize_html
from agents.pipeline import _normalize_scores, run_pipeline
from agents.zoning_rules import evaluate_zoning
from ingestion.load_parcels import MIN_PLAUSIBLE_SALE_PRICE, estimate_equity

PARCEL = {
    "id": "00000000-0000-0000-0000-000000000001",
    "apn": "999-001-01-00",
    "situs_address": "100 Sample Grove Rd",
    "jurisdiction": "san_diego_county",
    "zoning_code": "RR",
    "lot_area_sqft": 83635,
    "year_built": 1978,
    "living_area_sqft": 2140,
    "owner_occupied": True,
    "assessed_total_value": 390000,
    "tax_year": 2026,
    "last_sale_date": "2004-06-15",
    "last_sale_price": 520000,
    "estimated_market_value": 1375000,
    "estimated_equity": 1130000,
    "equity_confidence": 0.55,
}


# --- HTML / Markdown injection -------------------------------------------------------
def test_report_html_strips_script_and_event_handlers():
    hostile = {**PARCEL, "situs_address": '<img src=x onerror=alert(1)> 1 Evil St'}
    scores = {"roof": {"observations": ['<script>alert(2)</script> curling shingles', '[x](javascript:alert(3))']}}
    md = render_markdown(hostile, scores, evaluate_zoning(hostile), {})
    html = render_html(md, apn=hostile["apn"], firewall_version="t")
    body = html[html.index("<body>"):]
    # No executable markup may survive: no tags other than the allow-list, no
    # event-handler attributes, no javascript: URLs. The hostile strings may
    # remain as escaped *text* (&lt;img ...&gt;), which is inert.
    assert not re.search(r"<\s*(script|img|iframe|svg|object)\b", body, re.I)
    assert not re.search(r"<[^>]*\bon\w+\s*=", body, re.I)
    assert not re.search(r"href\s*=\s*[\"']?\s*javascript:", body, re.I)
    assert "curling shingles" in body


def test_sanitize_html_allowlist():
    assert sanitize_html('<p onclick="x()">a</p><iframe src="//evil"></iframe>') == "<p>a</p>"
    assert 'rel="noopener noreferrer"' in sanitize_html('<a href="https://ok.example">x</a>')


# --- Vision observations are screened at ingestion ---------------------------------------
def test_normalize_scores_screens_observations_and_drops_raw():
    raw = {
        "roof": {"score": 40, "confidence": 0.8, "condition": "poor",
                 "observations": ["curling shingles", "toys in the yard suggest young children"]},
        "paint": {"score": 70, "confidence": 0.5, "condition": "fair", "observations": ["peeling white paint on fascia"]},
    }
    scores, findings = _normalize_scores(raw)
    assert scores["roof"]["observations"] == ["curling shingles"]
    assert scores["paint"]["observations"] == ["peeling white paint on fascia"]
    assert "raw" not in scores
    assert any(f["severity"] == "block" and f["source"] == "vision.roof" for f in findings)


def test_pipeline_demo_no_images_lands_in_hold_with_error_recorded():
    state = run_pipeline(PARCEL, None, [])
    rec = build_dossier_record(state)
    assert rec["errors"] and "no images" in rec["errors"][0]
    assert rec["status"] == "compliance_hold"
    assert "raw" not in state["vision_scores"]


# --- Status routing ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "state, expected",
    [
        ({"compliance_passed": True, "errors": [], "compliance_log": []}, "draft"),
        ({"compliance_passed": False, "errors": [], "compliance_log": []}, "compliance_hold"),
        ({"compliance_passed": True, "errors": ["vision failed"], "compliance_log": []}, "compliance_hold"),
        ({"compliance_passed": True, "errors": [], "compliance_log": [{"severity": "warn", "rule_id": "DISTRESS_TARGETING"}]}, "compliance_hold"),
        ({"compliance_passed": True, "errors": [], "compliance_log": [{"severity": "info", "rule_id": "DISCLAIMER_ADDED"}]}, "draft"),
    ],
)
def test_dossier_status(state, expected):
    assert dossier_status(state) == expected


# --- API fails closed ---------------------------------------------------------------------
def _import_api(monkeypatch):
    monkeypatch.setenv("VECTERRA_ENV", "dev")
    monkeypatch.delenv("VECTERRA_API_TOKEN", raising=False)
    import api.main as m
    return importlib.reload(m)


def test_api_refuses_to_start_without_token(monkeypatch):
    m = _import_api(monkeypatch)
    monkeypatch.setenv("VECTERRA_ENV", "production")
    with pytest.raises(RuntimeError):
        importlib.reload(m)
    _import_api(monkeypatch)


def test_api_rejects_placeholder_token(monkeypatch):
    m = _import_api(monkeypatch)
    monkeypatch.setenv("VECTERRA_API_TOKEN", "change-me")
    monkeypatch.setenv("VECTERRA_ENV", "production")
    with pytest.raises(RuntimeError):
        importlib.reload(m)
    _import_api(monkeypatch)


def test_api_auth_compares_token(monkeypatch):
    m = _import_api(monkeypatch)
    monkeypatch.setenv("VECTERRA_API_TOKEN", "s3cret")
    monkeypatch.setenv("VECTERRA_ENV", "production")
    importlib.reload(m)
    from fastapi import HTTPException
    m._auth("Bearer s3cret")
    with pytest.raises(HTTPException):
        m._auth("Bearer wrong")
    with pytest.raises(HTTPException):
        m._auth(None)
    _import_api(monkeypatch)


# --- Equity model --------------------------------------------------------------------------
def test_equity_trust_transfer_does_not_reset_clock_or_zero_equity():
    # $0 transfer last year on a long-held parcel: must fall back to the assessed floor,
    # not model "bought last year for $0".
    r = estimate_equity(assessed_total=390_000, last_sale_price=0, last_sale_date="2025-01-01")
    assert r["estimated_equity"] == 390_000 and r["equity_confidence"] == 0.2


def test_equity_transfer_tax_sized_price_is_ignored():
    r = estimate_equity(assessed_total=390_000, last_sale_price=572.0, last_sale_date="2004-06-15")  # DOC_AMT-like
    assert r["estimated_equity"] == 390_000


def test_equity_plausible_sale_uplifts_and_amortizes():
    r = estimate_equity(assessed_total=390_000, last_sale_price=520_000, last_sale_date="2004-06-15")
    assert r["estimated_market_value"] > 520_000
    assert 0 < r["estimated_equity"] < r["estimated_market_value"]
    assert r["equity_confidence"] == 0.55


def test_equity_missing_date_uses_price_without_uplift():
    r = estimate_equity(None, 600_000, None)
    assert r["estimated_market_value"] == 600_000
    assert r["estimated_equity"] == pytest.approx(600_000 * 0.2, rel=1e-6)  # only the 20% down is equity


def test_equity_nothing_known():
    assert estimate_equity(None, None, None)["estimated_equity"] is None
    assert MIN_PLAUSIBLE_SALE_PRICE > 1000


def test_ingestion_derives_lot_area_from_geometry_when_missing():
    from shapely.geometry import Polygon
    from ingestion.load_parcels import _polygon_area_sqft
    # ~0.0002 deg x ~0.00028 deg at 33.1N  ->  ~18.7 m x ~31 m  ->  ~6,230 sq ft
    sq = _polygon_area_sqft(Polygon([(-117.1120, 33.0960), (-117.1118, 33.0960), (-117.1118, 33.09628), (-117.1120, 33.09628)]))
    assert 6_000 < sq < 6_500
