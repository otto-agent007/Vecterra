"""Layered land-use rules for ADU guidance: who sets each rule, and who wins a conflict.

Every parcel answers to these layers, in this order of precedence:

1. **Federal law** — fair housing. Enforced by the compliance firewall, not here.
2. **State law** — floors local rules can't go below (an 800 sq ft ADU), ceilings
   they can't exceed (4 ft side/rear setbacks), and local or HOA restrictions it
   voids outright (sprinkler mandates, HOA bans). State law wins any conflict.
3. **One local land-use authority** — the County of San Diego for unincorporated
   parcels *or* the City of Escondido inside city limits, never both. Its rules
   apply wherever state law leaves room.
4. **Overlays that stack** — the fire authority, County Environmental Health
   (septic) and any HOA. Owners must satisfy all of them together, except where
   state law voids a particular requirement.

Each rule carries an effective date (results depend on ``as_of``) and a status:

* ``settled``     — current law, sourced.
* ``verify``      — sourced but needs confirming with the agency (final text not
  reviewed, or sources disagree).
* ``unsettled``   — the agencies themselves disagree, or the rule was withdrawn
  for revision. Vecterra never resolves these; it says so.
* ``upcoming``    — enacted, takes effect after ``as_of``.
* ``not_checked`` — depends on a parcel fact we haven't loaded yet.

Parcel overlay fields read here are optional and populated by ingestion:
``fire_hazard_zone`` ("Very High" | "High" | "Moderate" | "None"),
``fire_responsibility`` ("SRA" | "LRA"), ``fire_agency`` (display name),
``sewer_service`` ("sewer" | "septic") and ``hoa`` (True/False or the association name).

Sources are recorded per rule; the research behind them is in the Vecterra
project docs (research/*.md). This is a working model of the law, not legal advice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

LAYERS_VERSION = "rule-layers-v1"

# Layer keys, in precedence order, with the homeowner-facing group label.
LAYER_ORDER = ("state", "local", "fire", "septic", "hoa")

AB956_EFFECTIVE = date(2027, 1, 1)
SB543_AB1154_EFFECTIVE = date(2026, 1, 1)
ENERGY_CODE_2025_EFFECTIVE = date(2026, 1, 1)
UPCOMING_WINDOW_DAYS = 456  # show enacted changes up to ~15 months ahead

# State ADU law (Gov. Code §§ 66310–66342, formerly § 65852.2).
STATE_FLOOR = {
    "guaranteed_adu_sqft": 800,            # can't be denied on a residential lot
    "guaranteed_height_ft": 16,            # locals must allow at least this
    "max_side_rear_setback_ft": 4,         # locals can't require more for an ADU
    "local_cap_floor_1br_sqft": 850,       # a local size cap can't be lower than this (studio/1 bedroom)
    "local_cap_floor_2br_sqft": 1000,      # ...or this (2+ bedrooms)
    "default_detached_cap_sqft": 1200,     # applies when no valid local cap exists
    "jadu_max_sqft": 500,
    "impact_fee_free_max_sqft": 750,
}

SRC = {
    "state_adu": ("California ADU law, Gov. Code §§ 66310–66342 (HCD ADU Handbook)",
                  "https://www.hcd.ca.gov/policy-and-research/accessory-dwelling-units"),
    "ab956": ("AB 956 (2026), signed Sep 29, 2026; effective Jan 1, 2027",
              "https://leginfo.legislature.ca.gov/faces/billNavClient.xhtml?bill_id=202520260AB956"),
    "sb543_ab1154": ("SB 543 and AB 1154 (2025), effective Jan 1, 2026",
                     "https://bbklaw.com/resources/la-110725-governor-newsom-signs-four-new-accessory-dwelling-unit-bills"),
    "civ4751": ("Civil Code § 4751", "https://california.public.law/codes/civil_code_section_4751"),
    "chapter7a": ("California Building Code ch. 7A / Residential Code § R337 (wildland-urban interface)",
                  "https://osfm.fire.ca.gov/what-we-do/community-wildfire-preparedness-and-mitigation/fire-hazard-severity-zones"),
    "civ1102_19": ("Civil Code §§ 1102.19, 1102.6f", "https://california.public.law/codes/civil_code_section_1102.19"),
    "zone0": ("Board of Forestry Zone 0 package withdrawn from OAL review Sep 8, 2026",
              "https://cadefensiblespace.com/rule-status/"),
    "energy2025": ("2025 California Energy Code, ADU guide (CEC)",
                   "https://www.energy.ca.gov/sites/default/files/2026-04/2025_Energy_Code_Accessory_Dwelling_Units_%28ADU%29_ADA.pdf"),
    "hcd_malibu": ("HCD ADU findings letter to Malibu (Nov 2024)",
                   "https://www.hcd.ca.gov/sites/default/files/docs/policy-and-research/ordinance-review-letters/malibu-adu-findings-11132024.pdf"),
    "deh_septic": ("County DEH ADU / second dwelling FAQ",
                   "https://www.sandiegocounty.gov/content/dam/sdc/deh/lwqd/ADU%20Second%20Dwellings%20FAQ%20Revised%20020121.pdf"),
    "county_adu": ("County of San Diego PDS ADU page", "https://www.sandiegocounty.gov/content/sdc/pds/bldg/adu.html"),
    "county_pds611": ("County of San Diego ADU handout PDS-611 (Rev. 3/11/2022) and PDS ADU page",
                      "https://sandiegocounty.gov/content/dam/sdc/pds/zoning/PDS-PLN-611.pdf"),
    "esc_33_1474": ("Escondido Zoning Code § 33-1474, Table 33-1474 (Ord. 2023-15)",
                    "https://gocodebook.com/library/us/ca/escondido-zoning/division-9-zoning-administrator/article-70-accessory-dwelling-units-and-junior-accessory-dwelling-units/33-1474.-development-standards"),
    "county_sb9": ("County of San Diego SB 9 page", "https://www.sandiegocounty.gov/content/sdc/pds/zoninghome/sb9home.html"),
    "hcd_burbank": ("HCD technical assistance letter to Burbank (Sep 2025)",
                    "https://www.hcd.ca.gov/sites/default/files/docs/planning-and-community/HAU/burbank-hau-1904-ta-091525.pdf"),
    "county_ab1033": ("County Planning Commission ADU ordinance amendment (Jun 2026)",
                      "https://www.sandiegocounty.gov/content/dam/sdc/pds/PC/260612-Supporting-Documentation/Item%205%20ADU%20Ordinance%20Amendment.pdf"),
    "esc_str": ("City of Escondido short-term rental ordinance (effective Jul 1, 2025)",
                "https://escondido.gov/1215/Short-Term-Rentals"),
    "esc_paadu": ("City of Escondido pre-approved ADU plans", "https://www.escondido.gov/1207"),
}

# Local land-use authorities. Exactly one applies to a parcel.
LOCAL_AUTHORITIES: Dict[str, Dict[str, Any]] = {
    "san_diego_county": {
        "name": "County of San Diego",
        "detached_cap_sqft": 1200,
        "detached_cap_status": "settled",
        "detached_cap_note": None,
        "detached_cap_source": "county_pds611",
        "detached_height_ft": 24,
        "small_lot_cap": None,
        "ab956_ordinance_updated": False,   # County page doesn't reflect AB 956 yet (Oct 2026)
        "states_state_law_prevails": True,  # County ADU page: state law controls where they conflict
        "preapproved_plans": None,
        "adu_short_term_rental_banned": False,
        "adu_condo_sale": "opted_in_pending_conditions",  # AB 1033 opt-in Mar 4, 2026
        "sb9_excludes_high_fire": True,
    },
    "city_of_escondido": {
        "name": "City of Escondido",
        "detached_cap_sqft": 1000,
        "detached_cap_status": "settled",
        "detached_cap_note": None,
        "detached_cap_source": "esc_33_1474",
        "detached_height_ft": None,  # zone limit applies; state law guarantees 16 ft
        # Table 33-1474: lots under 20,000 sq ft cap a 0-1 bedroom ADU at 850 sq ft.
        "small_lot_cap": {"lot_under_sqft": 20000, "one_bedroom_cap_sqft": 850},
        "ab956_ordinance_updated": False,
        "states_state_law_prevails": False,
        "preapproved_plans": (484, 644, 851, 1000),
        "adu_short_term_rental_banned": True,
        "adu_condo_sale": "not_opted_in",
        "sb9_excludes_high_fire": None,  # not researched
    },
}

HIGH_FIRE = {"very high", "high"}


@dataclass
class ResolvedRule:
    key: str
    layer: str               # state | local | fire | septic | hoa
    authority: str           # who sets it, in plain words
    statement: str           # homeowner-facing sentence
    status: str              # settled | verify | unsettled | upcoming | not_checked
    source: Optional[str] = None
    url: Optional[str] = None
    effective_from: Optional[str] = None
    confirm_with: Optional[str] = None


@dataclass
class Conflict:
    key: str
    higher: str              # the rule that wins (or "unresolved")
    lower: str               # the rule it overrides / disagrees with
    resolution: str          # plain-language outcome
    status: str              # resolved | unsettled | sources_disagree


@dataclass
class LayeredResult:
    as_of: str
    land_use_authority: str
    numbers: Dict[str, Any]
    rules: List[ResolvedRule] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    authorities: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "layers_version": LAYERS_VERSION,
            "as_of": self.as_of,
            "land_use_authority": self.land_use_authority,
            "numbers": self.numbers,
            "rules": [asdict(r) for r in self.rules],
            "conflicts": [asdict(c) for c in self.conflicts],
            "authorities": self.authorities,
        }


def _src(key: str) -> Dict[str, Optional[str]]:
    label, url = SRC[key]
    return {"source": label, "url": url}


def _norm(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().lower()
    return s or None


def _upcoming(as_of: date, effective: date) -> bool:
    return as_of < effective and (effective - as_of).days <= UPCOMING_WINDOW_DAYS


def resolve_numbers(local: Optional[Dict[str, Any]], as_of: date) -> tuple[Dict[str, Any], List[Conflict]]:
    """Resolve the numeric ADU limits, applying state floors over local values."""
    conflicts: List[Conflict] = []
    cap = STATE_FLOOR["default_detached_cap_sqft"]
    cap_status = "settled"
    if local and local.get("detached_cap_sqft"):
        local_cap = int(local["detached_cap_sqft"])
        floor_cap = STATE_FLOOR["local_cap_floor_1br_sqft"]
        if local_cap < floor_cap:
            conflicts.append(Conflict(
                key="detached_cap",
                higher=f"State law: a local size cap can't be below {floor_cap:,} sq ft (or {STATE_FLOOR['local_cap_floor_2br_sqft']:,} for 2+ bedrooms)",
                lower=f"{local['name']} cap of {local_cap:,} sq ft",
                resolution=f"State minimum applies: at least {floor_cap:,} sq ft, {STATE_FLOOR['local_cap_floor_2br_sqft']:,} for 2+ bedrooms",
                status="resolved",
            ))
            cap = STATE_FLOOR["local_cap_floor_2br_sqft"]
        else:
            cap = local_cap
        cap_status = local.get("detached_cap_status") or "verify"
        if local.get("detached_cap_note"):
            conflicts.append(Conflict(
                key="detached_cap_sources",
                higher=f"{local['name']} materials",
                lower=local["detached_cap_note"],
                resolution=f"Using {cap:,} sq ft until confirmed with {local['name']}",
                status="sources_disagree",
            ))

    two_detached = as_of >= AB956_EFFECTIVE
    numbers = {
        "guaranteed_adu_sqft": STATE_FLOOR["guaranteed_adu_sqft"],
        "guaranteed_height_ft": STATE_FLOOR["guaranteed_height_ft"],
        "max_side_rear_setback_ft": STATE_FLOOR["max_side_rear_setback_ft"],
        "detached_cap_sqft": cap,
        "detached_cap_status": cap_status,
        "max_detached_units": 2 if two_detached else 1,
        "jadu_max_sqft": STATE_FLOOR["jadu_max_sqft"],
        "jadu_required_alongside_two_detached": False if two_detached else None,
        "impact_fee_free_max_sqft": STATE_FLOOR["impact_fee_free_max_sqft"],
    }
    if two_detached and local and not local.get("ab956_ordinance_updated"):
        conflicts.append(Conflict(
            key="detached_units",
            higher="State law (AB 956): two detached ADUs on a single-family lot from Jan 1, 2027",
            lower=f"{local['name']} ordinance not yet updated (allows one)",
            resolution="State law applies; the local ordinance is void where it conflicts",
            status="resolved",
        ))
    return numbers, conflicts


def resolve_rules(
    parcel: Dict[str, Any],
    *,
    jurisdiction: str,
    residential: Optional[bool],
    lot_area_sqft: float,
    remaining_sqft: float,
    as_of: Optional[date] = None,
    local_override: Optional[Dict[str, Any]] = None,
) -> LayeredResult:
    """Resolve every layer for one parcel and record who sets each rule."""
    as_of = as_of or date.today()
    local = local_override if local_override is not None else LOCAL_AUTHORITIES.get(jurisdiction)
    local_name = local["name"] if local else "Local authority not yet resolved"

    numbers, conflicts = resolve_numbers(local, as_of)
    rules: List[ResolvedRule] = []
    add = rules.append

    # ---- State law ---------------------------------------------------------------
    if residential is not False:
        add(ResolvedRule(
            key="adu_guaranteed", layer="state", authority="State law", status="settled",
            statement=(f"Guarantees room for at least an {numbers['guaranteed_adu_sqft']:,} sq ft ADU, up to "
                       f"{numbers['guaranteed_height_ft']} ft tall, with side and rear setbacks of no more than "
                       f"{numbers['max_side_rear_setback_ft']} ft."),
            **_src("state_adu"),
        ))
        add(ResolvedRule(
            key="adu_no_extra_conditions", layer="state", authority="State law", status="settled",
            statement=("No minimum lot size, no requirement that the owner live on site for an ADU, and no "
                       "replacement parking when a garage is converted."),
            **_src("state_adu"),
        ))
        if as_of >= AB956_EFFECTIVE:
            add(ResolvedRule(
                key="two_detached", layer="state", authority="State law", status="verify",
                statement="Allows two detached ADUs on a single-family lot; each can be limited to 800 sq ft.",
                effective_from=AB956_EFFECTIVE.isoformat(), confirm_with=local_name, **_src("ab956"),
            ))
        elif _upcoming(as_of, AB956_EFFECTIVE):
            add(ResolvedRule(
                key="two_detached", layer="state", authority="State law", status="upcoming",
                statement="Starting Jan 1, 2027, allows two detached ADUs on a single-family lot instead of one.",
                effective_from=AB956_EFFECTIVE.isoformat(), **_src("ab956"),
            ))
        jadu = f"Allows a junior ADU of up to {numbers['jadu_max_sqft']} sq ft inside the house."
        if as_of >= SB543_AB1154_EFFECTIVE:
            jadu += " The owner must live on site only if it shares a bathroom with the main house."
        else:
            jadu += " The owner must live on site."
        if as_of >= AB956_EFFECTIVE:
            jadu += " With two detached ADUs, a junior ADU is at the local authority's discretion."
        add(ResolvedRule(key="jadu", layer="state", authority="State law",
                         status="verify" if as_of >= AB956_EFFECTIVE else "settled",
                         statement=jadu, **_src("sb543_ab1154" if as_of >= SB543_AB1154_EFFECTIVE else "state_adu")))
        add(ResolvedRule(
            key="impact_fees", layer="state", authority="State law", status="settled",
            statement=(f"No impact fees on an ADU of {numbers['impact_fee_free_max_sqft']} sq ft or less; above that, "
                       "fees must be in proportion to the size of the main house."),
            **_src("sb543_ab1154" if as_of >= SB543_AB1154_EFFECTIVE else "state_adu"),
        ))
        add(ResolvedRule(
            key="sprinklers", layer="state", authority="State law", status="settled",
            statement="Fire sprinklers can't be required in an ADU unless the main house is required to have them.",
            **_src("state_adu"),
        ))
        if as_of >= ENERGY_CODE_2025_EFFECTIVE:
            add(ResolvedRule(
                key="energy_code", layer="state", authority="State energy code", status="verify",
                statement="A new detached ADU generally needs rooftop solar under the 2025 energy code, with some exceptions.",
                effective_from=ENERGY_CODE_2025_EFFECTIVE.isoformat(), confirm_with=local_name, **_src("energy2025"),
            ))

    # ---- Local land-use authority (exactly one) -------------------------------------
    if local is None:
        add(ResolvedRule(
            key="local_authority", layer="local", authority="Local rules", status="not_checked",
            statement=("We haven't confirmed whether this lot is in the City of Escondido or unincorporated County, "
                       "so only state law is applied."),
        ))
    elif residential is not False:
        cap_statement = f"Caps a detached ADU at {numbers['detached_cap_sqft']:,} sq ft"
        small = local.get("small_lot_cap")
        if small and lot_area_sqft and lot_area_sqft < small["lot_under_sqft"]:
            cap_statement += (f" ({small['one_bedroom_cap_sqft']:,} sq ft for one bedroom or less, because this lot is under "
                              f"{small['lot_under_sqft']:,} sq ft)")
        if local.get("detached_height_ft"):
            cap_statement += f" and allows it up to {local['detached_height_ft']} ft tall"
        cap_statement += "."
        if local.get("detached_cap_note"):
            cap_statement += f" ({local['detached_cap_note']}.)"
        add(ResolvedRule(
            key="detached_cap", layer="local", authority=local_name, status=numbers["detached_cap_status"],
            statement=cap_statement, confirm_with=local_name, **_src(local["detached_cap_source"]),
        ))
        if local.get("preapproved_plans"):
            sizes = ", ".join(f"{s:,}" for s in local["preapproved_plans"][:-1]) + f" and {local['preapproved_plans'][-1]:,}"
            add(ResolvedRule(
                key="preapproved_plans", layer="local", authority=local_name, status="settled",
                statement=f"Offers pre-approved ADU plans of {sizes} sq ft.", **_src("esc_paadu"),
            ))
        if local.get("adu_short_term_rental_banned"):
            add(ResolvedRule(
                key="adu_short_term_rental", layer="local", authority=local_name, status="settled",
                statement="Doesn't allow an ADU or junior ADU to be rented for stays under 30 days; longer rentals are fine.",
                **_src("esc_str"),
            ))
        if local.get("adu_condo_sale") == "opted_in_pending_conditions":
            add(ResolvedRule(
                key="adu_condo_sale", layer="local", authority=local_name, status="unsettled",
                statement=("Has opted in to letting owners sell an ADU separately as a condominium; the added "
                           "conditions are still being finalized."),
                effective_from="2026-03-04", confirm_with=local_name, **_src("county_ab1033"),
            ))
        elif local.get("adu_condo_sale") == "not_opted_in":
            add(ResolvedRule(
                key="adu_condo_sale", layer="local", authority=local_name, status="verify",
                statement="Hasn't opted in to letting owners sell an ADU separately, so that option isn't available here.",
                confirm_with=local_name,
            ))
        if as_of >= AB956_EFFECTIVE and not local.get("ab956_ordinance_updated"):
            note = " Its ADU page says state law controls where they conflict." if local.get("states_state_law_prevails") else ""
            add(ResolvedRule(
                key="local_update_pending", layer="local", authority=local_name, status="verify",
                statement=f"Hasn't updated its ADU rules for the two-ADU law yet; state law applies where they differ.{note}",
                confirm_with=local_name, **_src("ab956"),
            ))

    # ---- Fire overlay (stacks) ------------------------------------------------------
    zone = _norm(parcel.get("fire_hazard_zone"))
    resp = (parcel.get("fire_responsibility") or "").strip().upper() or None
    fire_agency = parcel.get("fire_agency") or "The local fire authority"
    if zone is None:
        add(ResolvedRule(
            key="fire_zone", layer="fire", authority="Fire hazard map", status="not_checked",
            statement="We haven't loaded this lot's fire hazard zone yet.",
        ))
    else:
        if zone in HIGH_FIRE:
            label = "Very High" if zone == "very high" else "High"
            add(ResolvedRule(
                key="fire_construction", layer="fire", authority="State building code", status="settled",
                statement=(f"This lot is in a {label} fire hazard zone, so new construction, including an ADU, must meet "
                           "the state's ignition-resistant building standards."),
                **_src("chapter7a"),
            ))
            add(ResolvedRule(
                key="fire_sale_disclosure", layer="fire", authority="State law", status="settled",
                statement="If the home is sold, the fire zone must be disclosed and a defensible-space inspection documented.",
                **_src("civ1102_19"),
            ))
            if local and local.get("sb9_excludes_high_fire"):
                add(ResolvedRule(
                    key="sb9_fire", layer="local", authority=local_name, status="unsettled",
                    statement=("Doesn't allow SB 9 lot splits on lots touching High or Very High fire zones. The state "
                               "housing department has taken a different view, so this is unsettled."),
                    confirm_with=local_name, **_src("county_sb9"),
                ))
                conflicts.append(Conflict(
                    key="sb9_fire",
                    higher="unresolved",
                    lower=f"{local_name} excludes High/Very High fire-zone parcels; HCD says fire-hardened parcels can qualify",
                    resolution="Unsettled until a court or HCD enforcement decides; Vecterra reports it as unsettled",
                    status="unsettled",
                ))
        if zone == "very high" or resp == "SRA":
            add(ResolvedRule(
                key="zone0", layer="fire", authority="State Board of Forestry", status="unsettled",
                statement=("Rules for a 5 ft ember-resistant zone around homes (Zone 0) were withdrawn for revision in "
                           "September 2026. They're recommended, not yet required."),
                **_src("zone0"),
            ))
    if residential is not False:
        add(ResolvedRule(
            key="fire_access", layer="fire", authority=fire_agency, status="verify",
            statement=("Sets fire access and water-supply requirements for new buildings, but can't add ADU rules "
                       "beyond what state law allows."),
            confirm_with=fire_agency if parcel.get("fire_agency") else None, **_src("hcd_malibu"),
        ))

    # ---- Septic / sewer overlay (stacks) ------------------------------------------------
    sewer = _norm(parcel.get("sewer_service"))
    if residential is not False:
        if sewer == "septic":
            add(ResolvedRule(
                key="septic", layer="septic", authority="County Environmental Health", status="settled",
                statement=("Must approve an ADU on septic: a licensed layout, percolation tests and a reserve area. Adding "
                           "an ADU usually means enlarging the system, and review takes about 60 to 90 business days."),
                **_src("deh_septic"),
            ))
        elif sewer == "sewer":
            add(ResolvedRule(
                key="sewer", layer="septic", authority="Sewer agency", status="settled",
                statement="Sewer connection fees for an ADU must be in proportion to its size or plumbing fixtures.",
                **_src("state_adu"),
            ))
        else:
            likely = lot_area_sqft and lot_area_sqft > 43560 * 0.9
            add(ResolvedRule(
                key="septic", layer="septic", authority="County Environmental Health", status="not_checked",
                statement=("This lot is likely on septic, which may limit ADU size; we haven't confirmed it yet."
                           if likely else "We haven't confirmed whether this lot is on sewer or septic."),
            ))

    # ---- HOA overlay (stacks; state law voids bans) ---------------------------------------
    hoa = parcel.get("hoa")
    if residential is not False:
        if hoa:
            hoa_name = hoa if isinstance(hoa, str) else "The HOA"
            add(ResolvedRule(
                key="hoa_review", layer="hoa", authority=hoa_name, status="settled",
                statement=("Can review an ADU's design alongside the permit, but can't ban it or impose unreasonable "
                           "restrictions."),
                **_src("civ4751"),
            ))
        elif hoa is None:
            add(ResolvedRule(
                key="hoa_review", layer="hoa", authority="HOA", status="not_checked",
                statement=("We haven't confirmed whether an HOA covers this lot. If one does, it can review an ADU's design "
                           "but can't ban it."),
                **_src("civ4751"),
            ))

    authorities = ["State law", local_name]
    for r in rules:
        if r.layer in ("fire", "septic", "hoa") and r.status != "not_checked" and r.authority not in authorities:
            authorities.append(r.authority)

    rules.sort(key=lambda r: LAYER_ORDER.index(r.layer) if r.layer in LAYER_ORDER else len(LAYER_ORDER))
    return LayeredResult(
        as_of=as_of.isoformat(),
        land_use_authority=local_name,
        numbers=numbers,
        rules=rules,
        conflicts=conflicts,
        authorities=authorities,
    )


def homeowner_groups(rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group resolved rules for display: what's in force, what's coming, what's unsettled, what's unchecked."""
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    def bucket(title: str) -> Dict[str, Any]:
        if title not in groups:
            groups[title] = {"title": title, "items": []}
            order.append(title)
        return groups[title]

    for r in rules:
        st = r.get("status")
        if st == "upcoming":
            title = "Coming up"
        elif st == "unsettled":
            title = "Not settled yet"
        elif st == "not_checked":
            title = "Still to check"
        elif r.get("layer") == "state":
            title = "State law"
        elif r.get("layer") == "local":
            title = r.get("authority") or "Local rules"
        else:
            title = "Also applies"
        item = dict(r)
        # Name who sets the rule unless the group title already does.
        item["lead"] = r.get("authority") if title not in ("State law", r.get("authority")) else None
        item.setdefault("confirm_with", None)
        bucket(title)["items"].append(item)

    rank = {"State law": 0, "Also applies": 2, "Coming up": 3, "Not settled yet": 4, "Still to check": 5}
    return sorted((groups[t] for t in order), key=lambda g: rank.get(g["title"], 1))
