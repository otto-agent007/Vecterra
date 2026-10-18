"""Vecterra backend API (FastAPI).

Thin HTTP surface over the agent pipeline so the mobile app (or a Supabase
database webhook on `property_scans` INSERT) can trigger dossier generation.

Run:  uvicorn api.main:app --reload --port 8000   (from backend/)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from agents.pipeline import load_inputs_for_apn, load_inputs_for_scan, persist_dossier, run_pipeline

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
log = logging.getLogger("vecterra.api")

app = FastAPI(title="Vecterra API", version="0.1.0")

API_TOKEN = os.getenv("VECTERRA_API_TOKEN")  # shared secret for MVP; replace with Supabase JWT verification


def _auth(authorization: Optional[str]) -> None:
    if not API_TOKEN:
        return  # auth disabled in local dev
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="unauthorized")


class RunRequest(BaseModel):
    scan_id: Optional[str] = Field(None, description="property_scans.id to process")
    apn: Optional[str] = Field(None, description="Alternatively, latest uploaded scan for this APN")
    persist: bool = True


class RunResponse(BaseModel):
    compliance_passed: bool
    dossier_id: Optional[str] = None
    version: Optional[int] = None
    status: Optional[str] = None
    health_scores: Dict[str, Any]
    zoning_analysis: Dict[str, Any]
    compliance_log: List[Dict[str, Any]]
    report_markdown: str
    errors: List[str]


class SupabaseWebhook(BaseModel):
    """Payload shape for a Supabase Database Webhook (INSERT on property_scans)."""
    type: str
    table: str
    record: Dict[str, Any]


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/pipeline/run", response_model=RunResponse)
def run(req: RunRequest, authorization: Optional[str] = Header(None)) -> RunResponse:
    _auth(authorization)
    if not req.scan_id and not req.apn:
        raise HTTPException(status_code=422, detail="scan_id or apn required")
    try:
        inputs = load_inputs_for_scan(req.scan_id) if req.scan_id else load_inputs_for_apn(req.apn or "")
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"could not load inputs: {exc}") from exc

    state = run_pipeline(inputs["parcel"], inputs["scan"], inputs["image_urls"])
    row = persist_dossier(state) if req.persist else None
    scores = state.get("vision_scores", {})
    return RunResponse(
        compliance_passed=bool(state.get("compliance_passed")),
        dossier_id=row["id"] if row else None,
        version=row["version"] if row else None,
        status=row["status"] if row else None,
        health_scores={k: scores.get(k) for k in ("roof", "paint", "structure", "overall")},
        zoning_analysis=state.get("zoning_analysis", {}),
        compliance_log=state.get("compliance_log", []),
        report_markdown=state.get("final_markdown", ""),
        errors=state.get("errors", []),
    )


@app.post("/webhooks/supabase/scan-uploaded", status_code=202)
def scan_uploaded(payload: SupabaseWebhook, tasks: BackgroundTasks, authorization: Optional[str] = Header(None)) -> Dict[str, str]:
    """Kick off dossier generation when a scan row flips to `uploaded`."""
    _auth(authorization)
    if payload.table != "property_scans" or payload.record.get("status") != "uploaded":
        return {"status": "ignored"}

    def _job(scan_id: str) -> None:
        try:
            inputs = load_inputs_for_scan(scan_id)
            persist_dossier(run_pipeline(inputs["parcel"], inputs["scan"], inputs["image_urls"]))
        except Exception:  # pragma: no cover
            log.exception("background dossier job failed for scan %s", scan_id)

    tasks.add_task(_job, payload.record["id"])
    return {"status": "queued"}
