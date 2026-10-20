"""Study-area parcels with overlay facts joined, from public ArcGIS services.

Fetches the SanGIS parcel layer (served by SANDAG) for the Harmony Grove bbox,
reshapes it to the ``parcels`` schema, and joins the overlays that
``agents/rule_layers.py`` reads:

=====================  =====================================================  ==========================
Column                 Source                                                 Join
=====================  =====================================================  ==========================
jurisdiction           SANDAG Municipal_Boundaries                            representative point
zoning_code / desc     Escondido Zoning (by APN); SANDAG Zoning_Unincorporated  APN / representative point
fire_hazard_zone       CAL FIRE FHSZ, SRA (2023 map) + LRA (2025 map)          largest share of lot area
fire_responsibility    same layers (SRA vs LRA)                               largest share of lot area
fire_agency            SANDAG Fire_Agencies                                   representative point
sewer_service / basis  County DEH designation; County wastewater districts;   APN / point /
                       Escondido sewer mains                                  nearest main (ft)
hoa / hoa_basis        parcels layer: subdivision with a $0 common-area lot   submap
=====================  =====================================================  ==========================

Everything the joins measured (per-class fire shares, raw designations, which
sources were used) goes into ``overlay_facts`` so a dossier can say how sure it is.

Privacy and fair-housing posture: the SANDAG parcel layer carries no owner
names. Last-document type is used only to keep a grant-deed date as the sale
date; the code itself (including death-certificate codes 4/5) is never stored.

Attribution: parcel and boundary data are derived from SanGIS / SANDAG
("GIS data derived from SanGIS"); fire zones from CAL FIRE.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import geopandas as gpd
import pandas as pd

log = logging.getLogger("vecterra.study_area")

STUDY_AREA_BBOX: Tuple[float, float, float, float] = (-117.145, 33.070, -117.090, 33.120)
# NAD83 / California zone 6 (US ft): areas and shares are measured in square feet.
WORK_CRS = 2230
SANGIS_ATTRIBUTION = "GIS data derived from SanGIS"

# A zone has to cover at least this share of the lot to count as "touching" it.
# Smaller slivers are usually boundary misalignment (SanGIS boundaries aren't
# accurate finer than 1:24,000), not real overlap.
FIRE_TOUCH_MIN_SHARE = 0.05


@dataclass(frozen=True)
class Source:
    key: str
    url: str
    title: str
    as_of: str
    note: str
    geometry: bool = True
    page_size: int = 2000
    order_by: str = "objectid"


SOURCES: Dict[str, Source] = {
    s.key: s
    for s in (
        Source("parcels", "https://geo.sandag.org/server/rest/services/Hosted/Parcels/FeatureServer/0",
               "SanGIS parcels (via SANDAG)", "live", SANGIS_ATTRIBUTION),
        Source("municipal", "https://geo.sandag.org/server/rest/services/Hosted/Municipal_Boundaries/FeatureServer/0",
               "SANDAG municipal boundaries", "live", SANGIS_ATTRIBUTION),
        Source("zoning_county", "https://geo.sandag.org/server/rest/services/Hosted/Zoning_Unincorporated/FeatureServer/0",
               "County of San Diego zoning (unincorporated)", "live", SANGIS_ATTRIBUTION),
        Source("zoning_escondido", "https://services2.arcgis.com/eJcVbjTyyZIzZ5Ye/arcgis/rest/services/Zoning/FeatureServer/0",
               "City of Escondido zoning by APN", "live", "City of Escondido GIS",
               geometry=False, page_size=1000, order_by="OBJECTID"),
        Source("fire_agencies", "https://geo.sandag.org/server/rest/services/Hosted/Fire_Agencies/FeatureServer/0",
               "SANDAG fire agencies", "live", SANGIS_ATTRIBUTION),
        Source("fhsz_sra", "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services/FHSZSRA_23_3/FeatureServer/0",
               "CAL FIRE Fire Hazard Severity Zones, State Responsibility Area", "2024-04-01",
               "CAL FIRE / State Fire Marshal", order_by="OBJECTID"),
        Source("fhsz_lra", "https://services1.arcgis.com/jUJYIo9tSA7EHvfZ/arcgis/rest/services/FHSALRA25_v1_All/FeatureServer/0",
               "CAL FIRE Fire Hazard Severity Zones, Local Responsibility Area", "2025-03-24",
               "CAL FIRE / State Fire Marshal", order_by="OBJECTID"),
        Source("septic_sewer", "https://geo.sandag.org/server/rest/services/Hosted/WW_Septic_Sewer_Public/FeatureServer/0",
               "County DEH septic/sewer designation", "live", SANGIS_ATTRIBUTION, geometry=False),
        Source("wastewater_districts", "https://geo.sandag.org/server/rest/services/Hosted/Wastewater_District_CN/FeatureServer/0",
               "County wastewater (sewer) district service areas", "live", SANGIS_ATTRIBUTION),
        Source("sewer_gravity_escondido",
               "https://services2.arcgis.com/eJcVbjTyyZIzZ5Ye/arcgis/rest/services/Public_Utilities/FeatureServer/3",
               "City of Escondido sewer gravity mains", "live", "City of Escondido GIS",
               page_size=1000, order_by="OBJECTID"),
        Source("sewer_pressure_escondido",
               "https://services2.arcgis.com/eJcVbjTyyZIzZ5Ye/arcgis/rest/services/Public_Utilities/FeatureServer/2",
               "City of Escondido sewer pressurized mains", "live", "City of Escondido GIS",
               page_size=1000, order_by="OBJECTID"),
    )
}

MUNICIPAL_CODE_TO_JURISDICTION = {
    "CN": "san_diego_county",
    "ES": "city_of_escondido",
    "SM": "city_of_san_marcos",
    "SD": "city_of_san_diego",
}

FHSZ_CLASS = {3: "Very High", 2: "High", 1: "Moderate"}  # anything else (e.g. -3 NonWildland) -> "None"
FIRE_ORDER = ("Very High", "High", "Moderate", "None")

# Sewer vs septic. County DEH's designation is the starting point, but its weakest
# call ("Assumed Septic Connected", confidence 6) is wrong inside sewered Harmony Grove
# Village, and ~1,600 Escondido lots have no DEH record at all. Two public layers fill
# the gap: the County's wastewater-district service areas and Escondido's sewer mains.
# A main within SEWER_MAIN_NEAR_FT shows sewer is *available* (what an ADU needs), not
# that the house is connected, so that basis is reported as "verify".
SEWER_MAIN_NEAR_FT = 50.0
SEPTIC_NO_MAIN_FT = 200.0

# sewer_basis values. The rules engine reads these.
SEWER_BASES = (
    "deh_known_septic",            # DEH: known septic                          -> septic (settled)
    "deh_known_sewer",             # DEH: known sewer                           -> sewer  (settled)
    "sewer_district",              # inside a wastewater district               -> sewer  (settled)
    "deh_assumed_sewer",           # DEH: assumed sewer, per infrastructure     -> sewer  (settled)
    "deh_likely_septic",           # DEH: likely septic                         -> septic (verify)
    "sewer_main_nearby",           # public main within 50 ft                   -> sewer  (verify)
    "deh_assumed_septic_no_main",  # DEH assumed septic, no main within 200 ft  -> septic (verify)
)


def resolve_sewer(designation: Any, *, in_sewer_district: bool,
                  nearest_main_ft: Optional[float]) -> Tuple[Optional[str], Optional[str]]:
    """(sewer_service, sewer_basis) from DEH's designation plus the two infrastructure layers.

    A DEH septic finding of "known" or "likely" always wins, even next to a main: some
    lots with a sewer in the street were never connected.
    """
    d = _s(designation).lower()
    near = nearest_main_ft is not None and nearest_main_ft <= SEWER_MAIN_NEAR_FT
    far = nearest_main_ft is None or nearest_main_ft > SEPTIC_NO_MAIN_FT
    if d == "known septic connected":
        return "septic", "deh_known_septic"
    if d == "known sewer connected":
        return "sewer", "deh_known_sewer"
    if d == "likely septic connected":
        return "septic", "deh_likely_septic"
    if in_sewer_district:
        return "sewer", "sewer_district"
    if d.startswith("assumed sewer connected"):
        return "sewer", "deh_assumed_sewer"
    if near:
        return "sewer", "sewer_main_nearby"
    if d == "assumed septic connected" and far:
        return "septic", "deh_assumed_septic_no_main"
    return None, None  # "Not Known", no record, or assumed septic with a main 50-200 ft away

# Fire agency labels in SANDAG Fire_Agencies -> the name a homeowner would recognize.
FIRE_AGENCY_NAMES = {
    "Escondido": "Escondido Fire Department",
    "Rancho Santa Fe FPD": "Rancho Santa Fe Fire Protection District",
    "Rincon del Diablo MWD": "Rincon del Diablo MWD fire district (ID E)",
    "San Marcos": "San Marcos Fire Department",
    "San Marcos FPD": "San Marcos Fire Protection District",
    "San Diego": "San Diego Fire-Rescue Department",
}

# nucleus_use_cd codes for common-area lots (HOA open space, private streets).
COMMON_AREA_USE_CODES = {"860", "861", "862"}
# Subdivision values that aren't recorded tracts.
NON_TRACT_SUBMAPS = {"", "MOBILE HOME"}


# =============================================================================
# Fetching
# =============================================================================
def _get_json(url: str, params: Dict[str, Any], retries: int = 5) -> Dict[str, Any]:
    """GET with backoff for transient network or server errors."""
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(full, timeout=180) as resp:
                data = json.load(resp)
            if "error" in data:
                raise RuntimeError(f"ArcGIS error from {url}: {data['error']}")
            return data
        except Exception as exc:
            if attempt == retries:
                raise
            wait = 5 * 2 ** (attempt - 1)
            log.warning("%s (attempt %d/%d); retrying in %ds", exc, attempt, retries, wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_layer(source: Source, bbox: Sequence[float]) -> List[Dict[str, Any]]:
    """Every feature intersecting ``bbox``, paged. GeoJSON features, or attribute dicts when geometry is off."""
    out: List[Dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "f": "geojson" if source.geometry else "json",
            "where": "1=1",
            "geometry": ",".join(str(v) for v in bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "outSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "*",
            "returnGeometry": "true" if source.geometry else "false",
            "resultOffset": offset,
            "resultRecordCount": source.page_size,
            "orderByFields": source.order_by,
        }
        data = _get_json(f"{source.url}/query", params)
        page = data.get("features") or []
        if not source.geometry:
            page = [f.get("attributes") or {} for f in page]
        out.extend(page)
        more = data.get("exceededTransferLimit") or (data.get("properties") or {}).get("exceededTransferLimit")
        if not page or (not more and len(page) < source.page_size):
            break
        offset += len(page)
    log.info("Fetched %d records from %s", len(out), source.title)
    return out


def fetch_all(bbox: Sequence[float] = STUDY_AREA_BBOX, cache_dir: Optional[Path] = None,
              from_cache: bool = False) -> Dict[str, Any]:
    """Fetch every source (or read it back from ``cache_dir``). Returns raw GeoJSON / attribute lists."""
    raw: Dict[str, Any] = {}
    for key, src in SOURCES.items():
        path = cache_dir / f"{key}.json" if cache_dir else None
        if from_cache:
            if not path or not path.exists():
                raise FileNotFoundError(f"--from-cache: {path} is missing; run once without --from-cache first")
            raw[key] = json.loads(path.read_text())
            continue
        records = fetch_layer(src, bbox)
        raw[key] = {"type": "FeatureCollection", "features": records} if src.geometry else records
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(raw[key]))
    return raw


def to_frames(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Raw fetch results -> GeoDataFrames (EPSG:4326) / DataFrames."""
    frames: Dict[str, Any] = {}
    for key, src in SOURCES.items():
        if src.geometry:
            feats = raw[key]["features"] if isinstance(raw[key], dict) else raw[key]
            gdf = gpd.GeoDataFrame.from_features(feats, crs=4326) if feats else gpd.GeoDataFrame(geometry=[], crs=4326)
            frames[key] = gdf
        else:
            frames[key] = pd.DataFrame(raw[key])
    return frames


# =============================================================================
# Parcel attributes (SanGIS field quirks)
# =============================================================================
def _s(v: Any) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v).strip()


def compose_address(row: Dict[str, Any]) -> Optional[str]:
    """SanGIS splits the situs address; the number field alone is not an address."""
    raw_number = row.get("situs_address")
    try:
        number = str(int(float(raw_number))) if _s(raw_number) else ""
    except ValueError:
        number = _s(raw_number)
    if number in ("", "0"):
        return None
    parts = [number, _s(row.get("situs_fraction")), _s(row.get("situs_pre_dir")),
             _s(row.get("situs_street")), _s(row.get("situs_suffix")), _s(row.get("situs_post_dir"))]
    addr = " ".join(p for p in parts if p)
    unit = _s(row.get("situs_suite")) or _s(row.get("situs_building"))
    if unit:
        addr += f" #{unit}"
    if not addr:
        return None
    # Title-case street words but keep ordinals ("4th", not "4Th").
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), addr.title())


def parse_two_digit_year(v: Any, *, has_building: bool, today: Optional[date] = None) -> Optional[int]:
    """SanGIS ``year_effective`` is two digits. '00' on a lot with no living area means 'none recorded'."""
    s = _s(v)
    if not s.isdigit():
        return None
    yy = int(s[-2:])
    if yy == 0 and not has_building:
        return None
    pivot = (today or date.today()).year % 100
    return 2000 + yy if yy <= pivot else 1900 + yy


def parse_doc_date(v: Any, *, today: Optional[date] = None) -> Optional[str]:
    """SanGIS ``docdate`` is MMDDYY."""
    s = _s(v)
    if len(s) != 6 or not s.isdigit():
        return None
    mm, dd, yy = int(s[:2]), int(s[2:4]), int(s[4:])
    pivot = (today or date.today()).year % 100
    try:
        return date(2000 + yy if yy <= pivot else 1900 + yy, mm, dd).isoformat()
    except ValueError:
        return None


def normalize_zip(v: Any) -> Optional[str]:
    s = _s(v).split("-")[0].strip()
    return s[:5] if len(s) >= 5 and s[:5].isdigit() else None


def parcel_attributes(row: Dict[str, Any], *, today: Optional[date] = None) -> Dict[str, Any]:
    """One SanGIS parcel record -> parcels columns (minus overlays and geometry)."""
    living = row.get("total_lvg_area")
    living = float(living) if living not in (None, "") and not (isinstance(living, float) and math.isnan(living)) else None
    is_grant_deed = _s(row.get("doctype")) == "1"
    acreage = row.get("acreage")
    try:
        acreage = float(acreage)
    except (TypeError, ValueError):
        acreage = None
    return {
        "apn": _s(row.get("apn")),
        "situs_address": compose_address(row),
        "situs_city": _s(row.get("situs_community")).title() or None,
        "situs_zip": normalize_zip(row.get("situs_zip")),
        "land_use_code": _s(row.get("nucleus_use_cd")) or None,
        "lot_area_sqft": round(acreage * 43560, 1) if acreage and acreage > 0 else None,
        "year_built": parse_two_digit_year(row.get("year_effective"), has_building=bool(living), today=today),
        "living_area_sqft": living if living else None,
        "owner_occupied": True if _s(row.get("ownerocc")).upper() == "Y" else None,
        "assessed_land_value": row.get("asr_land"),
        "assessed_improvement_value": row.get("asr_impr"),
        # Only a grant deed's date is treated as a sale date. Other document types
        # (quitclaims, trustee's deeds, death certificates) are dropped, not stored.
        "last_sale_date": parse_doc_date(row.get("docdate"), today=today) if is_grant_deed else None,
        "tax_data": {
            "asr_land": row.get("asr_land"),
            "asr_impr": row.get("asr_impr"),
            "asr_total": row.get("asr_total"),
            "tax_rate_area": _s(row.get("tranum")) or None,
            "asr_landuse": row.get("asr_landuse"),
            "nucleus_use_cd": _s(row.get("nucleus_use_cd")) or None,
            "unit_qty": row.get("unitqty"),
            "bedrooms": int(_s(row.get("bedrooms"))) if _s(row.get("bedrooms")).isdigit() else None,
            "baths": _s(row.get("baths")) or None,
            "pool": {"Y": True, "N": False}.get(_s(row.get("pool")).upper()),
            "garage_conversion": {"Y": True, "N": False}.get(_s(row.get("garage_conversion")).upper()),
            "submap": _s(row.get("submap")) or None,
            "subname": _s(row.get("subname")) or None,
            "situs_juris": _s(row.get("situs_juris")) or None,
            "last_doc_is_grant_deed": is_grant_deed,
        },
    }


# =============================================================================
# Overlay joins (pure functions over GeoDataFrames; tested without network)
# =============================================================================
def _points(parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    pts = parcels[["apn", "geometry"]].copy()
    pts["geometry"] = parcels.geometry.representative_point()
    return pts


def _point_lookup(parcels: gpd.GeoDataFrame, layer: gpd.GeoDataFrame, cols: List[str]) -> pd.DataFrame:
    """Attributes of the layer polygon containing each parcel's representative point (first match)."""
    if layer.empty:
        return pd.DataFrame(index=parcels.index, columns=cols)
    joined = gpd.sjoin(_points(parcels), layer[cols + ["geometry"]], how="left", predicate="within")
    joined = joined[~joined.index.duplicated(keep="first")]
    return joined.reindex(parcels.index)[cols]


def join_jurisdiction(parcels: gpd.GeoDataFrame, municipal: gpd.GeoDataFrame) -> pd.DataFrame:
    """Boundary layer decides; the parcel's own situs_juris is a fallback and a cross-check."""
    hit = _point_lookup(parcels, municipal, ["code"])["code"]
    situs = parcels.get("situs_juris", pd.Series(index=parcels.index, dtype=object))
    code = hit.where(hit.notna(), situs)
    return pd.DataFrame({
        "jurisdiction": code.map(lambda c: MUNICIPAL_CODE_TO_JURISDICTION.get(_s(c), "unknown")),
        "_juris_boundary": hit,
        "_juris_situs": situs,
    }, index=parcels.index)


def fire_shares(parcels: gpd.GeoDataFrame, sra: gpd.GeoDataFrame, lra: gpd.GeoDataFrame) -> pd.DataFrame:
    """Share of each lot in each fire class and responsibility area (projected; shares of lot area)."""
    zones = []
    for resp, layer in (("SRA", sra), ("LRA", lra)):
        if layer.empty:
            continue
        z = layer[["FHSZ", "geometry"]].copy()
        z["cls"] = z["FHSZ"].map(lambda v: FHSZ_CLASS.get(int(v), "None") if pd.notna(v) else "None")
        z["resp"] = resp
        zones.append(z[["cls", "resp", "geometry"]])
    if not zones:
        return pd.DataFrame(index=parcels.index)
    zones_gdf = gpd.GeoDataFrame(pd.concat(zones, ignore_index=True), crs=4326).to_crs(WORK_CRS)
    lots = parcels[["geometry"]].to_crs(WORK_CRS)
    lots["_lot_area"] = lots.area
    lots["_pid"] = lots.index
    inter = gpd.overlay(lots, zones_gdf, how="intersection", keep_geom_type=True)
    if inter.empty:
        return pd.DataFrame(index=parcels.index)
    inter["share"] = inter.area / inter["_lot_area"]
    cls = inter.pivot_table(index="_pid", columns="cls", values="share", aggfunc="sum", fill_value=0.0)
    resp = inter.pivot_table(index="_pid", columns="resp", values="share", aggfunc="sum", fill_value=0.0)
    resp.columns = [f"resp:{c}" for c in resp.columns]
    return cls.join(resp, how="outer").reindex(parcels.index)


def classify_fire(shares: Dict[str, float]) -> Dict[str, Any]:
    """Largest-share class (what the dossier states) and the highest class touching >= 5% (a caveat)."""
    cls = {c: float(shares.get(c, 0.0) or 0.0) for c in FIRE_ORDER}
    resp = {r: float(shares.get(f"resp:{r}", 0.0) or 0.0) for r in ("SRA", "LRA")}
    if sum(cls.values()) <= 0:
        return {"zone": None, "touching": None, "responsibility": None, "shares": {}, "responsibility_shares": {}}
    zone = max(FIRE_ORDER, key=lambda c: (cls[c], -FIRE_ORDER.index(c)))  # ties go to the more severe class
    touching = next((c for c in FIRE_ORDER[:3] if cls[c] >= FIRE_TOUCH_MIN_SHARE), "None")
    responsibility = max(resp, key=resp.get) if sum(resp.values()) > 0 else None
    return {
        "zone": zone,
        "touching": touching,
        "responsibility": responsibility,
        "shares": {c: round(v, 4) for c, v in cls.items() if v > 0},
        "responsibility_shares": {r: round(v, 4) for r, v in resp.items() if v > 0},
    }


def sewer_infrastructure(parcels: gpd.GeoDataFrame, districts: gpd.GeoDataFrame,
                         mains: List[Optional[gpd.GeoDataFrame]]) -> pd.DataFrame:
    """Per lot: the wastewater district containing it (if any) and the distance to the nearest public sewer main."""
    if districts is not None and not districts.empty:
        district = _point_lookup(parcels, districts, ["label"])["label"]
    else:
        district = pd.Series(None, index=parcels.index, dtype=object)
    lines = [m[["geometry"]] for m in mains if m is not None and not m.empty]
    dist = pd.Series(float("nan"), index=parcels.index)
    if lines:
        main_gdf = gpd.GeoDataFrame(pd.concat(lines, ignore_index=True), crs=4326).to_crs(WORK_CRS)
        lots = parcels[["geometry"]].to_crs(WORK_CRS)
        near = gpd.sjoin_nearest(lots, main_gdf, how="left", max_distance=SEPTIC_NO_MAIN_FT, distance_col="_d")
        near = near[~near.index.duplicated(keep="first")]
        dist = near["_d"].reindex(parcels.index)
    return pd.DataFrame({"district": district, "nearest_main_ft": dist}, index=parcels.index)


def hoa_flags(parcels: pd.DataFrame) -> pd.DataFrame:
    """Likely common-interest development: the lot's recorded subdivision contains a $0 common-area lot.

    Misses developments that deeded their common area to a city, and can't prove
    the absence of an HOA, so unflagged lots get ``None`` (not ``False``).
    """
    sub = parcels["_submap"].map(_s)
    common = (parcels["_asr_total"].fillna(-1) == 0) & parcels["land_use_code"].isin(COMMON_AREA_USE_CODES)
    tracts = set(sub[common & ~sub.isin(NON_TRACT_SUBMAPS)])
    flagged = sub.isin(tracts) & ~sub.isin(NON_TRACT_SUBMAPS)
    return pd.DataFrame({
        "hoa": flagged.map(lambda f: True if f else None),
        "hoa_basis": flagged.map(lambda f: "common_lot_in_subdivision" if f else None),
    }, index=parcels.index)


def join_overlays(parcels: gpd.GeoDataFrame, frames: Dict[str, Any], *, as_of: Optional[date] = None) -> gpd.GeoDataFrame:
    """Add jurisdiction, zoning, fire, sewer and HOA columns plus ``overlay_facts`` to normalized parcels.

    ``parcels`` must carry ``apn``, ``land_use_code``, ``_submap``, ``_asr_total``,
    ``situs_juris`` and geometry in EPSG:4326.
    """
    as_of = as_of or date.today()
    out = parcels.copy()

    juris = join_jurisdiction(out, frames["municipal"])
    out["jurisdiction"] = juris["jurisdiction"]

    # Zoning: Escondido publishes zoning by APN; the County layer is polygons.
    esc = frames["zoning_escondido"]
    esc_map = dict(zip(esc["APN"].map(_s), esc["ZONING"].map(_s))) if not esc.empty else {}
    county = _point_lookup(out, frames["zoning_county"], ["usereg", "legend"])
    zoning_code, zoning_desc, zoning_src = [], [], []
    for idx, row in out.iterrows():
        if row["jurisdiction"] == "city_of_escondido" and esc_map.get(row["apn"]) not in (None, "", "COUNTY"):
            zoning_code.append(esc_map[row["apn"]]); zoning_desc.append(None); zoning_src.append("zoning_escondido")
        elif row["jurisdiction"] == "san_diego_county" and _s(county.at[idx, "usereg"]):
            zoning_code.append(_s(county.at[idx, "usereg"])); zoning_desc.append(_s(county.at[idx, "legend"]) or None)
            zoning_src.append("zoning_county")
        else:
            zoning_code.append(None); zoning_desc.append(None); zoning_src.append(None)
    out["zoning_code"] = pd.Series(zoning_code, index=out.index, dtype=object)
    out["zoning_desc"] = pd.Series(zoning_desc, index=out.index, dtype=object)

    shares = fire_shares(out, frames["fhsz_sra"], frames["fhsz_lra"])
    agency = _point_lookup(out, frames["fire_agencies"], ["label", "designator", "entity"])

    infra = sewer_infrastructure(out, frames.get("wastewater_districts"),
                                 [frames.get("sewer_gravity_escondido"), frames.get("sewer_pressure_escondido")])
    sewer_df = frames["septic_sewer"]
    sewer_map: Dict[str, Tuple[str, Any]] = {}
    if not sewer_df.empty:
        for r in sewer_df.drop_duplicates("apn").itertuples(index=False):
            sewer_map[_s(r.apn)] = (_s(r.sewer_septic_parcel_designation), getattr(r, "septic_sewer_designation_confid", None))

    hoa = hoa_flags(out)
    out["hoa"], out["hoa_basis"] = hoa["hoa"], hoa["hoa_basis"]
    out["hoa_name"] = None

    fire_zone, fire_resp, fire_agency, sewer, sewer_basis, facts = [], [], [], [], [], []
    for i, (idx, row) in enumerate(out.iterrows()):
        f = classify_fire(shares.loc[idx].dropna().to_dict() if idx in shares.index else {})
        label = _s(agency.at[idx, "label"])
        unserved = label.lower().startswith("unserved")
        designation, confidence = sewer_map.get(row["apn"], (None, None))
        district = _s(infra.at[idx, "district"]) or None
        main_ft = infra.at[idx, "nearest_main_ft"]
        main_ft = None if pd.isna(main_ft) else round(float(main_ft), 1)
        svc, basis = resolve_sewer(designation, in_sewer_district=bool(district), nearest_main_ft=main_ft)
        fire_zone.append(f["zone"]); fire_resp.append(f["responsibility"])
        fire_agency.append(None if (unserved or not label) else FIRE_AGENCY_NAMES.get(label, label))
        sewer.append(svc); sewer_basis.append(basis)
        facts.append({
            "as_of": as_of.isoformat(),
            "jurisdiction": {"boundary": _s(juris.at[idx, "_juris_boundary"]) or None,
                             "situs_juris": _s(juris.at[idx, "_juris_situs"]) or None},
            "zoning": {"source": zoning_src[i]},
            "fire": {"largest_share_zone": f["zone"], "highest_zone_touching_5pct": f["touching"],
                     "shares": f["shares"], "responsibility_shares": f["responsibility_shares"]},
            "fire_agency": {"label": label or None, "designator": _s(agency.at[idx, "designator"]) or None,
                            "unserved": unserved},
            "sewer": {"designation": designation or None,
                      "confidence": int(confidence) if confidence not in (None, "") and not pd.isna(confidence) else None,
                      "basis": basis,
                      "district": district,
                      "nearest_main_ft": main_ft},  # null when no public main is mapped within 200 ft
            "hoa": ({"basis": row["hoa_basis"], "submap": _s(row["_submap"]) or None} if row["hoa"] else None),
            "sources": {k: {"title": SOURCES[k].title, "as_of": SOURCES[k].as_of, "note": SOURCES[k].note}
                        for k in ("parcels", "municipal", "fhsz_sra", "fhsz_lra", "fire_agencies", "septic_sewer",
                                  "wastewater_districts", "sewer_gravity_escondido")},
        })
    obj = lambda values: pd.Series(values, index=out.index, dtype=object)  # keep None as None, not NaN
    out["fire_hazard_zone"], out["fire_responsibility"] = obj(fire_zone), obj(fire_resp)
    out["fire_agency"], out["sewer_service"], out["overlay_facts"] = obj(fire_agency), obj(sewer), obj(facts)
    out["sewer_basis"] = obj(sewer_basis)
    out["overlays_as_of"] = as_of.isoformat()
    return out


# =============================================================================
# Assembly
# =============================================================================
def build_parcels(frames: Dict[str, Any], *, today: Optional[date] = None) -> gpd.GeoDataFrame:
    """Normalize the SanGIS parcel layer: one row per APN (multi-feature APNs are merged)."""
    gdf = frames["parcels"]
    gdf = gdf[gdf["apn"].map(_s) != ""].copy()
    gdf["apn"] = gdf["apn"].map(_s)
    dupes = gdf["apn"].duplicated(keep=False)
    if dupes.any():
        merged = gdf[dupes].dissolve(by="apn", aggfunc="first").reset_index()
        gdf = pd.concat([gdf[~dupes], merged], ignore_index=True)
        log.info("Merged %d features sharing an APN into %d parcels", int(dupes.sum()), merged.shape[0])
    rows = []
    for rec in gdf.to_dict("records"):
        attrs = parcel_attributes(rec, today=today)
        attrs["_submap"] = rec.get("submap")
        attrs["_asr_total"] = rec.get("asr_total")
        attrs["situs_juris"] = _s(rec.get("situs_juris")) or None
        attrs["geometry"] = rec["geometry"]
        rows.append(attrs)
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=4326)


def summarize(gdf: pd.DataFrame) -> Dict[str, Any]:
    """Counts by jurisdiction for the columns the rules engine reads."""
    def tab(col: str) -> Dict[str, Dict[str, int]]:
        t = pd.crosstab(gdf["jurisdiction"], gdf[col].fillna("(none)").astype(str))
        return {j: {k: int(v) for k, v in r.items() if v} for j, r in t.iterrows()}
    return {
        "parcels": int(len(gdf)),
        "jurisdiction": {k: int(v) for k, v in gdf["jurisdiction"].value_counts().items()},
        "fire_hazard_zone": tab("fire_hazard_zone"),
        "fire_responsibility": tab("fire_responsibility"),
        "fire_agency": tab("fire_agency"),
        "sewer_service": tab("sewer_service"),
        "sewer_basis": tab("sewer_basis"),
        "hoa": tab("hoa"),
        "zoning_code_missing": {j: int(n) for j, n in gdf[gdf["zoning_code"].isna()]["jurisdiction"].value_counts().items()},
    }

