"""Designed HTML dossier — the version a homeowner actually sees.

The Markdown dossier (dossier_generator.render_markdown) stays the canonical,
screened text. This module renders the same structured state into a branded,
printable document:

* Hero: the parcel itself, drawn from its PostGIS geometry as a small survey
  plat (outline, 4 ft side/rear setback line, scale bar, north arrow) on the
  logo's night background. This is the one bold element; everything else is a
  quiet light document.
* Sections in a left-rail layout: exterior condition, lot and ADU potential,
  estimated value and equity, why this property, suggested next steps, fine print.

Safety:
* Jinja autoescape is ON. Every county string and model observation is escaped;
  the only values marked safe are the SVG built here from numbers and the
  embedded logo data URI.
* The compliance disclaimers live inside a FIREWALL_SKIP region, so the
  pipeline can screen the rest of the visible text without flagging the
  Equal Housing statement's own list of protected classes.
"""

from __future__ import annotations

import base64
import html as html_lib
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, StrictUndefined, select_autoescape
from markupsafe import Markup

from .compliance import EQUAL_HOUSING_STATEMENT, ESTIMATE_DISCLAIMER, FIREWALL_VERSION
from .rule_layers import homeowner_groups

REPORT_TEMPLATE_VERSION = "dossier-html-v1"

FIREWALL_SKIP_OPEN = "<!--firewall:skip-->"
FIREWALL_SKIP_CLOSE = "<!--/firewall:skip-->"

_LOGO_PATH = Path(__file__).parent / "assets" / "report-logo.jpg"
_LOGO_DATA_URI = (
    "data:image/jpeg;base64," + base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    if _LOGO_PATH.exists() else ""
)

SQFT_PER_ACRE = 43_560
M_PER_FT = 0.3048

# Fixed status palette (dataviz reference); always paired with a text label.
_CONDITION_STYLE = {
    "excellent": ("Excellent", "#0ca30c"),
    "good": ("Good", "#0ca30c"),
    "fair": ("Fair", "#fab219"),
    "poor": ("Poor", "#d03b3b"),
    "unknown": ("Not assessed", None),
}

_FEASIBILITY_PHRASE = {
    "strong": "ADU potential looks strong",
    "good": "there is good room for an ADU",
    "possible": "an ADU may be possible",
    "constrained": "ADU options look constrained",
    "unlikely": "an ADU looks unlikely under current zoning",
    "unknown": "ADU potential has not been assessed",
}

_RATIONALE_LABELS = {
    "estimated_equity": "Estimated equity",
    "equity_confidence": "Equity model confidence",
    "estimated_market_value": "Estimated market value",
    "assessed_total_value": "Assessed value",
    "assessed_vs_market_gap": "Gap between assessed and estimated value",
    "years_since_last_sale": "Years since last sale",
    "last_sale_date": "Last sale",
    "owner_occupied": "Owner-occupied",
    "lot_area_sqft": "Lot area",
    "living_area_sqft": "Living area",
    "year_built": "Year built",
    "zoning_code": "Zoning",
    "jurisdiction": "Jurisdiction",
    "adu_feasibility": "ADU feasibility",
    "adu_feasibility_score": "ADU feasibility score",
    "roof_score": "Roof score",
    "paint_score": "Paint and siding score",
    "structure_score": "Structure score",
    "overall_health_score": "Overall condition score",
    "deferred_maintenance_flags": "Items that may need attention",
    "scan_date": "Field visit",
}


# =============================================================================
# Formatting helpers
# =============================================================================
def _money(v: Any) -> Optional[str]:
    try:
        return None if v is None else "${:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return None


def _lot_phrase(sqft: Optional[float]) -> Optional[str]:
    if not sqft:
        return None
    if sqft >= SQFT_PER_ACRE * 0.5:
        return f"{sqft / SQFT_PER_ACRE:.2f}-acre"
    return f"{sqft:,.0f} sq ft"


def _lot_caption(sqft: Optional[float]) -> Optional[str]:
    if not sqft:
        return None
    if sqft >= SQFT_PER_ACRE * 0.5:
        return f"{sqft / SQFT_PER_ACRE:.2f} acres ({sqft:,.0f} sq ft)"
    return f"{sqft:,.0f} sq ft"


def _zoning_phrase(code: Optional[str]) -> str:
    z = (code or "").upper()
    if z.startswith("RR"):
        return "rural residential"
    if z.startswith("RS"):
        return "semi-rural residential"
    if z.startswith(("A70", "A72")):
        return "agricultural"
    if z.startswith(("S88", "S92")):
        return "specific-plan residential"
    if z.startswith(("R-1", "RE")):
        return "single-dwelling residential"
    if z.startswith(("R-2", "R-3", "R-4", "R-5", "RM", "RV")):
        return "multi-dwelling residential"
    return "residential" if z else "parcel"


def _jurisdiction_phrase(zoning: Dict[str, Any]) -> str:
    j = zoning.get("jurisdiction")
    if j == "san_diego_county":
        return "unincorporated San Diego County"
    if j == "city_of_escondido":
        return "the City of Escondido"
    return "San Diego County"


def summary_sentence(parcel: Dict[str, Any], scores: Dict[str, Any], zoning: Dict[str, Any]) -> str:
    """One plain-language sentence pair built only from structured fields."""
    lot = _lot_phrase(parcel.get("lot_area_sqft") or zoning.get("lot_area_sqft"))
    kind = _zoning_phrase(zoning.get("zoning_code") or parcel.get("zoning_code"))
    where = _jurisdiction_phrase(zoning)
    noun = f"{lot} {kind}" if lot else kind
    # "an" before a spoken vowel sound: 8, 11, 18, 80-89 (eight, eleven, eighteen, eighty-)
    vowel = re.match(r"(8|11(?!\d{2})|18(?!\d{2})|a|e|i|o|u)", noun.replace(",", ""), re.I) is not None
    first = f"{'An' if vowel else 'A'} {noun} lot in {where}."
    feas = _FEASIBILITY_PHRASE.get(zoning.get("feasibility") or "unknown", _FEASIBILITY_PHRASE["unknown"])
    overall = (scores.get("overall") or {})
    if overall.get("score") is None:
        cond = "the exterior hasn't been assessed yet"
    else:
        cond = f"the exterior reads as {overall.get('condition', 'unknown')} overall"
    second = feas[0].upper() + feas[1:] + "; " + cond + "."
    return f"{first} {second}"


def _format_rationale_value(key: str, value: Any) -> str:
    if key == "lot_area_sqft":
        try:
            return _lot_caption(float(str(value).replace(",", ""))) or str(value)
        except ValueError:
            return str(value)
    if key == "adu_feasibility":
        return str(value).capitalize()
    if key == "owner_occupied":
        return "Yes" if str(value).lower() in {"yes", "true"} else "No"
    if key == "deferred_maintenance_flags":
        return ", ".join(s.strip().capitalize() for s in str(value).split(","))
    return str(value)


# =============================================================================
# Plat (parcel drawing) — geometry in, SVG out, numbers only
# =============================================================================
def _load_geometry(g: Any):
    """Accept GeoJSON dict, WKT, EWKT, or (E)WKB hex as returned by PostgREST."""
    if g is None or g == "":
        return None
    from shapely import wkb, wkt
    from shapely.geometry import MultiPolygon, Polygon, shape

    try:
        if isinstance(g, dict):
            geom = shape(g)
        else:
            s = str(g).strip()
            if s.upper().startswith("SRID="):
                s = s.split(";", 1)[1]
            if re.fullmatch(r"[0-9A-Fa-f]+", s):
                geom = wkb.loads(s, hex=True)
            else:
                geom = wkt.loads(s)
    except Exception:
        return None
    if isinstance(geom, MultiPolygon):
        geom = max(geom.geoms, key=lambda p: p.area)
    return geom if isinstance(geom, Polygon) and not geom.is_empty else None


def _nice_scale_feet(target_ft: float) -> int:
    for ft in (10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000):
        if ft >= target_ft * 0.7:
            return ft
    return 2000


def build_plat(geom_value: Any, *, width: int = 360, height: int = 280) -> Optional[Dict[str, Any]]:
    """Draw the parcel as a survey plat: outline, boundary lengths, scale bar, north arrow.

    The 4 ft side/rear setback line is drawn only when it sits visibly inside the
    outline at this scale; on a multi-acre lot it would just double the boundary.
    Returns {"svg": Markup, "setback": bool} or None when there is no geometry.
    """
    poly = _load_geometry(geom_value)
    if poly is None:
        return None
    from shapely.affinity import affine_transform
    from shapely.geometry.polygon import orient

    lon0, lat0 = poly.centroid.x, poly.centroid.y
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 110_540.0
    # lon/lat -> local metres around the centroid; counter-clockwise so outward normals are consistent
    m = orient(affine_transform(poly, [kx, 0, 0, ky, -lon0 * kx, -lat0 * ky]), sign=1.0)

    minx, miny, maxx, maxy = m.bounds
    w_m, h_m = max(maxx - minx, 1e-6), max(maxy - miny, 1e-6)
    pad, bottom = 40, 40  # pad leaves room for edge labels; bottom band holds the scale bar
    s = min((width - 2 * pad) / w_m, (height - pad - bottom) / h_m)
    ox = (width - w_m * s) / 2 - minx * s
    oy = pad + (height - pad - bottom - h_m * s) / 2 + maxy * s  # y is flipped

    def px(x: float, y: float) -> Tuple[float, float]:
        return ox + x * s, oy - y * s

    def path(p) -> str:
        parts = []
        for r in [p.exterior, *p.interiors]:
            pts = [f"{a:.1f},{b:.1f}" for a, b in (px(x, y) for x, y in r.coords)]
            parts.append("M" + " L".join(pts) + " Z")
        return " ".join(parts)

    lot_path = path(m)

    setback_m = 4 * M_PER_FT
    setback = m.buffer(-setback_m, join_style=2)
    show_setback = (setback_m * s >= 5) and not setback.is_empty and setback.geom_type == "Polygon"
    setback_svg = (
        f'<path d="{path(setback)}" fill="none" stroke="#C9D0FF" stroke-opacity="0.6" stroke-width="1" stroke-dasharray="4 4"/>'
        if show_setback else ""
    )

    # Boundary lengths, survey-style: along each edge, outside the lot, kept upright.
    coords = list(m.exterior.coords)
    edges = []
    for (x1, y1), (x2, y2) in zip(coords, coords[1:]):
        length_m = math.hypot(x2 - x1, y2 - y1)
        if length_m * s >= 42:
            edges.append((length_m, x1, y1, x2, y2))
    labels = []
    for length_m, x1, y1, x2, y2 in sorted(edges, reverse=True)[:10]:
        dx, dy = (x2 - x1) / length_m, (y2 - y1) / length_m
        nx, ny = dy, -dx  # outward normal for a counter-clockwise ring
        mx, my = px((x1 + x2) / 2, (y1 + y2) / 2)
        lx, ly = mx + nx * 11, my - ny * 11
        ang = math.degrees(math.atan2(-(dy), dx))
        if ang > 90:
            ang -= 180
        elif ang < -90:
            ang += 180
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" transform="rotate({ang:.1f} {lx:.1f} {ly:.1f})" '
            f'text-anchor="middle" dominant-baseline="middle">{length_m / M_PER_FT:,.0f} ft</text>'
        )

    target_ft = (width - 2 * pad) * 0.3 / s / M_PER_FT
    bar_ft = _nice_scale_feet(target_ft)
    bar_px = bar_ft * M_PER_FT * s
    bx, by = pad, height - 16

    title = ("Parcel outline drawn from county GIS data, with approximate boundary lengths"
             + (", the 4 foot side and rear setback line," if show_setback else "")
             + f" and a {bar_ft} foot scale bar.")
    svg = f"""<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img" aria-labelledby="plat-title">
<title id="plat-title">{title}</title>
<defs>
  <linearGradient id="lot-stroke" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#28A1FC"/><stop offset="1" stop-color="#5105F6"/>
  </linearGradient>
  <linearGradient id="lot-fill" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#28A1FC" stop-opacity="0.14"/><stop offset="1" stop-color="#5105F6" stop-opacity="0.10"/>
  </linearGradient>
  <filter id="glow" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="6"/></filter>
  <pattern id="survey-grid" width="24" height="24" patternUnits="userSpaceOnUse">
    <path d="M24 0H0V24" fill="none" stroke="#E8EBFF" stroke-opacity="0.07" stroke-width="1"/>
  </pattern>
</defs>
<rect width="{width}" height="{height}" fill="url(#survey-grid)"/>
<path d="{lot_path}" fill="none" stroke="url(#lot-stroke)" stroke-width="6" stroke-opacity="0.55" filter="url(#glow)"/>
<path d="{lot_path}" fill="url(#lot-fill)" stroke="url(#lot-stroke)" stroke-width="2.25" stroke-linejoin="round"/>
{setback_svg}
<g fill="#C9CEEF" fill-opacity="0.85" font-size="10" font-family="Archivo, system-ui, sans-serif" style="font-variant-numeric: tabular-nums">
{chr(10).join(labels)}
</g>
<g transform="translate({width - 22},{18})" fill="none" stroke="#E8EBFF" stroke-opacity="0.75">
  <path d="M0 18 L0 0 M-5 7 L0 0 L5 7" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  <text x="0" y="31" fill="#E8EBFF" fill-opacity="0.75" stroke="none" font-size="11" text-anchor="middle" font-family="Archivo, system-ui, sans-serif">N</text>
</g>
<path d="M{bx} {by - 4} V{by + 4} M{bx} {by} H{bx + bar_px:.1f} M{bx + bar_px:.1f} {by - 4} V{by + 4}" fill="none" stroke="#E8EBFF" stroke-opacity="0.75" stroke-width="1.5"/>
<text x="{bx + bar_px + 8:.1f}" y="{by + 4}" fill="#E8EBFF" fill-opacity="0.75" font-size="11" font-family="Archivo, system-ui, sans-serif">{bar_ft} ft</text>
</svg>"""
    return {"svg": Markup(svg), "setback": show_setback}


def plat_svg(geom_value: Any, **kw: Any) -> Optional[Markup]:
    built = build_plat(geom_value, **kw)
    return built["svg"] if built else None


# =============================================================================
# View model
# =============================================================================
def _condition_rows(scores: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
    def row(key: str, label: str) -> Dict[str, Any]:
        item = scores.get(key) or {}
        cond = (item.get("condition") or "unknown").lower()
        word, color = _CONDITION_STYLE.get(cond, _CONDITION_STYLE["unknown"])
        score = item.get("score")
        conf = item.get("confidence")
        return {
            "label": label,
            "score": score,
            "word": word,
            "color": color,
            "confidence": None if conf in (None, 0, 0.0) or score is None else round(float(conf) * 100),
            "observations": list(item.get("observations") or []),
        }

    overall = row("overall", "Overall")
    parts = [row("roof", "Roof"), row("paint", "Paint and siding"), row("structure", "Structure and upkeep")]
    assessed = any(r["score"] is not None for r in [overall, *parts])
    return overall, parts, assessed


def _value_picture(parcel: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    market = parcel.get("estimated_market_value")
    equity = parcel.get("estimated_equity")
    try:
        market_f = float(market) if market is not None else None
        equity_f = float(equity) if equity is not None else None
    except (TypeError, ValueError):
        return None
    if not market_f or equity_f is None or market_f <= 0:
        return None
    equity_f = max(0.0, min(equity_f, market_f))
    balance = market_f - equity_f
    return {
        "market": _money(market_f),
        "equity": _money(equity_f),
        "balance": _money(balance),
        "equity_pct": round(100 * equity_f / market_f, 1),
        "balance_pct": round(100 * balance / market_f, 1),
        "has_balance": balance >= 1,
    }


def hold_reasons_from_state(state: Dict[str, Any], *, warnings: List[Dict[str, Any]]) -> List[str]:
    reasons: List[str] = []
    for err in state.get("errors") or []:
        if "no images" in err:
            reasons.append("no exterior photos yet")
        elif err.startswith("vision_scorer"):
            reasons.append("photo scoring failed")
        else:
            reasons.append("a pipeline step reported an error")
    if warnings:
        reasons.append("some wording needs a human check")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# =============================================================================
# Template
# =============================================================================
_env = Environment(autoescape=select_autoescape(default=True, default_for_string=True), undefined=StrictUndefined,
                   trim_blocks=True, lstrip_blocks=True)

_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ address }} property dossier</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@62..125,100..900&display=swap" rel="stylesheet">
<style>
  :root {
    --night: #000007; --hero-ink: #E8EBFF; --hero-ink-2: #9AA0C8;
    --ink: #0E1230; --ink-2: #4A5072; --ink-3: #6B7193; --rule: #DCE1F0;
    --page: #FFFFFF; --canvas: #E9ECF6;
    --accent: #2F3BE0; --accent-2: #949EFF; --track: #E4E7FF;
  }
  * { box-sizing: border-box; }
  html { background: var(--canvas); }
  body { margin: 0; color: var(--ink); background: var(--canvas);
         font-family: "Archivo", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         font-size: 15.5px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
  .page { max-width: 880px; margin: 32px auto; background: var(--page);
          box-shadow: 0 1px 2px rgba(14,18,48,.05), 0 18px 40px rgba(14,18,48,.08); }

  /* Hero: the parcel, on the logo's night canvas */
  .hero { background: var(--night); color: var(--hero-ink); padding: 30px 40px 34px;
          display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 28px 36px; align-items: start;
          -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  .hold { grid-column: 1 / -1; margin: 0; padding: 9px 14px; border-radius: 6px; font-size: 13.5px; line-height: 1.45;
          color: #FFE3A3; background: rgba(250,178,25,.10); border: 1px solid rgba(250,178,25,.40); }
  /* screen-blend drops the logo's own near-black canvas, so no JPEG box shows on the hero */
  .brand { display: block; width: 104px; height: auto; margin: -6px 0 0 -8px; mix-blend-mode: screen; }
  .kind { margin: 22px 0 0; font-size: 14px; font-stretch: 112%; color: var(--hero-ink-2); }
  h1 { margin: 6px 0 14px; font-size: clamp(30px, 4.6vw, 44px); line-height: 1.04; font-weight: 640;
       font-stretch: 125%; letter-spacing: -0.012em; }
  .summary { margin: 0; font-size: 16.5px; line-height: 1.55; max-width: 36ch; color: var(--hero-ink); }
  .meta { display: grid; grid-template-columns: max-content 1fr; gap: 3px 16px; margin: 22px 0 0; font-size: 13.5px; }
  .meta dt { color: var(--hero-ink-2); }
  .meta dd { margin: 0; }
  .plat { margin: 0; }
  .plat svg { display: block; width: 100%; height: auto; }
  .plat figcaption { margin-top: 6px; font-size: 13.5px; color: var(--hero-ink-2); }
  .plat figcaption strong { color: var(--hero-ink); font-weight: 560; }
  .plat-missing { border: 1px solid rgba(232,235,255,.18); border-radius: 8px; padding: 22px; font-size: 14px; color: var(--hero-ink-2); }

  /* Body rows: section title on the left rail, content on the right */
  .row { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 36px; padding: 30px 40px; border-top: 1px solid var(--rule); }
  .row:first-of-type { border-top: 0; }
  .row h2 { margin: 0; font-size: 17px; line-height: 1.25; font-weight: 620; font-stretch: 118%; }
  .row .lede { margin: 6px 0 0; font-size: 13.5px; line-height: 1.45; color: var(--ink-3); }
  .content > :first-child { margin-top: 0; }
  .content p { margin: 0 0 12px; max-width: 62ch; }
  .muted { color: var(--ink-2); }
  .small { font-size: 13.5px; line-height: 1.5; }

  /* Meters */
  .meter-row { display: grid; grid-template-columns: 10.5rem minmax(0, 1fr) 8.5rem; gap: 6px 16px; align-items: center; padding: 9px 0; }
  .meter-row + .meter-row { border-top: 1px solid #EEF0F8; }
  .meter-row.lead { padding-bottom: 14px; }
  .meter-row.lead .m-label { font-weight: 620; }
  .m-label { font-weight: 500; }
  .meter { position: relative; height: 8px; border-radius: 4px; background: var(--track); }
  .meter > span { position: absolute; inset: 0 auto 0 0; border-radius: 4px; }
  .m-value { font-size: 14px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
  .m-value strong { color: var(--ink); font-weight: 600; }
  .m-obs { grid-column: 2 / -1; margin: 2px 0 0; padding-left: 18px; font-size: 14px; color: var(--ink-2); }
  .m-obs li { margin: 2px 0; }
  .empty { margin: 0; padding: 16px 18px; border-radius: 8px; background: #F5F6FC; color: var(--ink-2); max-width: 62ch; }
  .empty strong { color: var(--ink); font-weight: 600; }

  /* Facts tables */
  table.facts { border-collapse: collapse; width: 100%; max-width: 560px; margin: 4px 0 14px; font-size: 15px; }
  table.facts th, table.facts td { padding: 8px 0; border-bottom: 1px solid #EEF0F8; vertical-align: top; }
  table.facts th { text-align: left; font-weight: 400; color: var(--ink-2); padding-right: 24px; }
  table.facts td { text-align: right; font-variant-numeric: tabular-nums; font-weight: 520; }
  table.facts tr:last-child th, table.facts tr:last-child td { border-bottom: 0; }

  /* Feasibility */
  .feas { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 18px; align-items: center; max-width: 560px; margin: 0 0 18px; }
  .feas .word { font-size: 22px; font-weight: 620; font-stretch: 112%; }
  .feas .score { font-size: 14px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
  .feas .meter { grid-column: 1 / -1; height: 10px; border-radius: 5px; }
  .feas .meter > span { background: var(--accent); border-radius: 5px; }
  h3 { margin: 18px 0 6px; font-size: 14.5px; font-weight: 620; }
  ul.plain { margin: 0; padding-left: 18px; max-width: 62ch; }
  ul.plain li { margin: 4px 0; }

  /* Who sets the rules: one group per layer, in precedence order */
  .rules { max-width: 62ch; margin: 2px 0 6px; }
  .rule-group + .rule-group { margin-top: 12px; }
  .rg-title { margin: 0 0 2px; font-size: 14px; font-weight: 620; font-stretch: 108%; }
  .rg-lead { font-weight: 560; }
  .tag { display: inline-block; margin-left: 2px; padding: 0 7px; border-radius: 9px; font-size: 12px; line-height: 1.6;
         color: var(--ink-2); background: #EEF0FA; white-space: nowrap; }

  /* Value picture: one bar, two shades of one hue, 2px surface gap */
  .split { display: flex; gap: 2px; height: 18px; max-width: 560px; margin: 10px 0 10px; }
  .split > span { display: block; height: 100%; }
  .split .eq { background: var(--accent); border-radius: 4px 0 0 4px; }
  .split .bal { background: var(--accent-2); border-radius: 0 4px 4px 0; }
  .split .eq.only { border-radius: 4px; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 22px; margin: 0 0 16px; padding: 0; list-style: none; font-size: 14px; color: var(--ink-2); }
  .legend li { display: flex; align-items: center; gap: 8px; }
  .legend i { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }
  .legend strong { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
  .bignum { font-size: 34px; line-height: 1.1; font-weight: 600; letter-spacing: -0.01em; margin: 0 0 2px; }

  /* Factors */
  dl.factors { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 0 28px; margin: 0; max-width: 560px; }
  dl.factors dt, dl.factors dd { padding: 8px 0; border-bottom: 1px solid #EEF0F8; margin: 0; }
  dl.factors dt { color: var(--ink-2); }
  dl.factors dd { font-weight: 520; }

  /* Fine print */
  .fine { padding: 26px 40px 34px; border-top: 1px solid var(--rule); background: #F8F9FD; font-size: 12.5px; line-height: 1.55; color: var(--ink-2); }
  .fine p { margin: 0 0 10px; max-width: 92ch; }
  .fine .eho { color: var(--ink); font-weight: 620; }
  .fine .gen { margin-top: 16px; color: var(--ink-3); }

  @media (max-width: 720px) {
    .page { margin: 0; box-shadow: none; }
    .hero { grid-template-columns: 1fr; padding: 24px 20px 26px; }
    .row { grid-template-columns: 1fr; gap: 14px; padding: 24px 20px; }
    .meter-row { grid-template-columns: minmax(0, 1fr) auto; }
    .meter-row .meter { grid-column: 1 / -1; grid-row: 2; }
    .m-obs { grid-column: 1 / -1; }
    .fine { padding: 22px 20px 28px; }
  }
  @page { size: letter; margin: 11mm 12mm; }
  @media print {
    html, body { background: #fff; }
    body { font-size: 10.5pt; }
    .page { margin: 0; max-width: none; box-shadow: none; }
    .hero { padding: 20px 24px 22px; break-inside: avoid; }
    .row { grid-template-columns: 150px minmax(0, 1fr); gap: 22px; padding: 16px 24px; }
    .fine { padding: 14px 24px 16px; }
    /* Keep small units whole; let sections flow across pages instead of leaving gaps */
    .meter-row, .feas, .split, .legend, table.facts tr, dl.factors dt, dl.factors dd, ul.plain li, .fine p, .empty, .rg-title { break-inside: avoid; }
    .rg-title { break-after: avoid; }
    .row h2, .row .lede { break-after: avoid; }
    .bignum { font-size: 26px; }
    .meter > span, .split > span, .legend i, .hero { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  }
</style>
</head>
<body>
<main class="page">

<header class="hero">
  {% if hold_reasons %}
  <p class="hold">Held for review before sending: {{ hold_reasons | join(", ") }}.</p>
  {% endif %}
  <div>
    {% if logo %}<img class="brand" src="{{ logo }}" alt="Vecterra" width="104">{% endif %}
    <p class="kind">Property dossier</p>
    <h1>{{ address }}</h1>
    <p class="summary">{{ summary }}</p>
    <dl class="meta">
      <dt>Parcel (APN)</dt><dd>{{ apn }}</dd>
      <dt>Jurisdiction</dt><dd>{{ jurisdiction }}</dd>
      <dt>Prepared</dt><dd>{{ prepared }}</dd>
    </dl>
  </div>
  {% if plat %}
  <figure class="plat">
    {{ plat.svg }}
    <figcaption>{% if lot_caption %}<strong>{{ lot_caption }}</strong> drawn from county parcel data. {% endif %}Boundary lengths are approximate{% if plat.setback %}; the dashed line marks the 4 ft side and rear setback{% endif %}.</figcaption>
  </figure>
  {% else %}
  <div class="plat-missing">The parcel outline will appear here once county geometry is loaded for this APN.</div>
  {% endif %}
</header>

<section class="row" aria-labelledby="h-condition">
  <div><h2 id="h-condition">Exterior condition</h2><p class="lede">Scored from photos taken from the street.</p></div>
  <div class="content">
  {% if condition_assessed %}
    {% for r in [overall] + parts %}
    <div class="meter-row{% if loop.first %} lead{% endif %}">
      <span class="m-label">{{ r.label }}</span>
      <div class="meter" role="img" aria-label="{{ r.label }}: {% if r.score is not none %}{{ r.score }} out of 100, {{ r.word }}{% else %}not assessed{% endif %}">
        {% if r.score is not none and r.color %}<span style="width: {{ r.score }}%; background: {{ r.color }}"></span>{% endif %}
      </div>
      <span class="m-value">{% if r.score is not none %}<strong>{{ r.word }}</strong>, {{ r.score }}/100{% else %}Not assessed{% endif %}</span>
      {% if r.observations %}<ul class="m-obs">{% for o in r.observations %}<li>{{ o }}</li>{% endfor %}</ul>{% endif %}
    </div>
    {% endfor %}
    {% if overall.confidence is not none %}<p class="small muted">Model confidence for the overall score: {{ overall.confidence }}%.</p>{% endif %}
  {% else %}
    <p class="empty"><strong>Not assessed yet.</strong> A field visit photographs the house from the street, and scores for the roof, paint and siding, and structure appear here afterwards.</p>
  {% endif %}
  </div>
</section>

<section class="row" aria-labelledby="h-lot">
  <div><h2 id="h-lot">Lot and ADU potential</h2><p class="lede">From zoning rules and the state ADU minimums.</p></div>
  <div class="content">
    <div class="feas">
      <span class="word">{{ feas_word }}</span>
      <span class="score">{{ feas_score }} of 100</span>
      <div class="meter" role="img" aria-label="ADU feasibility {{ feas_score }} out of 100"><span style="width: {{ feas_score }}%"></span></div>
    </div>
    <table class="facts">
      <tr><th scope="row">Zoning</th><td>{{ zoning_code }}</td></tr>
      <tr><th scope="row">Lot area</th><td>{{ lot_caption or "Unknown" }}</td></tr>
      <tr><th scope="row">Estimated house footprint</th><td>{{ footprint }}</td></tr>
      <tr><th scope="row">Room left after setbacks</th><td>{{ buildable }}</td></tr>
    </table>
    {% if rule_groups %}
    <h3>Who sets the rules</h3>
    <p class="small muted">State law overrides {{ land_use_authority }} rules where they conflict. Fire, septic and HOA requirements apply on top.</p>
    <div class="rules">
    {% for g in rule_groups %}
      <div class="rule-group">
        <p class="rg-title">{{ g.title }}</p>
        <ul class="plain">{% for r in g["items"] %}<li>{% if r.lead %}<span class="rg-lead">{{ r.lead }}:</span> {% endif %}{{ r.statement }}{% if r.status == "verify" %} <span class="tag">To confirm{% if r.confirm_with %} with {{ r.confirm_with }}{% endif %}</span>{% endif %}</li>{% endfor %}</ul>
      </div>
    {% endfor %}
    </div>
    {% else %}
    <p>California law requires that an ADU of at least {{ adu.by_right_min_sqft }} sq ft be allowed on residential lots, with side and rear setbacks of no more than {{ adu.side_rear_setback_ft }} ft. The local cap for a detached ADU here is about {{ "{:,}".format(adu.max_detached_sqft) }} sq ft, and a junior ADU inside the house can be up to {{ adu.jadu_max_sqft }} sq ft.</p>
    {% endif %}
    {% if caveats %}
    <h3>Worth checking</h3>
    <ul class="plain">{% for c in caveats %}<li>{{ c }}</li>{% endfor %}</ul>
    {% endif %}
  </div>
</section>

<section class="row" aria-labelledby="h-value">
  <div><h2 id="h-value">Estimated value and equity</h2><p class="lede">Modeled from public sale and tax records. Not an appraisal.</p></div>
  <div class="content">
  {% if value %}
    <p class="bignum">{{ value.market }}</p>
    <p class="small muted">Estimated market value</p>
    <div class="split" role="img" aria-label="Estimated equity {{ value.equity }} and estimated loan balance {{ value.balance }} of {{ value.market }}">
      <span class="eq{% if not value.has_balance %} only{% endif %}" style="width: {{ value.equity_pct }}%" title="Estimated equity {{ value.equity }}"></span>
      {% if value.has_balance %}<span class="bal" style="width: {{ value.balance_pct }}%" title="Estimated loan balance {{ value.balance }}"></span>{% endif %}
    </div>
    <ul class="legend">
      <li><i style="background: var(--accent)"></i>Estimated equity <strong>{{ value.equity }}</strong></li>
      {% if value.has_balance %}<li><i style="background: var(--accent-2)"></i>Estimated loan balance <strong>{{ value.balance }}</strong></li>{% endif %}
    </ul>
  {% else %}
    <p class="empty"><strong>No estimate yet.</strong> An estimate appears once a recorded sale or assessed value is available for this parcel.</p>
  {% endif %}
    <table class="facts">
      <tr><th scope="row">Assessed value{% if tax_year %} ({{ tax_year }}){% endif %}</th><td>{{ assessed or "Unknown" }}</td></tr>
      <tr><th scope="row">Last recorded sale</th><td>{{ last_sale }}</td></tr>
      <tr><th scope="row">Model confidence</th><td>{{ confidence }}</td></tr>
    </table>
    <p class="small muted">Refinances and home-equity loans don't appear in public records, so the equity figure is an upper estimate.</p>
  </div>
</section>

<section class="row" aria-labelledby="h-why">
  <div><h2 id="h-why">Why this property</h2><p class="lede">Property factors only. Nothing about who lives here is used.</p></div>
  <div class="content">
    <dl class="factors">
    {% for label, value in factors %}<dt>{{ label }}</dt><dd>{{ value }}</dd>{% endfor %}
    </dl>
  </div>
</section>

<section class="row" aria-labelledby="h-next">
  <div><h2 id="h-next">If you'd like to explore it</h2><p class="lede">No obligation, in any order.</p></div>
  <div class="content">
    <ul class="plain">
      <li>Ask for a free exterior walk-through to confirm the condition notes.</li>
      <li>Ask the {{ jurisdiction }} planning counter whether a pre-approved ADU plan fits this lot.</li>
      <li>Get an independent appraisal before relying on the equity estimate.</li>
    </ul>
  </div>
</section>

<footer class="fine">
  <p>Prepared from public county records and exterior photographs taken from the street. No MLS listing data was used.</p>
  {{ firewall_skip_open }}
  <p>{% if eho.startswith(eho_lead) %}<strong class="eho">{{ eho_lead }}</strong>{{ eho[eho_lead | length:] }}{% else %}{{ eho }}{% endif %}</p>
  <p>{{ disclaimer }}</p>
  {{ firewall_skip_close }}
  <p class="gen">Generated by Vecterra on {{ generated }} with report template {{ template_version }} and compliance rules {{ firewall_version }}.</p>
</footer>

</main>
</body>
</html>
"""


def render_report_html(
    parcel: Dict[str, Any],
    scores: Dict[str, Any],
    zoning: Dict[str, Any],
    rationale: Dict[str, Any],
    *,
    hold_reasons: Optional[List[str]] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    now = generated_at or datetime.now(timezone.utc)
    overall, parts, assessed = _condition_rows(scores or {})
    lot_sqft = parcel.get("lot_area_sqft") or zoning.get("lot_area_sqft")
    try:
        lot_sqft = float(lot_sqft) if lot_sqft is not None else None
    except (TypeError, ValueError):
        lot_sqft = None
    adu = zoning.get("adu") or {}
    feas = zoning.get("feasibility") or "unknown"

    last_sale_date = parcel.get("last_sale_date")
    last_sale_price = _money(parcel.get("last_sale_price"))
    if last_sale_date and last_sale_price:
        last_sale = f"{last_sale_price} in {str(last_sale_date)[:4]}"
    elif last_sale_date:
        last_sale = f"{str(last_sale_date)[:4]} (price not recorded)"
    else:
        last_sale = "Not on record"

    conf = parcel.get("equity_confidence")
    try:
        confidence = f"{round(float(conf) * 100)}%" if conf is not None else "Unknown"
    except (TypeError, ValueError):
        confidence = "Unknown"

    factors = [(_RATIONALE_LABELS.get(k, k.replace("_", " ").capitalize()), _format_rationale_value(k, v))
               for k, v in (rationale or {}).items()]

    ctx = dict(
        address=parcel.get("situs_address") or "Address on file",
        apn=parcel.get("apn") or "Unknown",
        jurisdiction=zoning.get("jurisdiction_name") or "Jurisdiction not resolved",
        prepared=f"{now:%B} {now.day}, {now.year}",
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        summary=summary_sentence(parcel, scores or {}, zoning),
        logo=_LOGO_DATA_URI,
        plat=build_plat(parcel.get("geom")),
        lot_caption=_lot_caption(lot_sqft),
        hold_reasons=hold_reasons or [],
        overall=overall, parts=parts, condition_assessed=assessed,
        feas_word={"unknown": "Not assessed"}.get(feas, feas.capitalize()),
        feas_score=int(zoning.get("feasibility_score") or 0),
        zoning_code=zoning.get("zoning_code") or "Unknown",
        footprint=f"{zoning['estimated_primary_footprint_sqft']:,} sq ft" if zoning.get("estimated_primary_footprint_sqft") else "Unknown",
        buildable=f"{zoning['estimated_buildable_remaining_sqft']:,} sq ft" if zoning.get("estimated_buildable_remaining_sqft") is not None else "Unknown",
        adu={"by_right_min_sqft": adu.get("by_right_min_sqft", 800), "side_rear_setback_ft": adu.get("side_rear_setback_ft", 4),
             "max_detached_sqft": adu.get("max_detached_sqft", 1200), "jadu_max_sqft": adu.get("jadu_max_sqft", 500)},
        caveats=list(zoning.get("caveats") or []),
        rule_groups=homeowner_groups((zoning.get("rule_layers") or {}).get("rules") or []),
        land_use_authority=(zoning.get("rule_layers") or {}).get("land_use_authority") or "local",
        value=_value_picture(parcel),
        assessed=_money(parcel.get("assessed_total_value")),
        tax_year=parcel.get("tax_year"),
        last_sale=last_sale,
        confidence=confidence,
        factors=factors,
        eho=EQUAL_HOUSING_STATEMENT,
        eho_lead="Equal Housing Opportunity.",
        disclaimer=ESTIMATE_DISCLAIMER,
        firewall_skip_open=Markup(FIREWALL_SKIP_OPEN),
        firewall_skip_close=Markup(FIREWALL_SKIP_CLOSE),
        template_version=REPORT_TEMPLATE_VERSION,
        firewall_version=FIREWALL_VERSION,
    )
    return _env.from_string(_TEMPLATE).render(**ctx)


# =============================================================================
# Visible text for the compliance firewall
# =============================================================================
_SKIP_RE = re.compile(re.escape(FIREWALL_SKIP_OPEN) + r".*?" + re.escape(FIREWALL_SKIP_CLOSE), re.S)
_STRIP_BLOCKS = re.compile(r"<(style|script|title|svg)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def visible_text(report_html: str) -> str:
    """What a reader sees, minus the fixed compliance disclaimers (screened by design elsewhere)."""
    s = _SKIP_RE.sub(" ", report_html)
    s = _STRIP_BLOCKS.sub(" ", s)
    s = _TAG.sub(" ", s)
    s = html_lib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()
