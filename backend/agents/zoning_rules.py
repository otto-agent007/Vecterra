"""Deterministic zoning / ADU feasibility rules for the Harmony Grove study area.

Two jurisdictions overlap the study area:

* **Unincorporated San Diego County** — Harmony Grove proper and Harmony Grove
  Village (county zoning codes such as RR, RS, A70, S88/S92 specific plans).
* **City of Escondido** — parcels inside city limits that share the 92029 ZIP
  (city codes such as R-1-7, R-1-20, RE-20).

Every numeric value below carries a ``verify`` note. California ADU law and
local ordinances are amended almost every legislative session; treat these as
MVP defaults to be confirmed against the current municipal code and the
state ADU handbook before any figure reaches a homeowner.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from .rule_layers import STATE_FLOOR, resolve_rules

RULES_VERSION = "zoning-rules-v0.2"

# State-law floor that applies regardless of jurisdiction (Gov. Code ADU
# statutes, formerly § 65852.2; recodified as §§ 66310 et seq.).
STATE_ADU_BASELINE: Dict[str, Any] = {
    "source": "California Government Code ADU statutes (formerly § 65852.2; now §§ 66310–66342)",
    "verify": "Confirm current text at leginfo.legislature.ca.gov and the HCD ADU Handbook.",
    "min_side_rear_setback_ft": STATE_FLOOR["max_side_rear_setback_ft"],  # locals may not require more
    "by_right_min_sqft_cannot_be_denied": STATE_FLOOR["guaranteed_adu_sqft"],
    "typical_local_max_detached_sqft": STATE_FLOOR["default_detached_cap_sqft"],
    "max_height_ft_minimum_allowed": STATE_FLOOR["guaranteed_height_ft"],
    "min_lot_size_requirement_allowed": False,
    "jadu_max_sqft": STATE_FLOOR["jadu_max_sqft"],
    "owner_occupancy_required_for_adu": False,
    "parking_replacement_required_when_garage_converted": False,
}
# Unit counts, dated rules (AB 956 from 2027), local caps and overlays live in
# rule_layers.py, which also records who sets each rule and how conflicts resolve.

JURISDICTION_RULES: Dict[str, Dict[str, Any]] = {
    "san_diego_county": {
        "name": "County of San Diego (unincorporated)",
        "ordinance": "San Diego County Zoning Ordinance § 6156 (Accessory Dwelling Units)",
        "verify": "Confirm with County Planning & Development Services; Harmony Grove Village has a Specific Plan overlay.",
        "residential_zone_prefixes": ("RR", "RS", "RM", "RV", "A70", "A72", "S88", "S92"),
        "adu_max_attached_pct_of_primary": 50,
        "front_setback_ft_typical": 20,
        "notes": [
            "Rural residential (RR) lots in Harmony Grove are frequently 1–4 acres, making detached ADUs physically straightforward.",
            "Septic capacity, fire-access road width, and Very High Fire Hazard Severity Zone requirements often govern feasibility more than zoning setbacks.",
            "Harmony Grove Village (S88 Specific Plan) parcels are small; check HOA CC&Rs in addition to county rules.",
        ],
    },
    "city_of_escondido": {
        "name": "City of Escondido",
        "ordinance": "Escondido Zoning Code Article 44 (Accessory Dwelling Units)",
        "verify": "Confirm with City of Escondido Planning Division.",
        "residential_zone_prefixes": ("R-1", "RE", "R-2", "R-3", "R-4", "R-5"),
        "adu_max_attached_pct_of_primary": 50,
        "front_setback_ft_typical": 20,
        "notes": [
            "Escondido offers pre-approved ADU plans; eligibility depends on lot configuration.",
        ],
    },
    "unknown": {
        "name": "Jurisdiction not yet resolved",
        "ordinance": None,
        "verify": "Resolve jurisdiction via spatial join with the city boundary layer before issuing guidance.",
        "residential_zone_prefixes": (),
        "adu_max_attached_pct_of_primary": 50,
        "front_setback_ft_typical": 20,
        "notes": ["Only the state-law floor is applied until jurisdiction is confirmed."],
    },
}


def _is_residential(zoning_code: Optional[str], prefixes: tuple) -> Optional[bool]:
    if not zoning_code:
        return None
    z = zoning_code.strip().upper()
    if not prefixes:
        return None
    return any(z.startswith(p) for p in prefixes)


def estimate_primary_footprint(living_area_sqft: Optional[float], year_built: Optional[int]) -> float:
    """Rough ground-floor footprint. Pre-1990 Harmony Grove stock is mostly single-story."""
    if not living_area_sqft:
        return 1800.0
    stories = 1 if (year_built or 0) < 1990 or living_area_sqft < 2200 else 2
    return living_area_sqft / stories * 1.15  # garage/porch allowance


def evaluate_zoning(parcel: Dict[str, Any], *, as_of: Optional[date] = None) -> Dict[str, Any]:
    """Return an ADU-feasibility snapshot for a parcel row, with every rule's layer and source.

    ``as_of`` decides which dated rules are in force (e.g. AB 956 from 2027-01-01);
    it defaults to today.
    """
    as_of = as_of or date.today()
    jurisdiction = parcel.get("jurisdiction") or "unknown"
    if jurisdiction not in JURISDICTION_RULES:
        jurisdiction = "unknown"
    rules = JURISDICTION_RULES[jurisdiction]
    zoning_code = parcel.get("zoning_code")
    lot = float(parcel.get("lot_area_sqft") or 0)
    living = parcel.get("living_area_sqft")
    year_built = parcel.get("year_built")

    residential = _is_residential(zoning_code, rules["residential_zone_prefixes"])
    footprint = estimate_primary_footprint(living, year_built)

    # Deduct a setback band around the perimeter (approximating the lot as a square).
    side = lot ** 0.5 if lot else 0.0
    setback = STATE_ADU_BASELINE["min_side_rear_setback_ft"]
    front = rules["front_setback_ft_typical"]
    usable_side = max(0.0, side - 2 * setback)
    usable = max(0.0, usable_side * max(0.0, side - setback - front)) if lot else 0.0
    remaining = max(0.0, usable - footprint - 400)  # 400 sq ft driveway/paths allowance

    layered = resolve_rules(
        parcel, jurisdiction=jurisdiction, residential=residential,
        lot_area_sqft=lot, remaining_sqft=remaining, as_of=as_of,
    )
    numbers = layered.numbers
    max_detached = numbers["detached_cap_sqft"]
    by_right = numbers["guaranteed_adu_sqft"]

    if residential is False:
        feasibility, score = "unlikely", 10
    elif lot == 0:
        feasibility, score = "unknown", 0
    elif remaining >= max_detached * 1.5:
        feasibility, score = "strong", 90
    elif remaining >= max_detached:
        feasibility, score = "good", 75
    elif remaining >= by_right:
        feasibility, score = "possible", 55
    else:
        # State law still guarantees an 800 sq ft ADU on residential lots; it may need to be attached or a conversion.
        feasibility, score = "constrained", 35 if residential else 20

    caveats = list(rules["notes"])
    if jurisdiction == "unknown":
        caveats.insert(0, "Jurisdiction unresolved — only the state-law floor applied.")
    if residential is None:
        caveats.append("Zoning code missing; residential status unverified.")
    two_fit = bool(residential is not False and remaining >= 2 * by_right)
    if two_fit and numbers["max_detached_units"] == 2:
        caveats.append(f"There's room on paper for two detached ADUs of {by_right:,} sq ft each.")
    elif two_fit:
        caveats.append(f"From Jan 1, 2027 there may be room for two detached ADUs of {by_right:,} sq ft each.")

    return {
        "rules_version": RULES_VERSION,
        "jurisdiction": jurisdiction,
        "jurisdiction_name": rules["name"],
        "ordinance": rules["ordinance"],
        "zoning_code": zoning_code,
        "residential": residential,
        "lot_area_sqft": lot or None,
        "estimated_primary_footprint_sqft": round(footprint),
        "estimated_buildable_remaining_sqft": round(remaining),
        "adu": {
            "allowed_by_state_floor": residential is not False,
            "by_right_min_sqft": by_right,
            "max_detached_sqft": max_detached,
            "max_detached_units": numbers["max_detached_units"],
            "two_detached_fit": two_fit,
            "max_attached_pct_of_primary": rules["adu_max_attached_pct_of_primary"],
            "side_rear_setback_ft": setback,
            "front_setback_ft_typical": front,
            "jadu_max_sqft": numbers["jadu_max_sqft"],
            "min_lot_size_required": STATE_ADU_BASELINE["min_lot_size_requirement_allowed"],
        },
        "feasibility": feasibility,
        "feasibility_score": score,
        "caveats": caveats,
        "verify": [STATE_ADU_BASELINE["verify"], rules["verify"]],
        "rule_layers": layered.as_dict(),
    }
