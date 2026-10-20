#!/usr/bin/env python3
"""Load the real Harmony Grove study area into Supabase, overlays joined.

Fetches the SanGIS parcel layer and the overlay layers straight from their
public ArcGIS services (see ingestion/study_area.py for the source table),
joins jurisdiction, zoning, fire zone, fire agency, sewer/septic and HOA facts,
and upserts on APN. Owner names are not in the source and are never loaded.

Run from backend/:
  python -m ingestion.load_study_area --dry-run                 # fetch + join + print counts, write nothing
  python -m ingestion.load_study_area --cache-dir .cache/study   # also keep the raw layers for re-runs
  python -m ingestion.load_study_area --cache-dir .cache/study --from-cache --dry-run
  python -m ingestion.load_study_area                            # upsert (needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY)

Apply migrations/20261020_parcel_overlays.sql (or schema.sql) first.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

if __package__ in (None, ""):  # allow `python ingestion/load_study_area.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

from ingestion.load_parcels import (  # noqa: E402
    EQUITY_MODEL_VERSION,
    _polygon_area_sqft,
    estimate_equity,
    to_ewkt,
    to_polygon,
    upsert_rows,
)
from ingestion.study_area import (  # noqa: E402
    SANGIS_ATTRIBUTION,
    STUDY_AREA_BBOX,
    build_parcels,
    fetch_all,
    join_overlays,
    summarize,
    to_frames,
)

log = logging.getLogger("vecterra.ingest.study_area")

SOURCE_TAG = "sangis_parcels_via_sandag"


def _json_safe(v: Any) -> Any:
    """Recursively turn numpy / NaN values into plain JSON."""
    if isinstance(v, dict):
        return {str(k): _json_safe(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:  # pragma: no cover
            return str(v)
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _num(v: Any) -> Optional[float]:
    v = _json_safe(v)
    return None if v is None or v == "" else float(v)


def _int(v: Any) -> Optional[int]:
    f = _num(v)
    return int(round(f)) if f is not None else None


def build_rows(joined, *, ingested_at: Optional[str] = None, as_of: Optional[date] = None) -> List[Dict[str, Any]]:
    """Joined GeoDataFrame -> rows for `parcels` (same shape load_parcels.normalize produces, plus overlays)."""
    ingested_at = ingested_at or datetime.now(timezone.utc).isoformat()
    tag = f"{SOURCE_TAG}@{(as_of or date.today()).isoformat()} ({SANGIS_ATTRIBUTION})"
    rows: List[Dict[str, Any]] = []
    skipped = 0
    for rec in joined.to_dict("records"):
        poly = to_polygon(rec.get("geometry"))
        if poly is None:
            skipped += 1
            continue
        lot = _num(rec.get("lot_area_sqft")) or _polygon_area_sqft(poly)
        land = _num(rec.get("assessed_land_value"))
        impr = _num(rec.get("assessed_improvement_value"))
        assessed_total = (land or 0) + (impr or 0) or None
        eq = estimate_equity(assessed_total, None, rec.get("last_sale_date"))
        row = {
            "apn": rec["apn"],
            "situs_address": rec.get("situs_address"),
            "situs_city": rec.get("situs_city"),
            "situs_zip": rec.get("situs_zip"),
            "jurisdiction": rec["jurisdiction"],
            "zoning_code": rec.get("zoning_code"),
            "zoning_desc": rec.get("zoning_desc"),
            "land_use_code": rec.get("land_use_code"),
            "lot_area_sqft": lot,
            "year_built": _int(rec.get("year_built")),
            "living_area_sqft": _num(rec.get("living_area_sqft")),
            "owner_occupied": rec.get("owner_occupied"),
            "assessed_land_value": land,
            "assessed_improvement_value": impr,
            "last_sale_date": rec.get("last_sale_date"),
            "last_sale_price": None,  # the public layer has no price; see research/sale-prices-and-fire-zones.md
            "tax_data": _json_safe(rec.get("tax_data") or {}),
            "estimated_market_value": eq["estimated_market_value"],
            "estimated_equity": eq["estimated_equity"],
            "equity_confidence": eq["equity_confidence"],
            "equity_model_version": EQUITY_MODEL_VERSION,
            "fire_hazard_zone": rec.get("fire_hazard_zone"),
            "fire_responsibility": rec.get("fire_responsibility"),
            "fire_agency": rec.get("fire_agency"),
            "sewer_service": rec.get("sewer_service"),
            "hoa": rec.get("hoa"),
            "hoa_name": rec.get("hoa_name"),
            "hoa_basis": rec.get("hoa_basis"),
            "overlay_facts": _json_safe(rec.get("overlay_facts") or {}),
            "overlays_as_of": rec.get("overlays_as_of"),
            "geom": to_ewkt(poly),
            "source": tag,
            "source_ingested_at": ingested_at,
        }
        rows.append(_json_safe(row))
    if skipped:
        log.warning("Skipped %d parcels with unusable geometry", skipped)
    return rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load the Harmony Grove study area with overlay facts")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                   default=list(STUDY_AREA_BBOX), help=f"default {STUDY_AREA_BBOX}")
    p.add_argument("--cache-dir", type=Path, help="Keep raw layer downloads here")
    p.add_argument("--from-cache", action="store_true", help="Read layers from --cache-dir instead of the network")
    p.add_argument("--summary-json", type=Path, help="Write the count summary to this file")
    p.add_argument("--chunk-size", type=int, default=500)
    p.add_argument("--dry-run", action="store_true", help="Fetch, join and summarize; write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if args.from_cache and not args.cache_dir:
        sys.exit("--from-cache needs --cache-dir")

    frames = to_frames(fetch_all(args.bbox, args.cache_dir, args.from_cache))
    parcels = build_parcels(frames)
    joined = join_overlays(parcels, frames)
    summary = summarize(joined)
    print(json.dumps(summary, indent=2))
    if args.summary_json:
        args.summary_json.write_text(json.dumps(summary, indent=2))

    rows = build_rows(joined)
    if args.dry_run:
        preview = {k: (v[:60] + "…" if k == "geom" else v) for k, v in rows[0].items()} if rows else {}
        print(json.dumps(preview, indent=2, default=str))
        print(f"\n[dry-run] {len(rows)} parcels ready for upsert")
        return 0

    n = upsert_rows(rows, args.chunk_size)
    log.info("Done. %d parcels upserted.", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
