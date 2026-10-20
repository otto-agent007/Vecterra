"""Study-area ingestion: SanGIS field quirks and overlay joins, on synthetic geometry (no network)."""

import json
import math
from datetime import date

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from agents.zoning_rules import evaluate_zoning
from ingestion.load_study_area import build_rows
from ingestion.study_area import (
    build_parcels,
    classify_fire,
    compose_address,
    hoa_flags,
    join_overlays,
    parse_doc_date,
    parse_two_digit_year,
    parcel_attributes,
    sewer_from_designation,
)

TODAY = date(2026, 10, 20)
AFTER = date(2027, 1, 2)

# A ~110 m square lot near Harmony Grove, and helpers to slice it.
X0, Y0, D = -117.120, 33.090, 0.001


def lot(i: int = 0):
    return box(X0 + i * 2 * D, Y0, X0 + i * 2 * D + D, Y0 + D)


def left_part(i: int, frac: float):
    x = X0 + i * 2 * D
    return box(x - D, Y0 - D, x + D * frac, Y0 + 2 * D)


def right_part(i: int, frac: float):
    x = X0 + i * 2 * D
    return box(x + D * frac, Y0 - D, x + 2 * D, Y0 + 2 * D)


def sangis(apn, i, **kw):
    rec = {
        "apn": apn, "situs_address": 100.0 + i, "situs_street": "HARMONY GROVE", "situs_suffix": "RD",
        "situs_community": "ESCONDIDO", "situs_zip": "92029-1234", "nucleus_use_cd": "111", "acreage": 0.25,
        "year_effective": "89", "total_lvg_area": 1800, "ownerocc": "Y", "asr_land": 300000, "asr_impr": 200000,
        "asr_total": 500000, "doctype": "1", "docdate": "061521", "tranum": "74173", "submap": "", "subname": "",
        "situs_juris": "CN", "bedrooms": "003", "pool": "N", "geometry": lot(i),
    }
    rec.update(kw)
    return rec


def frames(parcel_records, *, municipal=None, sra=None, lra=None, agencies=None, esc_zoning=None,
           county_zoning=None, septic=None):
    g = lambda rows: gpd.GeoDataFrame(rows, geometry="geometry", crs=4326) if rows else gpd.GeoDataFrame(geometry=[], crs=4326)
    return {
        "parcels": g(parcel_records),
        "municipal": g(municipal or []),
        "zoning_county": g(county_zoning or []),
        "zoning_escondido": pd.DataFrame(esc_zoning or [], columns=["APN", "ZONING"]),
        "fire_agencies": g(agencies or []),
        "fhsz_sra": g(sra or []),
        "fhsz_lra": g(lra or []),
        "septic_sewer": pd.DataFrame(septic or [], columns=["apn", "sewer_septic_parcel_designation",
                                                            "septic_sewer_designation_confid"]),
    }


# ---- SanGIS field quirks -----------------------------------------------------------
def test_address_is_composed_and_ordinals_stay_lowercase():
    row = {"situs_address": 934.0, "situs_pre_dir": "W", "situs_street": "4TH", "situs_suffix": "AVE", "situs_suite": "B"}
    assert compose_address(row) == "934 W 4th Ave #B"
    assert compose_address({"situs_address": 0, "situs_street": "X"}) is None


def test_two_digit_years_and_mmddyy_dates():
    assert parse_two_digit_year("89", has_building=True, today=TODAY) == 1989
    assert parse_two_digit_year("17", has_building=True, today=TODAY) == 2017
    assert parse_two_digit_year("00", has_building=False, today=TODAY) is None
    assert parse_two_digit_year("00", has_building=True, today=TODAY) == 2000
    assert parse_two_digit_year("  ", has_building=True, today=TODAY) is None
    assert parse_doc_date("120325", today=TODAY) == "2025-12-03"
    assert parse_doc_date("061599", today=TODAY) == "1999-06-15"
    assert parse_doc_date("133125", today=TODAY) is None


def test_only_grant_deeds_set_a_sale_date_and_doc_codes_are_never_stored():
    deed = parcel_attributes(sangis("1", 0), today=TODAY)
    assert deed["last_sale_date"] == "2021-06-15"
    for code in ("2", "4", "5", "6"):
        attrs = parcel_attributes(sangis("1", 0, doctype=code), today=TODAY)
        assert attrs["last_sale_date"] is None
        blob = json.dumps(attrs["tax_data"])
        assert "doctype" not in blob and '"4"' not in blob and '"5"' not in blob
        assert attrs["tax_data"]["last_doc_is_grant_deed"] is False


# ---- Fire classification ---------------------------------------------------------
def test_fire_zone_is_the_largest_share_and_touching_records_the_highest_class():
    f = classify_fire({"Moderate": 0.6, "Very High": 0.4, "resp:SRA": 1.0})
    assert f["zone"] == "Moderate"
    assert f["touching"] == "Very High"
    assert f["responsibility"] == "SRA"
    sliver = classify_fire({"None": 0.97, "High": 0.03, "resp:LRA": 1.0})
    assert sliver["zone"] == "None" and sliver["touching"] == "None"
    tie = classify_fire({"High": 0.5, "Moderate": 0.5, "resp:LRA": 1.0})
    assert tie["zone"] == "High"  # ties go to the more severe class
    assert classify_fire({})["zone"] is None


def test_join_overlays_measures_fire_shares_on_real_geometry():
    fr = frames(
        [sangis("A", 0)],
        sra=[{"FHSZ": 3, "geometry": left_part(0, 0.7)}],          # 70% of the lot, Very High, SRA
        lra=[{"FHSZ": 2, "geometry": right_part(0, 0.7)}],         # 30%, High, LRA
        municipal=[{"code": "CN", "geometry": box(-117.2, 33.0, -117.0, 33.2)}],
    )
    out = join_overlays(build_parcels(fr, today=TODAY), fr, as_of=TODAY)
    r = out.iloc[0]
    assert r["fire_hazard_zone"] == "Very High"
    assert r["fire_responsibility"] == "SRA"
    shares = r["overlay_facts"]["fire"]["shares"]
    assert math.isclose(shares["Very High"], 0.7, abs_tol=0.02) and math.isclose(shares["High"], 0.3, abs_tol=0.02)
    assert r["overlay_facts"]["fire"]["highest_zone_touching_5pct"] == "Very High"


def test_nonwildland_lra_is_no_zone():
    fr = frames([sangis("A", 0, situs_juris="ES")], lra=[{"FHSZ": -3, "geometry": box(-117.2, 33.0, -117.0, 33.2)}])
    r = join_overlays(build_parcels(fr, today=TODAY), fr, as_of=TODAY).iloc[0]
    assert r["fire_hazard_zone"] == "None" and r["fire_responsibility"] == "LRA"


# ---- Jurisdiction, zoning, agency, sewer, HOA --------------------------------------
def test_boundary_decides_jurisdiction_and_situs_is_the_fallback():
    fr = frames(
        [sangis("A", 0, situs_juris="CN"), sangis("B", 1, situs_juris="SM"), sangis("C", 2, situs_juris="ES")],
        municipal=[{"code": "ES", "geometry": lot(0).buffer(0.0001)},
                   {"code": "SM", "geometry": lot(1).buffer(0.0001)}],
    )
    out = join_overlays(build_parcels(fr, today=TODAY), fr, as_of=TODAY).set_index("apn")
    assert out.at["A", "jurisdiction"] == "city_of_escondido"    # boundary beats the parcel's own code
    assert out.at["A", "overlay_facts"]["jurisdiction"] == {"boundary": "ES", "situs_juris": "CN"}
    assert out.at["B", "jurisdiction"] == "city_of_san_marcos"
    assert out.at["C", "jurisdiction"] == "city_of_escondido"    # outside every boundary: situs fallback


def test_zoning_agency_and_sewer_joins():
    everywhere = box(-117.2, 33.0, -117.0, 33.2)
    fr = frames(
        [sangis("E1", 0, situs_juris="ES"), sangis("C1", 1, situs_juris="CN"), sangis("C2", 2, situs_juris="CN")],
        municipal=[{"code": "ES", "geometry": lot(0).buffer(0.0001)},
                   {"code": "CN", "geometry": box(X0 + 1.5 * D, Y0 - D, X0 + 6 * D, Y0 + 2 * D)}],
        esc_zoning=[{"APN": "E1", "ZONING": "RE-20"}, {"APN": "C1", "ZONING": "COUNTY"}],
        county_zoning=[{"usereg": "RR", "legend": "Residential", "geometry": everywhere}],
        agencies=[{"label": "Rancho Santa Fe FPD", "designator": "RSF", "entity": "FPD", "geometry": lot(1).buffer(0.0001)},
                  {"label": "Unserved Island 7", "designator": "", "entity": "UNSV", "geometry": lot(2).buffer(0.0001)}],
        septic=[{"apn": "E1", "sewer_septic_parcel_designation": "Assumed Sewer Connected (per infrastructure)",
                 "septic_sewer_designation_confid": 4},
                {"apn": "C1", "sewer_septic_parcel_designation": "Known Septic Connected", "septic_sewer_designation_confid": 2},
                {"apn": "C2", "sewer_septic_parcel_designation": "Not Known", "septic_sewer_designation_confid": None}],
    )
    out = join_overlays(build_parcels(fr, today=TODAY), fr, as_of=TODAY).set_index("apn")
    assert out.at["E1", "zoning_code"] == "RE-20"
    assert out.at["C1", "zoning_code"] == "RR" and out.at["C1", "zoning_desc"] == "Residential"
    assert out.at["C1", "fire_agency"] == "Rancho Santa Fe Fire Protection District"
    assert out.at["C2", "fire_agency"] is None and out.at["C2", "overlay_facts"]["fire_agency"]["unserved"] is True
    assert out.at["E1", "sewer_service"] == "sewer"
    assert out.at["C1", "sewer_service"] == "septic"
    assert out.at["C2", "sewer_service"] is None
    assert out.at["C2", "overlay_facts"]["sewer"]["designation"] == "Not Known"


def test_sewer_designation_mapping():
    assert sewer_from_designation("Likely Septic Connected") == "septic"
    assert sewer_from_designation("Assumed Septic Connected") is None  # weakest DEH call; kept in facts only
    assert sewer_from_designation("Not Known") is None
    assert sewer_from_designation(None) is None


def test_hoa_flags_whole_tract_with_a_common_lot_and_never_says_no_hoa():
    df = pd.DataFrame({
        "_submap": ["015995", "015995", "015995", "000407", "", "MOBILE HOME", "MOBILE HOME"],
        "_asr_total": [600000, 0, 650000, 500000, 0, 0, 90000],
        "land_use_code": ["111", "860", "111", "111", "860", "862", "111"],
    })
    flags = hoa_flags(df)
    assert flags["hoa"].tolist()[:3] == [True, True, True]
    assert flags["hoa_basis"].iloc[0] == "common_lot_in_subdivision"
    # no tract, a different tract, or a mobile-home "submap": no evidence, so None rather than False
    assert all(v is None for v in flags["hoa"].tolist()[3:])


# ---- Rows for Supabase -----------------------------------------------------------
def test_build_rows_are_json_safe_and_typed():
    fr = frames([sangis("A", 0, acreage=None, year_effective="05")],
                municipal=[{"code": "CN", "geometry": box(-117.2, 33.0, -117.0, 33.2)}])
    joined = join_overlays(build_parcels(fr, today=TODAY), fr, as_of=TODAY)
    rows = build_rows(joined, ingested_at="2026-10-20T00:00:00+00:00", as_of=TODAY)
    json.dumps(rows, allow_nan=False)
    r = rows[0]
    assert r["lot_area_sqft"] and r["lot_area_sqft"] > 100_000      # measured from the outline when acreage is missing
    assert isinstance(r["year_built"], int) and r["year_built"] == 2005
    assert r["situs_zip"] == "92029"
    assert r["last_sale_price"] is None
    assert r["source"].endswith("(GIS data derived from SanGIS)")
    assert "owner_name" not in r and "owner_mailing_address" not in r


def test_duplicate_apn_features_merge_into_one_parcel():
    fr = frames([sangis("A", 0), sangis("A", 1), sangis("B", 2)])
    p = build_parcels(fr, today=TODAY)
    assert sorted(p["apn"]) == ["A", "B"]


# ---- Rules engine reads the new facts --------------------------------------------------
def test_unmodeled_city_is_named_not_unresolved():
    z = evaluate_zoning({"apn": "X", "jurisdiction": "city_of_san_marcos", "lot_area_sqft": 9000}, as_of=TODAY)
    assert z["jurisdiction_name"] == "City of San Marcos"
    rules = {r["key"]: r for r in z["rule_layers"]["rules"]}
    assert rules["local_authority"]["authority"] == "City of San Marcos"
    assert rules["local_authority"]["status"] == "not_checked"
    assert z["rule_layers"]["land_use_authority"] == "City of San Marcos"


def test_inferred_hoa_is_verify_not_settled():
    base = {"apn": "X", "jurisdiction": "san_diego_county", "zoning_code": "S88", "lot_area_sqft": 6000,
            "living_area_sqft": 2200, "year_built": 2017}
    inferred = {r["key"]: r for r in evaluate_zoning({**base, "hoa": True, "hoa_basis": "common_lot_in_subdivision"},
                                                     as_of=AFTER)["rule_layers"]["rules"]}
    assert inferred["hoa_review"]["status"] == "verify"
    assert "suggest" in inferred["hoa_review"]["statement"]
    confirmed = {r["key"]: r for r in evaluate_zoning({**base, "hoa": True, "hoa_basis": "confirmed",
                                                       "hoa_name": "Example Association"},
                                                      as_of=AFTER)["rule_layers"]["rules"]}
    assert confirmed["hoa_review"]["status"] == "settled"
    assert confirmed["hoa_review"]["authority"] == "Example Association"
