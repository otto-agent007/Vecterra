"""Rendered-report structure. Python-Markdown needs blank lines around tables and
lists and 4-space indents for nested lists; a missing blank line silently folds
a paragraph into the table or list above it."""

import re

from agents.dossier_generator import render_html, render_markdown
from agents.zoning_rules import evaluate_zoning

PARCEL = {
    "apn": "999-001-01-00", "situs_address": "100 Sample Grove Rd", "jurisdiction": "san_diego_county",
    "zoning_code": "RR", "lot_area_sqft": 83635, "year_built": 1978, "living_area_sqft": 2140,
    "assessed_total_value": 390000, "last_sale_date": "2004-06-15", "last_sale_price": 520000,
    "estimated_market_value": 1390000, "estimated_equity": 1200000, "equity_confidence": 0.55,
}


def _html(scores):
    z = evaluate_zoning(PARCEL)
    return render_html(render_markdown(PARCEL, scores, z, {"adu_feasibility": "strong"}), apn="x", firewall_version="t")


def _section(html, start, end):
    return html[html.index(start):html.index(end)]


def test_no_photo_note_is_outside_the_table():
    html = _html({})
    sec = _section(html, "1. Exterior Condition Snapshot", "2. Lot")
    assert "No exterior photographs" in sec
    table = sec[sec.index("<table>"):sec.index("</table>")]
    assert "No exterior photographs" not in table


def test_observations_render_as_a_list_after_the_table():
    html = _html({"roof": {"score": 40, "confidence": 0.8, "condition": "poor", "observations": ["curling shingles"]}})
    sec = _section(html, "1. Exterior Condition Snapshot", "2. Lot")
    after_table = sec[sec.index("</table>"):]
    assert "<strong>Observations</strong>" in after_table
    assert re.search(r"<li>\s*curling shingles\s*</li>", after_table)


def test_adu_subpoints_are_nested_and_caveats_is_its_own_block():
    html = _html({})
    sec = _section(html, "2. Lot", "3. Public-Record")
    # The two ADU sub-points live in a nested list under the feasibility item.
    feas = sec[sec.index("ADU feasibility"):]
    assert re.search(r"<ul>\s*<li>State floor allows", feas), "ADU sub-points are not nested"
    # "Caveats" is a paragraph, not glued into a list item.
    assert re.search(r"<p><strong>Caveats</strong></p>", sec), "Caveats heading folded into a list item"
    assert not re.search(r"<li>[^<]*Local detached cap[^<]*<strong>Caveats", sec)
