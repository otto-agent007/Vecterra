"""Vecterra LangGraph dossier pipeline.

    scan photos ──► VisionScorerNode ──► ZoningAnalyzerNode ──► DraftDossierNode ──► ComplianceFirewallNode ──► END

* VisionScorerNode      — multimodal LLM scores roof / paint / structure from exterior photos.
* ZoningAnalyzerNode    — deterministic ADU & setback feasibility (zoning_rules.py).
* DraftDossierNode      — renders the Markdown dossier from structured state.
* ComplianceFirewallNode— deterministic FHA / FEHA guardrail; redacts, logs, appends disclaimers.

Run locally:
    python -m agents.pipeline --parcel-apn 999-001-01-00          # pulls parcel + latest scan from Supabase
    python -m agents.pipeline --demo                              # synthetic parcel, no network, no LLM
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

from .compliance import FIREWALL_VERSION, screen_fragments, screen_text
from .dossier_generator import GENERATOR_VERSION, build_dossier_record, build_lead_rationale, render_html, render_markdown
from .state import VecterraState, VisionScores
from .zoning_rules import RULES_VERSION, evaluate_zoning

log = logging.getLogger("vecterra.agents")

VISION_MODEL = os.getenv("VECTERRA_VISION_MODEL", "gpt-4o")  # swap for the best available vision model
SCAN_BUCKET = "property-scans"
SIGNED_URL_TTL_SECONDS = 900

# -----------------------------------------------------------------------------
# Vision prompt — structure only. The instruction to ignore people/occupant
# signals is a compliance control, not a stylistic choice.
# -----------------------------------------------------------------------------
VISION_SYSTEM_PROMPT = """You are a building-envelope condition assessor reviewing exterior photographs of a single-family property.

Assess ONLY the physical structure. Never describe, infer, or mention people, vehicles, pets, toys, flags, religious items,
signage, or anything that could suggest who lives there or their characteristics. If such things are visible, ignore them.

Score each element from 0 (failing / needs replacement) to 100 (new or like-new). Use "unknown" when the photos do not show the element.

Respond with ONLY a JSON object of this exact shape:
{
  "roof":      {"score": int|null, "confidence": float, "condition": "excellent|good|fair|poor|unknown", "observations": [string]},
  "paint":     {"score": int|null, "confidence": float, "condition": "...", "observations": [string]},
  "structure": {"score": int|null, "confidence": float, "condition": "...", "observations": [string]},
  "overall":   {"score": int|null, "confidence": float, "condition": "...", "observations": [string]}
}
Observations must be short, factual, and about materials or wear only (e.g. "curling asphalt shingles on south slope",
"peeling paint on fascia", "hairline stucco cracks near garage header")."""

VISION_USER_PROMPT = "Evaluate roof wear, paint/siding degradation, and structural upkeep from these exterior photos."


def _unknown_score() -> Dict[str, Any]:
    return {"score": None, "confidence": 0.0, "condition": "unknown", "observations": []}


def _normalize_scores(raw: Dict[str, Any]) -> Tuple[VisionScores, List[Dict[str, Any]]]:
    """Coerce the model's JSON into VisionScores.

    Every free-text observation goes through the compliance firewall here, at
    the point it enters state, so nothing unscreened can reach the dossier
    row, the API response, or the report. Blocked observations are dropped and
    the findings are returned for the compliance log.
    """
    out: Dict[str, Any] = {}
    findings: List[Dict[str, Any]] = []
    for key in ("roof", "paint", "structure", "overall"):
        item = raw.get(key) or {}
        score = item.get("score")
        try:
            score = None if score is None else max(0, min(100, int(round(float(score)))))
        except (TypeError, ValueError):
            score = None
        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        cond = str(item.get("condition", "unknown")).lower()
        if cond not in {"excellent", "good", "fair", "poor", "unknown"}:
            cond = "unknown"
        raw_obs = [str(o)[:200] for o in (item.get("observations") or [])][:6]
        obs, found = screen_fragments(raw_obs)
        findings.extend({**f, "source": f"vision.{key}"} for f in found)
        out[key] = {"score": score, "confidence": conf, "condition": cond, "observations": obs}
    return out, findings  # type: ignore[return-value]


# =============================================================================
# Nodes
# =============================================================================
def vision_scorer_node(state: VecterraState) -> Dict[str, Any]:
    """VisionScorerNode: multimodal LLM assessment of exterior condition."""
    urls: List[str] = state.get("image_urls") or []
    errors = list(state.get("errors") or [])
    versions = dict(state.get("model_versions") or {})

    if not urls:
        log.warning("No images for parcel %s; emitting unknown scores", state.get("parcel", {}).get("apn"))
        scores, _ = _normalize_scores({})
        scores["model"] = "none"
        scores["image_count"] = 0
        errors.append("vision_scorer: no images available for this parcel")
        return {"vision_scores": scores, "errors": errors, "model_versions": {**versions, "vision": "none"}, "vision_findings": []}

    try:
        from openai import OpenAI

        client = OpenAI()
        content: List[Dict[str, Any]] = [{"type": "text", "text": VISION_USER_PROMPT}]
        content += [{"type": "image_url", "image_url": {"url": u, "detail": "high"}} for u in urls[:8]]
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": VISION_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        raw = json.loads(resp.choices[0].message.content or "{}")
        scores, vision_findings = _normalize_scores(raw)
        # The raw model reply is intentionally NOT kept in state: it is the one
        # channel the firewall has not screened.
        scores["model"] = VISION_MODEL
        scores["image_count"] = len(urls[:8])
        versions["vision"] = VISION_MODEL
        if vision_findings:
            log.warning("Firewall dropped %d vision observation(s) for APN %s",
                        sum(1 for f in vision_findings if f["severity"] == "block"), state.get("parcel", {}).get("apn"))
    except Exception as exc:  # network, auth, JSON — degrade gracefully, but record it
        log.exception("Vision scoring failed")
        errors.append(f"vision_scorer: {type(exc).__name__}: {exc}")
        scores, vision_findings = _normalize_scores({})
        scores["model"] = VISION_MODEL
        scores["image_count"] = len(urls)
        versions["vision"] = f"{VISION_MODEL} (failed)"

    return {"vision_scores": scores, "errors": errors, "model_versions": versions, "vision_findings": vision_findings}


def zoning_analyzer_node(state: VecterraState) -> Dict[str, Any]:
    """ZoningAnalyzerNode: deterministic ADU / setback feasibility."""
    analysis = evaluate_zoning(state["parcel"])
    versions = {**(state.get("model_versions") or {}), "zoning": RULES_VERSION}
    return {"zoning_analysis": analysis, "model_versions": versions}


def draft_dossier_node(state: VecterraState) -> Dict[str, Any]:
    """DraftDossierNode: structured state → Markdown draft (pre-firewall)."""
    parcel = state["parcel"]
    scores = state.get("vision_scores") or {}
    zoning = state.get("zoning_analysis") or {}
    rationale = build_lead_rationale(parcel, scores, zoning)
    draft = render_markdown(parcel, scores, zoning, rationale)
    versions = {**(state.get("model_versions") or {}), "dossier": GENERATOR_VERSION}
    return {"draft_markdown": draft, "lead_rationale": rationale, "model_versions": versions}


def compliance_firewall_node(state: VecterraState) -> Dict[str, Any]:
    """ComplianceFirewallNode: deterministic FHA / FEHA guardrail on all generated text."""
    result = screen_text(state.get("draft_markdown", ""), rationale=state.get("lead_rationale") or {})
    if not result.passed:
        log.warning("Compliance firewall BLOCKED %d span(s) for APN %s",
                    len(result.blocked), state.get("parcel", {}).get("apn"))
    html = render_html(result.text, apn=state["parcel"].get("apn", ""), firewall_version=result.firewall_version)
    versions = {**(state.get("model_versions") or {}), "firewall": FIREWALL_VERSION}
    # Findings from screening vision observations at ingestion time are part of
    # the audit trail even though they never reached the Markdown.
    vision_findings = state.get("vision_findings") or []
    return {
        "final_markdown": result.text,
        "final_html": html,
        "compliance_log": vision_findings + result.as_log(),
        "compliance_passed": result.passed,
        "model_versions": versions,
    }


# =============================================================================
# Graph
# =============================================================================
def build_graph():
    g = StateGraph(VecterraState)
    g.add_node("vision_scorer", vision_scorer_node)
    g.add_node("zoning_analyzer", zoning_analyzer_node)
    g.add_node("draft_dossier", draft_dossier_node)
    g.add_node("compliance_firewall", compliance_firewall_node)

    g.set_entry_point("vision_scorer")
    g.add_edge("vision_scorer", "zoning_analyzer")
    g.add_edge("zoning_analyzer", "draft_dossier")
    g.add_edge("draft_dossier", "compliance_firewall")
    g.add_edge("compliance_firewall", END)
    return g.compile()


def run_pipeline(parcel: Dict[str, Any], scan: Optional[Dict[str, Any]] = None, image_urls: Optional[List[str]] = None) -> VecterraState:
    graph = build_graph()
    initial: VecterraState = {
        "parcel": parcel,
        "scan": scan or {},
        "image_urls": image_urls or [],
        "errors": [],
        "model_versions": {},
    }
    return graph.invoke(initial)  # type: ignore[return-value]


# =============================================================================
# Supabase glue
# =============================================================================
def _supabase():
    from supabase import create_client

    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    return create_client(url, key)


def load_inputs_for_scan(scan_id: str) -> Dict[str, Any]:
    sb = _supabase()
    scan = sb.table("property_scans").select("*").eq("id", scan_id).single().execute().data
    parcel = sb.table("parcels").select("*").eq("id", scan["parcel_id"]).single().execute().data
    urls = [
        sb.storage.from_(SCAN_BUCKET).create_signed_url(p, SIGNED_URL_TTL_SECONDS)["signedURL"]
        for p in (scan.get("image_paths") or [])
    ]
    return {"parcel": parcel, "scan": scan, "image_urls": urls}


def load_inputs_for_apn(apn: str) -> Dict[str, Any]:
    sb = _supabase()
    parcel = sb.table("parcels").select("*").eq("apn", apn).single().execute().data
    scans = (
        sb.table("property_scans").select("*").eq("parcel_id", parcel["id"])
        .in_("status", ["uploaded", "scored"]).order("captured_at", desc=True).limit(1).execute().data
    )
    scan = scans[0] if scans else None
    urls = []
    if scan:
        urls = [
            sb.storage.from_(SCAN_BUCKET).create_signed_url(p, SIGNED_URL_TTL_SECONDS)["signedURL"]
            for p in (scan.get("image_paths") or [])
        ]
    return {"parcel": parcel, "scan": scan, "image_urls": urls}


def persist_dossier(state: VecterraState) -> Dict[str, Any]:
    sb = _supabase()
    record = build_dossier_record(state)
    existing = sb.table("dossiers").select("version").eq("parcel_id", record["parcel_id"]).order("version", desc=True).limit(1).execute().data
    record["version"] = (existing[0]["version"] + 1) if existing else 1
    row = sb.table("dossiers").insert(record).execute().data[0]
    if state.get("scan"):
        sb.table("property_scans").update({"status": "scored"}).eq("id", state["scan"]["id"]).execute()
    return row


# =============================================================================
# CLI
# =============================================================================
DEMO_PARCEL: Dict[str, Any] = {
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


def main() -> None:
    p = argparse.ArgumentParser(description="Run the Vecterra dossier pipeline")
    p.add_argument("--scan-id")
    p.add_argument("--parcel-apn")
    p.add_argument("--demo", action="store_true", help="Synthetic parcel; no Supabase, no LLM call")
    p.add_argument("--persist", action="store_true", help="Write the dossier back to Supabase")
    p.add_argument("--out", help="Write final HTML to this path")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    if args.demo:
        inputs = {"parcel": DEMO_PARCEL, "scan": None, "image_urls": []}
    elif args.scan_id:
        inputs = load_inputs_for_scan(args.scan_id)
    elif args.parcel_apn:
        inputs = load_inputs_for_apn(args.parcel_apn)
    else:
        p.error("one of --demo, --scan-id, --parcel-apn is required")
        return

    state = run_pipeline(inputs["parcel"], inputs["scan"], inputs["image_urls"])

    print(state["final_markdown"])
    print("\n--- compliance:", "PASSED" if state["compliance_passed"] else "HOLD", "---")
    for f in state["compliance_log"]:
        print(f"  [{f['severity']}] {f['rule_id']}: {f['matched_text']!r}")
    if state.get("errors"):
        print("--- errors:", *state["errors"], sep="\n  ")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(state["final_html"])
        print(f"HTML written to {args.out}")
    if args.persist and not args.demo:
        row = persist_dossier(state)
        print(f"Dossier persisted: {row['id']} (v{row['version']}, {row['status']})")


if __name__ == "__main__":
    main()
