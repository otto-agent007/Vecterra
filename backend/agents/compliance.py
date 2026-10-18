"""Deterministic fair-housing compliance firewall.

No LLM is involved here on purpose: every verdict must be reproducible and
auditable. The firewall does three things:

1. ``screen_text``            — scans generated text for protected-class
                                 references, steering language, and
                                 demographic targeting (federal Fair Housing
                                 Act + California FEHA / Unruh categories).
                                 "block" findings are redacted; "warn" findings
                                 are logged for human review.
2. ``validate_lead_rationale`` — guarantees that the *reason* a homeowner is
                                 being contacted is expressed only in terms of
                                 whitelisted property / financial factors.
3. ``ensure_disclaimers``      — appends the Equal Housing statement and the
                                 "estimates, not appraisals" disclaimer when
                                 absent.

Rule tuning note: color words like "white"/"black" legitimately describe paint
and roofing, so race rules only fire when the word is tied to people, buyers,
or neighborhoods (see ``_PEOPLE``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Pattern, Tuple

FIREWALL_VERSION = "fha-feha-rules-v1.0"

# Word group reused by several rules so that "white trim" passes but
# "white neighborhood" does not.
_PEOPLE = r"(?:people|person|persons|folks|families|family|buyers?|owners?|residents?|neighbou?rs?|neighbou?rhoods?|communit(?:y|ies)|areas?|crowd|clientele|tenants?|occupants?)"

_RULES: List[Tuple[str, str, str, List[str]]] = [
    # (rule_id, severity, explanation, patterns)
    (
        "FHA_RACE_COLOR",
        "block",
        "Reference to race or color of people/neighborhood (FHA §3604).",
        [
            rf"\b(?:white|black|asian|hispanic|latin[oax]s?|caucasian|african[- ]american|chinese|mexican|filipino|indian|arab|native[- ]american|minority|ethnic)\s+{_PEOPLE}\b",
            r"\b(?:racial(?:ly)?|ethnicit(?:y|ies)|ethnic\s+(?:mix|makeup|background))\b",
            rf"\b{_PEOPLE}\s+(?:of\s+colou?r|who\s+are\s+(?:white|black|asian|hispanic|latin[oax]))\b",
        ],
    ),
    (
        "FHA_NATIONAL_ORIGIN",
        "block",
        "Reference to national origin, ancestry, citizenship, or immigration status (FHA; CA FEHA).",
        [
            r"\b(?:national\s+origin|ancestry|immigra(?:nt|tion)|citizenship|undocumented|foreign(?:er|-born)?|non-?native\s+speakers?|english[- ]only)\b",
        ],
    ),
    (
        "FHA_RELIGION",
        "block",
        "Reference to religion or religious affiliation (FHA §3604).",
        [
            r"\b(?:christian|catholic|protestant|jewish|muslim|islamic|hindu|buddhist|mormon|lds|evangelical|religious|church-?going|god-?fearing|faith(?:ful|-based)?)\b",
            r"\b(?:near|close\s+to|walk(?:ing)?\s+(?:distance\s+)?to)\s+(?:the\s+)?(?:church|temple|mosque|synagogue|parish)\b",
        ],
    ),
    (
        "FHA_SEX_GENDER",
        "block",
        "Reference to sex, gender identity, or sexual orientation (FHA; CA FEHA).",
        [
            r"\b(?:bachelor\s+pad|bachelorette|ideal\s+for\s+(?:a\s+)?(?:single\s+)?(?:man|men|woman|women|guys?|gals?|ladies|gentlemen))\b",
            r"\b(?:gay|lesbian|lgbtq?\+?|transgender|trans\s+people|straight\s+couples?|sexual\s+orientation|gender\s+identity)\b",
        ],
    ),
    (
        "FHA_FAMILIAL_STATUS",
        "block",
        "Preference or limitation based on familial status (FHA §3604; CA FEHA marital status).",
        [
            r"\b(?:family[- ]friendly|perfect\s+for\s+(?:a\s+)?famil(?:y|ies)|great\s+for\s+(?:a\s+)?famil(?:y|ies)|growing\s+famil(?:y|ies)|young\s+famil(?:y|ies))\b",
            r"\b(?:no\s+(?:kids|children)|adults?[- ]only|childless|empty[- ]nesters?|singles?\s+only|couples?\s+only|newlyweds?|young\s+(?:couples?|professionals?)|mature\s+(?:persons?|adults?|couples?))\b",
            r"\b(?:married|unmarried|divorc(?:e|ed|ing)|widow(?:ed|er)?|single\s+(?:parent|mother|father|mom|dad))\b",
        ],
    ),
    (
        "FHA_DISABILITY",
        "block",
        "Reference to disability or health condition of people (FHA §3604; CA FEHA).",
        [
            r"\b(?:handicap(?:ped)?|disabled|disabilit(?:y|ies)|crippled|mentally\s+ill|mental\s+(?:health|illness)|drug\s+(?:user|addict)s?|alcoholics?|sober\s+living|able-?bodied|healthy\s+(?:adults?|people))\b",
            r"\bno\s+(?:wheelchairs?|service\s+animals?|emotional\s+support\s+animals?)\b",
        ],
    ),
    (
        "FHA_DISABILITY_FEATURE",
        "warn",
        "Accessibility feature mentioned. Describing a feature is permitted; implying who it is for is not.",
        [r"\b(?:wheelchair|service\s+animals?|emotional\s+support\s+animals?|ada[- ]compliant|accessible\s+(?:entry|bathroom|ramp))\b"],
    ),
    (
        "FEHA_AGE",
        "block",
        "Age-based preference or targeting (CA FEHA / Unruh; HUD guidance).",
        [
            r"\b(?:senior|seniors|elderly|retirees?|retirement-aged|older\s+(?:adults?|owners?|people|homeowners?)|aging\s+(?:owners?|homeowners?|in\s+place)|young\s+(?:buyers?|owners?|people)|millennials?|boomers?|gen[- ]?[xz])\b",
            rf"\b{_PEOPLE}\s+(?:over|under|aged|age)\s+\d{{2}}\b",
            r"\b\d{2}\+\s+(?:community|living|only|adults?)\b",
        ],
    ),
    (
        "FEHA_SOURCE_OF_INCOME",
        "block",
        "Source-of-income or public-assistance reference (CA FEHA).",
        [
            r"\b(?:section\s+8|housing\s+vouchers?|no\s+vouchers?|public\s+assistance|welfare|ssi|disability\s+income|source\s+of\s+income)\b",
        ],
    ),
    (
        "FEHA_MILITARY_STATUS",
        "warn",
        "Military or veteran status reference (CA FEHA). Allowed only in a neutral, non-targeting context.",
        [r"\b(?:veterans?|military|active[- ]duty|service\s+members?)\b"],
    ),
    (
        "STEERING_NEIGHBORHOOD",
        "block",
        "Subjective neighborhood characterization that can function as steering (HUD guidance).",
        [
            r"\b(?:safe|unsafe|dangerous|high[- ]crime|low[- ]crime|crime[- ]free)\s+(?:neighbou?rhoods?|areas?|streets?|communit(?:y|ies)|parts?\s+of\s+town)\b",
            r"\b(?:good|great|excellent|top|best|bad|poor|failing)\s+schools?\b",
            r"\b(?:exclusive|prestigious|desirable|undesirable|integrated|changing|transitional|up[- ]and[- ]coming|gentrif(?:ying|ied))\s+(?:neighbou?rhoods?|areas?|communit(?:y|ies)|enclaves?)\b",
            r"\b(?:the\s+right\s+kind\s+of\s+(?:people|neighbou?rs?)|people\s+like\s+you|your\s+kind)\b",
        ],
    ),
    (
        "DISTRESS_TARGETING",
        "warn",
        "Language implying the owner is being targeted for personal hardship; review for predatory framing.",
        [
            r"\b(?:foreclos(?:ure|ed|ing)|pre-?foreclosure|probate|estate\s+sale|inherit(?:ed|ance)|behind\s+on\s+(?:payments|taxes)|tax\s+lien|hardship|bankrupt(?:cy)?|passed\s+away|deceased)\b",
        ],
    ),
    (
        "OCCUPANT_INFERENCE",
        "block",
        "Inference about who lives at the property; dossiers describe the structure only.",
        [
            rf"\b(?:the\s+)?{_PEOPLE}\s+(?:appear|appears|seem|seems|look|looks|must\s+be|are\s+likely|is\s+likely)\b",
            r"\b(?:children'?s?\s+toys?|strollers?|religious\s+(?:symbols?|statues?|iconography)|menorah|nativity\s+scene|prayer\s+(?:flags?|rugs?))\b",
        ],
    ),
]

_COMPILED: List[Tuple[str, str, str, Pattern[str]]] = [
    (rule_id, severity, explanation, re.compile(p, re.IGNORECASE))
    for rule_id, severity, explanation, patterns in _RULES
    for p in patterns
]

# Factors that may appear in a lead rationale. Everything is about the parcel,
# its condition, its public-record financials, or zoning — never about people.
ALLOWED_RATIONALE_KEYS = frozenset(
    {
        "estimated_equity",
        "equity_confidence",
        "estimated_market_value",
        "assessed_total_value",
        "assessed_vs_market_gap",
        "years_since_last_sale",
        "last_sale_date",
        "owner_occupied",
        "lot_area_sqft",
        "living_area_sqft",
        "year_built",
        "zoning_code",
        "jurisdiction",
        "adu_feasibility",
        "adu_feasibility_score",
        "roof_score",
        "paint_score",
        "structure_score",
        "overall_health_score",
        "deferred_maintenance_flags",
        "scan_date",
    }
)

EQUAL_HOUSING_STATEMENT = (
    "Equal Housing Opportunity. This outreach is based solely on publicly available "
    "property records and exterior condition observations. It does not consider, and "
    "no decision is made on the basis of, race, color, religion, sex, gender identity, "
    "sexual orientation, disability, familial status, marital status, national origin, "
    "ancestry, age, source of income, or any other characteristic protected by federal "
    "or California law."
)

ESTIMATE_DISCLAIMER = (
    "All values, equity figures, condition scores, and zoning observations in this "
    "report are automated estimates derived from public records and photographs. They "
    "are not appraisals, inspections, or legal advice, and they may be inaccurate. "
    "Verify independently with a licensed appraiser, contractor, and the relevant "
    "planning department before relying on them."
)


@dataclass
class ComplianceResult:
    passed: bool
    text: str                                   # redacted + disclaimed output
    findings: List[Dict] = field(default_factory=list)
    disclaimers_added: List[str] = field(default_factory=list)
    firewall_version: str = FIREWALL_VERSION

    @property
    def blocked(self) -> List[Dict]:
        return [f for f in self.findings if f["severity"] == "block"]

    @property
    def warnings(self) -> List[Dict]:
        return [f for f in self.findings if f["severity"] == "warn"]

    def as_log(self) -> List[Dict]:
        return self.findings + [
            {"rule_id": "DISCLAIMER_ADDED", "severity": "info", "matched_text": d,
             "start": -1, "end": -1, "explanation": "Required disclaimer appended."}
            for d in self.disclaimers_added
        ]


def find_violations(text: str) -> List[Dict]:
    findings: List[Dict] = []
    for rule_id, severity, explanation, pattern in _COMPILED:
        for m in pattern.finditer(text):
            findings.append(
                {
                    "rule_id": rule_id,
                    "severity": severity,
                    "matched_text": m.group(0),
                    "start": m.start(),
                    "end": m.end(),
                    "explanation": explanation,
                }
            )
    findings.sort(key=lambda f: (f["start"], -f["end"]))
    return findings


def redact(text: str, findings: Iterable[Dict]) -> str:
    """Replace every 'block' span with a bracketed marker, right-to-left."""
    spans = sorted((f for f in findings if f["severity"] == "block"), key=lambda f: f["start"], reverse=True)
    out = text
    last_start = None
    for f in spans:
        if last_start is not None and f["end"] > last_start:
            continue  # overlapping span already redacted
        out = out[: f["start"]] + f"[REDACTED:{f['rule_id']}]" + out[f["end"]:]
        last_start = f["start"]
    return out


def ensure_disclaimers(text: str) -> Tuple[str, List[str]]:
    added: List[str] = []
    lower = text.lower()
    if "equal housing opportunity" not in lower:
        text = text.rstrip() + "\n\n---\n\n" + EQUAL_HOUSING_STATEMENT
        added.append("equal_housing")
    if "not appraisals" not in lower and "not an appraisal" not in lower:
        text = text.rstrip() + "\n\n" + ESTIMATE_DISCLAIMER
        added.append("estimate_disclaimer")
    return text, added


def validate_lead_rationale(rationale: Dict) -> List[Dict]:
    """Reject any rationale key that is not a whitelisted property factor."""
    findings: List[Dict] = []
    for key in rationale:
        if key not in ALLOWED_RATIONALE_KEYS:
            findings.append(
                {
                    "rule_id": "RATIONALE_NON_PROPERTY_FACTOR",
                    "severity": "block",
                    "matched_text": key,
                    "start": -1,
                    "end": -1,
                    "explanation": "Lead rationale may only cite property, condition, financial, or zoning factors.",
                }
            )
    return findings


def screen_text(text: str, *, rationale: Dict | None = None) -> ComplianceResult:
    """Full firewall pass: scan, redact, validate rationale, add disclaimers."""
    findings = find_violations(text)
    redacted = redact(text, findings)
    rationale_findings = validate_lead_rationale(rationale or {})
    final_text, added = ensure_disclaimers(redacted)
    all_findings = findings + rationale_findings
    passed = not any(f["severity"] == "block" for f in all_findings)
    return ComplianceResult(passed=passed, text=final_text, findings=all_findings, disclaimers_added=added)
