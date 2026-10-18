"""Shared LangGraph state for the Vecterra dossier pipeline."""

from __future__ import annotations

from typing import Any, Dict, List, TypedDict


class HealthScore(TypedDict, total=False):
    score: int             # 0–100; 100 = excellent condition
    confidence: float      # 0–1
    condition: str         # excellent | good | fair | poor | unknown
    observations: List[str]


class VisionScores(TypedDict, total=False):
    roof: HealthScore
    paint: HealthScore
    structure: HealthScore
    overall: HealthScore
    model: str
    image_count: int
    raw: Dict[str, Any]


class ComplianceFinding(TypedDict, total=False):
    rule_id: str
    severity: str          # "block" | "warn"
    matched_text: str
    start: int
    end: int
    explanation: str


class VecterraState(TypedDict, total=False):
    # Inputs
    parcel: Dict[str, Any]          # row from `parcels`
    scan: Dict[str, Any]            # row from `property_scans`
    image_urls: List[str]           # signed URLs for the scan's photos

    # Agent outputs
    vision_scores: VisionScores
    zoning_analysis: Dict[str, Any]
    lead_rationale: Dict[str, Any]  # property-only factors; validated by the firewall

    # Draft → firewall → final
    draft_markdown: str
    compliance_log: List[ComplianceFinding]
    compliance_passed: bool
    final_markdown: str
    final_html: str

    # Bookkeeping
    model_versions: Dict[str, str]
    errors: List[str]
