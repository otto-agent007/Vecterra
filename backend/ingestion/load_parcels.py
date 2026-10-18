#!/usr/bin/env python3
"""Vecterra GIS parcel ingestion.

Reads a parcel layer (GeoJSON / Shapefile / GeoPackage), filters it to the
Harmony Grove study area by bounding box or ZIP, normalizes columns to the
`parcels` schema, computes a first-pass equity estimate, and bulk-upserts into
Supabase PostGIS.

Examples
--------
  # SanGIS county parcel shapefile, filtered to ZIP 92029
  python load_parcels.py --input data/PARCELS.shp --zip 92029

  # Any GeoJSON, filtered to an explicit bbox (min_lon min_lat max_lon max_lat)
  python load_parcels.py --input sample_parcels.geojson \
      --bbox -117.145 33.070 -117.090 33.120

  # Dry run: print normalized rows, touch nothing
  python load_parcels.py --input sample_parcels.geojson --zip 92029 --dry-run

Environment (backend/.env)
--------------------------
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY   # server-side only; never ship to the mobile app

Data sources (all public, non-MLS)
----------------------------------
  * SanGIS "PARCELS" layer (San Diego County assessor parcels)
  * San Diego County Assessor roll extracts for values / sale history
  * County & City zoning layers for zoning_code / jurisdiction refinement
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import geopandas as gpd
import pandas as pd
from dotenv import load_dotenv
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

log = logging.getLogger("vecterra.ingest")

# --- Study area --------------------------------------------------------------
# Harmony Grove / Harmony Grove Village, SW of Escondido (unincorporated SD County).
HARMONY_GROVE_BBOX = (-117.145, 33.070, -117.090, 33.120)  # (min_lon, min_lat, max_lon, max_lat)
HARMONY_GROVE_ZIPS = {"92029"}

# --- Column mapping ----------------------------------------------------------
# Candidate source column names -> canonical `parcels` column. First match wins.
# Override at runtime with --columns '{"apn": "MY_APN_COL"}'.
COLUMN_CANDIDATES: Dict[str, List[str]] = {
    "apn": ["APN", "apn", "PARCEL_ID", "ParcelNumber", "PIN"],
    "situs_address": ["SITUS_ADDRESS", "SITUS_ADDR", "SITUS_FULL", "ADDRESS", "situs_address"],
    "situs_city": ["SITUS_COMMUNITY", "SITUS_COMM", "SITUS_CITY", "CITY", "situs_city"],
    "situs_zip": ["SITUS_ZIP", "ZIP", "ZIPCODE", "situs_zip"],
    "zoning_code": ["ZONING", "ZONE", "ZONE_CODE", "zoning_code"],
    "land_use_code": ["LANDUSE", "LAND_USE", "ASR_LANDUSE", "land_use_code"],
    "lot_area_sqft": ["LOT_SQFT", "LOT_AREA", "lot_area_sqft"],
    "acreage": ["ACREAGE", "ACRES", "acreage"],
    "year_built": ["YEAR_BUILT", "YR_BUILT", "YEAR_EFFECTIVE", "year_built"],
    "living_area_sqft": ["TOTAL_LVG_AREA", "LIVING_AREA", "LVG_AREA", "SQFT", "living_area_sqft"],
    "assessed_land_value": ["ASR_LAND", "LAND_VALUE", "assessed_land_value"],
    "assessed_improvement_value": ["ASR_IMPR", "IMPR_VALUE", "assessed_improvement_value"],
    "tax_year": ["TAX_YEAR", "ROLL_YEAR", "tax_year"],
    "owner_occupied": ["OWNEROCC", "OWNER_OCC", "owner_occupied"],
    "last_sale_date": ["SALE_DATE", "DOC_DATE", "last_sale_date"],
    "last_sale_price": ["SALE_PRICE", "DOC_AMT", "last_sale_price"],
    "owner_name": ["OWN_NAME1", "OWNER_NAME", "owner_name"],
    "owner_mailing_address": ["OWN_ADDR1", "MAIL_ADDR", "owner_mailing_address"],
}

RAW_TAX_COLUMNS = [
    "ASR_LAND", "ASR_IMPR", "ASR_TOTAL", "TAXSTAT", "TAX_RATE_AREA", "TRA",
    "EXEMPT_AMT", "NUCLEUS_USE_CD", "ASR_LANDUSE", "LEGLDESC",
]

EQUITY_MODEL_VERSION = "naive-prop13-v0.1"


# =============================================================================
# Helpers
# =============================================================================
def _clean(v: Any) -> Any:
    """Convert pandas/numpy scalars to JSON-safe Python values; NaN/NaT -> None."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.date().isoformat()
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        try:
            return v.item()
        except Exception:  # pragma: no cover
            return str(v)
    return v


def _to_float(v: Any) -> Optional[float]:
    v = _clean(v)
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None and f > 0 else None


def _to_bool(v: Any) -> Optional[bool]:
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    if s in {"Y", "YES", "T", "TRUE", "1"}:
        return True
    if s in {"N", "NO", "F", "FALSE", "0"}:
        return False
    return None


def _to_zip(v: Any) -> Optional[str]:
    v = _clean(v)
    if v is None:
        return None
    s = str(v).split(".")[0].strip()
    return s[:5].zfill(5) if s.isdigit() else (s or None)


def _to_date(v: Any) -> Optional[str]:
    v = _clean(v)
    if v is None or v == "":
        return None
    try:
        return pd.to_datetime(v).date().isoformat()
    except Exception:
        return None


def resolve_columns(available: Sequence[str], overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Return {canonical: source_column} for every canonical field found."""
    overrides = overrides or {}
    cols = set(available)
    resolved: Dict[str, str] = {}
    for canonical, candidates in COLUMN_CANDIDATES.items():
        if canonical in overrides and overrides[canonical] in cols:
            resolved[canonical] = overrides[canonical]
            continue
        for c in candidates:
            if c in cols:
                resolved[canonical] = c
                break
    missing = [c for c in COLUMN_CANDIDATES if c not in resolved]
    if missing:
        log.info("Columns not found in source (will be NULL): %s", ", ".join(missing))
    return resolved


def infer_jurisdiction(city: Optional[str], zoning: Optional[str]) -> str:
    """Rough jurisdiction guess.

    Harmony Grove is *unincorporated* San Diego County with an Escondido mailing
    address, so the SITUS city string alone is misleading. County zoning codes
    look like 'RR', 'RS', 'A70', 'S92'; City of Escondido codes look like
    'R-1-7', 'RE-20', 'R-1-20'. Replace with a spatial join against the city
    boundary layer once it is loaded (TODO).
    """
    z = (zoning or "").strip().upper()
    c = (city or "").strip().upper()
    if z and "-" in z:
        return "city_of_escondido"
    if z and z.isalnum():
        return "san_diego_county"
    if "HARMONY" in c or "UNINCORPORATED" in c:
        return "san_diego_county"
    if c == "ESCONDIDO":
        return "unknown"  # could be either; needs boundary join
    return "unknown"


def estimate_equity(
    assessed_total: Optional[float],
    last_sale_price: Optional[float],
    last_sale_date: Optional[str],
    *,
    annual_appreciation: float = 0.045,
    purchase_ltv: float = 0.80,
    mortgage_rate: float = 0.06,
    term_years: int = 30,
) -> Dict[str, Optional[float]]:
    """Naive, clearly-labeled equity model (placeholder for an AVM).

    Prop 13 means assessed values lag the market by the years since the last
    reassessment, so we uplift the last sale price (or assessed total) by a
    flat appreciation rate, then subtract an amortized loan balance assuming a
    typical purchase LTV. Every output is an *estimate* and is flagged as such
    in the dossier and compliance layer.
    """
    base = last_sale_price or assessed_total
    if not base or base <= 0:
        return {"estimated_market_value": None, "estimated_equity": None, "equity_confidence": None}

    years = 0.0
    if last_sale_date:
        try:
            sold = datetime.fromisoformat(last_sale_date).date()
            years = max(0.0, (date.today() - sold).days / 365.25)
        except ValueError:
            years = 0.0

    market = base * ((1 + annual_appreciation) ** min(years, 30))

    # Amortized remaining balance on a purchase-money loan.
    principal = base * purchase_ltv
    r = mortgage_rate / 12
    n = term_years * 12
    k = min(int(years * 12), n)
    if r > 0:
        balance = principal * ((1 + r) ** n - (1 + r) ** k) / ((1 + r) ** n - 1)
    else:
        balance = principal * (1 - k / n)
    balance = max(0.0, balance)

    equity = max(0.0, market - balance)
    confidence = 0.55 if last_sale_price else 0.3
    if years > 25:
        confidence -= 0.15  # ancient sale data → wide error bars
    return {
        "estimated_market_value": round(market, 2),
        "estimated_equity": round(equity, 2),
        "equity_confidence": round(max(0.05, confidence), 2),
    }


def to_polygon(geom: BaseGeometry) -> Optional[Polygon]:
    """Reduce any (multi)polygon to a single valid Polygon (largest ring)."""
    if geom is None or geom.is_empty:
        return None
    geom = make_valid(geom)
    if isinstance(geom, Polygon):
        return geom
    if isinstance(geom, MultiPolygon):
        return max(geom.geoms, key=lambda g: g.area)
    if hasattr(geom, "geoms"):  # GeometryCollection
        polys = [g for g in geom.geoms if isinstance(g, (Polygon, MultiPolygon))]
        return to_polygon(MultiPolygon([p for g in polys for p in (g.geoms if isinstance(g, MultiPolygon) else [g])])) if polys else None
    return None


def to_ewkt(poly: Polygon) -> str:
    return f"SRID=4326;{poly.wkt}"


# =============================================================================
# Pipeline steps
# =============================================================================
def load_layer(path: str, layer: Optional[str] = None) -> gpd.GeoDataFrame:
    log.info("Reading %s", path)
    gdf = gpd.read_file(path, layer=layer) if layer else gpd.read_file(path)
    if gdf.crs is None:
        log.warning("Source has no CRS; assuming EPSG:4326")
        gdf = gdf.set_crs(4326)
    if gdf.crs.to_epsg() != 4326:
        log.info("Reprojecting %s -> EPSG:4326", gdf.crs)
        gdf = gdf.to_crs(4326)
    log.info("Loaded %d features", len(gdf))
    return gdf


def filter_area(
    gdf: gpd.GeoDataFrame,
    bbox: Optional[Sequence[float]],
    zips: Optional[Iterable[str]],
    zip_col: Optional[str],
) -> gpd.GeoDataFrame:
    before = len(gdf)
    if bbox:
        minx, miny, maxx, maxy = bbox
        gdf = gdf.cx[minx:maxx, miny:maxy]
        log.info("bbox filter: %d -> %d", before, len(gdf))
    if zips:
        if not zip_col:
            log.warning("ZIP filter requested but no ZIP column found; skipping ZIP filter")
        else:
            wanted = {str(z).zfill(5) for z in zips}
            mask = gdf[zip_col].map(_to_zip).isin(wanted)
            n = len(gdf)
            gdf = gdf[mask]
            log.info("zip filter %s: %d -> %d", sorted(wanted), n, len(gdf))
    return gdf


def normalize(
    gdf: gpd.GeoDataFrame,
    colmap: Dict[str, str],
    *,
    source_tag: str,
    include_owner: bool = False,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    ingested_at = datetime.now(timezone.utc).isoformat()
    skipped_geom = skipped_apn = 0

    for rec in gdf.itertuples(index=False):
        row_src = rec._asdict()

        def get(canonical: str) -> Any:
            col = colmap.get(canonical)
            return row_src.get(col) if col else None

        apn = _clean(get("apn"))
        if not apn:
            skipped_apn += 1
            continue
        apn = str(apn).strip()

        poly = to_polygon(row_src.get("geometry"))
        if poly is None:
            skipped_geom += 1
            continue

        lot_sqft = _to_float(get("lot_area_sqft"))
        if lot_sqft is None:
            acres = _to_float(get("acreage"))
            lot_sqft = round(acres * 43560, 1) if acres else None

        assessed_land = _to_float(get("assessed_land_value"))
        assessed_impr = _to_float(get("assessed_improvement_value"))
        assessed_total = (assessed_land or 0) + (assessed_impr or 0) or None
        sale_price = _to_float(get("last_sale_price"))
        sale_date = _to_date(get("last_sale_date"))
        city = _clean(get("situs_city"))
        zoning = _clean(get("zoning_code"))

        eq = estimate_equity(assessed_total, sale_price, sale_date)

        row: Dict[str, Any] = {
            "apn": apn,
            "situs_address": _clean(get("situs_address")),
            "situs_city": city,
            "situs_zip": _to_zip(get("situs_zip")),
            "jurisdiction": infer_jurisdiction(city, zoning),
            "zoning_code": zoning,
            "land_use_code": _clean(get("land_use_code")),
            "lot_area_sqft": lot_sqft,
            "year_built": _to_int(get("year_built")),
            "living_area_sqft": _to_float(get("living_area_sqft")),
            "owner_occupied": _to_bool(get("owner_occupied")),
            "assessed_land_value": assessed_land,
            "assessed_improvement_value": assessed_impr,
            "tax_year": _to_int(get("tax_year")),
            "last_sale_date": sale_date,
            "last_sale_price": sale_price,
            "tax_data": {k: _clean(row_src[k]) for k in RAW_TAX_COLUMNS if k in row_src},
            "estimated_market_value": eq["estimated_market_value"],
            "estimated_equity": eq["estimated_equity"],
            "equity_confidence": eq["equity_confidence"],
            "equity_model_version": EQUITY_MODEL_VERSION,
            "geom": to_ewkt(poly),
            "source": source_tag,
            "source_ingested_at": ingested_at,
        }
        if include_owner:
            row["owner_name"] = _clean(get("owner_name"))
            row["owner_mailing_address"] = _clean(get("owner_mailing_address"))
        rows.append(row)

    log.info("Normalized %d rows (skipped: %d no APN, %d bad geometry)", len(rows), skipped_apn, skipped_geom)
    return rows


def upsert_rows(rows: List[Dict[str, Any]], chunk_size: int = 500) -> int:
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set (see backend/.env.example)")

    client = create_client(url, key)
    total = 0
    for i in range(0, len(rows), chunk_size):
        chunk = rows[i : i + chunk_size]
        # PostgREST accepts EWKT strings for geometry columns.
        client.table("parcels").upsert(chunk, on_conflict="apn").execute()
        total += len(chunk)
        log.info("Upserted %d / %d", total, len(rows))
    return total


# =============================================================================
# CLI
# =============================================================================
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Load parcels into Vecterra Supabase/PostGIS")
    p.add_argument("--input", required=True, help="GeoJSON / .shp / .gpkg path")
    p.add_argument("--layer", help="Layer name for multi-layer sources (GPKG, GDB)")
    p.add_argument("--bbox", nargs=4, type=float, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"),
                   help=f"Bounding box filter. Default study area: {HARMONY_GROVE_BBOX}")
    p.add_argument("--zip", nargs="*", help=f"ZIP code filter (default: {sorted(HARMONY_GROVE_ZIPS)})")
    p.add_argument("--all", action="store_true", help="Disable the default Harmony Grove bbox filter")
    p.add_argument("--columns", help='JSON overrides for column mapping, e.g. \'{"apn":"APN_8"}\'')
    p.add_argument("--source-tag", default=None, help="Provenance label stored in parcels.source")
    p.add_argument("--include-owner", action="store_true", help="Ingest owner name / mailing address columns")
    p.add_argument("--chunk-size", type=int, default=500)
    p.add_argument("--dry-run", action="store_true", help="Normalize and print, but do not write")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

    overrides = json.loads(args.columns) if args.columns else None
    bbox = None if args.all else (args.bbox or HARMONY_GROVE_BBOX)
    zips = args.zip if args.zip is not None else None  # only filter by ZIP when asked

    gdf = load_layer(args.input, args.layer)
    colmap = resolve_columns(list(gdf.columns), overrides)
    gdf = filter_area(gdf, bbox, zips, colmap.get("situs_zip"))
    if gdf.empty:
        log.warning("No parcels after filtering; nothing to do")
        return 0

    source_tag = args.source_tag or f"{os.path.basename(args.input)}@{date.today().isoformat()}"
    rows = normalize(gdf, colmap, source_tag=source_tag, include_owner=args.include_owner)

    if args.dry_run:
        for r in rows[:5]:
            preview = {k: (v[:60] + "…" if k == "geom" else v) for k, v in r.items()}
            print(json.dumps(preview, indent=2, default=str))
        print(f"\n[dry-run] {len(rows)} rows ready for upsert")
        return 0

    n = upsert_rows(rows, args.chunk_size)
    log.info("Done. %d parcels upserted.", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
