"""Designed HTML report: escaping, firewall coverage, geometry handling, empty states."""

import re

import pytest

from agents.compliance import EQUAL_HOUSING_STATEMENT, find_violations, normalize
from agents.pipeline import DEMO_PARCEL, run_pipeline
from agents.report_html import (
    FIREWALL_SKIP_CLOSE, FIREWALL_SKIP_OPEN, _load_geometry, build_plat, plat_svg, render_report_html, summary_sentence, visible_text,
)
from agents.zoning_rules import evaluate_zoning

WKT = "POLYGON ((-117.118 33.094, -117.118 33.0948, -117.11862 33.09503, -117.11931 33.09479, -117.11922 33.09418, -117.11878 33.09389, -117.118 33.094))"
SCORES = {
    "roof": {"score": 42, "confidence": 0.7, "condition": "poor", "observations": ["curling shingles on south slope"]},
    "paint": {"score": 61, "confidence": 0.6, "condition": "fair", "observations": ["peeling paint on fascia"]},
    "structure": {"score": 80, "confidence": 0.5, "condition": "good", "observations": []},
    "overall": {"score": 58, "confidence": 0.6, "condition": "fair", "observations": []},
}


def _render(parcel=None, scores=None, **kw):
    parcel = parcel or DEMO_PARCEL
    return render_report_html(parcel, scores or {}, evaluate_zoning(parcel), {"estimated_equity": "$1,130,000", "adu_feasibility": "strong"}, **kw)


def _body(html):
    return html[html.index("<body>"):]


# --- Escaping ----------------------------------------------------------------------------
def test_hostile_strings_are_escaped():
    hostile = {**DEMO_PARCEL, "situs_address": '<img src=x onerror=alert(1)>1 Evil St', "apn": "<script>alert(2)</script>"}
    scores = {"roof": {"score": 40, "confidence": 0.8, "condition": "poor", "observations": ['<svg onload=alert(3)>']}}
    body = _body(_render(hostile, scores))
    assert not re.search(r"<\s*script\b", body, re.I)
    # Exactly one <img>: the embedded brand logo.
    imgs = re.findall(r"<img\b[^>]*>", body, re.I)
    assert len(imgs) == 1 and 'class="brand"' in imgs[0] and 'src="data:image/jpeg;base64,' in imgs[0]
    assert "onerror=" not in body.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    # The only <svg> in the body is the plat built from numbers.
    assert len(re.findall(r"<svg\b", body)) == 1 and "onload" not in body.replace("&lt;svg onload=alert(3)&gt;", "")


# --- Firewall coverage --------------------------------------------------------------------
def test_visible_text_passes_firewall_for_demo_and_scored_reports():
    for scores in ({}, SCORES):
        text = visible_text(_render(scores=scores))
        blocks = [f for f in find_violations(normalize(text)) if f["severity"] == "block"]
        assert not blocks, blocks


def test_disclaimer_block_is_exactly_the_fixed_constants():
    html = _render()
    region = html[html.index(FIREWALL_SKIP_OPEN) + len(FIREWALL_SKIP_OPEN):html.index(FIREWALL_SKIP_CLOSE)]
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", region)).strip()
    assert text.startswith(EQUAL_HOUSING_STATEMENT)
    assert "Equal Housing Opportunity" not in visible_text(html)  # excluded from screening, present on the page
    assert "Equal Housing Opportunity" in html


def test_pipeline_uses_designed_report_when_clean():
    state = run_pipeline(DEMO_PARCEL, None, [])
    assert state["model_versions"]["report"] == "dossier-html-v1"
    assert "Held for review before sending: no exterior photos yet." in state["final_html"]
    assert 'class="plat"' in state["final_html"]


def test_pipeline_falls_back_when_designed_report_trips_a_block():
    # A county situs string that the firewall blocks must never ship in the designed report.
    parcel = {**DEMO_PARCEL, "situs_address": "Great for young families Rd"}
    state = run_pipeline(parcel, None, [])
    assert state["compliance_passed"] is False
    assert state["model_versions"]["report"] == "markdown-fallback"
    assert "young families" not in state["final_html"]


# --- Geometry --------------------------------------------------------------------------------
@pytest.mark.parametrize("value", [
    WKT,
    "SRID=4326;" + WKT,
    {"type": "Polygon", "coordinates": [[[-117.118, 33.094], [-117.118, 33.0948], [-117.1186, 33.095], [-117.118, 33.094]]]},
    {"type": "MultiPolygon", "coordinates": [[[[-117.118, 33.094], [-117.118, 33.0948], [-117.1186, 33.095], [-117.118, 33.094]]]]},
])
def test_geometry_formats_parse(value):
    assert _load_geometry(value) is not None


def test_hex_ewkb_from_postgrest_parses():
    from shapely import wkb, wkt
    hexed = wkb.dumps(wkt.loads(WKT), hex=True, srid=4326)
    assert _load_geometry(hexed) is not None


SMALL_LOT = "POLYGON ((-117.112 33.096, -117.1115 33.096, -117.1115 33.0964, -117.112 33.0964, -117.112 33.096))"


def test_plat_has_scale_bar_and_boundary_lengths():
    svg = str(plat_svg(WKT))
    assert re.search(r">\d+ ft</text>", svg)                          # scale bar
    assert len(re.findall(r'rotate\([^)]*\)" text-anchor="middle"', svg)) >= 4  # edge lengths


def test_setback_line_only_when_visible_at_scale():
    big, small = build_plat(WKT), build_plat(SMALL_LOT)
    assert big["setback"] is False and "stroke-dasharray" not in str(big["svg"])     # 3 acres: ~1px, omitted
    assert small["setback"] is True and "stroke-dasharray" in str(small["svg"])      # 0.14 acre: visible


def test_no_geometry_means_no_plat():
    assert plat_svg(None) is None and plat_svg("garbage") is None
    html = _render({**DEMO_PARCEL, "geom": None})
    assert "parcel outline will appear here" in html


# --- Content states ------------------------------------------------------------------------------
def test_condition_empty_state_and_scored_state():
    empty = visible_text(_render(scores={}))
    assert "Not assessed yet." in empty
    scored = _render(scores=SCORES)
    assert "Not assessed yet." not in visible_text(scored)
    assert "curling shingles on south slope" in scored
    # Status color always ships with its word.
    assert re.search(r"<strong>Poor</strong>, 42/100", scored)


def test_summary_sentence_articles():
    z = {"jurisdiction": "san_diego_county", "zoning_code": "RR", "feasibility": "strong"}
    assert summary_sentence({"lot_area_sqft": 8.1 * 43560}, {}, z).startswith("An 8.10-acre")
    assert summary_sentence({"lot_area_sqft": 2 * 43560}, {}, z).startswith("A 2.00-acre")
