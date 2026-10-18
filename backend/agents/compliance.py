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
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Pattern, Tuple

FIREWALL_VERSION = "fha-feha-rules-v1.1"

# Word group reused by several rules so that "white trim" passes but
# "white neighborhood" does not. Deliberately excludes "area(s)" — it appears
# in legitimate structural text ("roof area appears sound").
_PEOPLE = r"(?:people|person|persons|folks|families|family|households?|buyers?|owners?|residents?|neighbou?rs?|neighbou?rhoods?|communit(?:y|ies)|streets?|blocks?|enclaves?|crowd|clientele|tenants?|occupants?|demographics?|population)"
_JOIN = r"(?:[-\s]+)"  # "Latino-owned", "Latino owned", "Latino  owned"
_STREET_SUFFIX = r"(?!\s+(?:rd|road|st|street|ave|avenue|dr|drive|ln|lane|way|blvd|boulevard|ct|court|pl|place|hwy|highway|trl|trail)\b)"

# Demonyms / group nouns that identify people by race, color, or national origin
# when used as nouns ("mostly Latinos", "Jews and Muslims nearby").
_GROUP_NOUNS = r"(?:whites|blacks|asians|hispanics|latin[oa]s?|latinx|caucasians|african[- ]americans?|mexicans?|filipinos?|chinese|koreans?|vietnamese|indians?|arabs?|jews|muslims|christians|catholics|protestants|hindus|buddhists|mormons|immigrants|foreigners|minorit(?:y|ies))"

_RULES: List[Tuple[str, str, str, List[str]]] = [
    # (rule_id, severity, explanation, patterns)
    (
        "FHA_RACE_COLOR",
        "block",
        "Reference to race or color of people/neighborhood (FHA §3604).",
        [
            rf"\b(?:white|black|asian|hispanic|latin[oax]s?|latinx|caucasian|african[- ]american|chinese|mexican|filipino|korean|vietnamese|indian|arab|native[- ]american|minority|ethnic|diverse|integrated|mixed){_JOIN}(?:owned{_JOIN})?{_PEOPLE}\b",
            r"\b(?:racial(?:ly)?|ethnicit(?:y|ies)|ethnic\s+(?:mix|makeup|background)|diversity\s+of\s+the\s+(?:neighbou?rhood|area|community))\b",
            rf"\b{_PEOPLE}\s+(?:of\s+colou?r|who\s+are\s+(?:white|black|asian|hispanic|latin[oax]))\b",
            rf"\b(?:mostly|mainly|largely|predominantly|primarily|many|few|no|some)\s+{_GROUP_NOUNS}\b",
            rf"\b{_GROUP_NOUNS}\s+(?:live|living|reside|residing|nearby|next\s+door|on\s+(?:the|this)\s+(?:street|block))\b",
        ],
    ),
    (
        "FHA_NATIONAL_ORIGIN",
        "block",
        "Reference to national origin, ancestry, citizenship, immigration status, or language (FHA; CA FEHA; HUD LEP guidance).",
        [
            r"\b(?:national\s+origin|ancestry|immigra(?:nts?|tion)|citizenship|undocumented|foreign(?:ers?|-born|\s+nationals?)|non-?native\s+speakers?|english[- ]only|non-?english)\b",
            rf"\b(?:spanish|english|chinese|mandarin|cantonese|tagalog|vietnamese|korean|arabic|russian|farsi|hindi|punjabi){_JOIN}speaking\b",
            r"\b(?:bilingual|multilingual|primary\s+language|language\s+(?:barrier|spoken))\b",
        ],
    ),
    (
        "FHA_RELIGION",
        "block",
        "Reference to religion or religious affiliation (FHA §3604).",
        [
            rf"\b(?:christian|catholic|protestant|jewish|muslim|islamic|hindu|buddhist|mormon|lds|evangelical|religious|church-?going|god-?fearing|faith-based|faith\s+communit(?:y|ies)){_STREET_SUFFIX}\b(?!\s+tea\b)",
            rf"\b(?:parish|parochial|catholic|christian|church|synagogue|mosque|temple)\s+(?:school|academy|communit(?:y|ies)|famil(?:y|ies))\b",
            r"\b(?:near|close\s+to|walk(?:ing)?\s+(?:distance\s+)?to|minutes\s+from)\s+(?:the\s+)?(?:st\.?|saint)\s+[A-Z][a-z]+(?:'s)?\b",
        ],
    ),
    (
        "FHA_RELIGION_PROXIMITY",
        "warn",
        "Proximity to a place of worship. Directional use may be fine; targeting is not.",
        [r"\b(?:near|close\s+to|walk(?:ing)?\s+(?:distance\s+)?to|minutes\s+from)\s+(?:the\s+|a\s+)?(?:church|temple|mosque|synagogue|parish|chapel)\b"],
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
            r"\b(?:family[- ]friendly|perfect\s+for\s+(?:a\s+)?famil(?:y|ies)|great\s+for\s+(?:a\s+)?famil(?:y|ies)|ideal\s+for\s+(?:a\s+)?famil(?:y|ies)|growing\s+famil(?:y|ies)|young\s+famil(?:y|ies)|starter\s+home)\b",
            r"\b(?:no\s+(?:kids|children)|adults?[- ]only|childless|empty[- ]nesters?|singles?\s+only|couples?\s+only|newlyweds?|young\s+(?:couples?|professionals?)|mature\s+(?:persons?|adults?|couples?))\b",
            r"\b(?:married|unmarried|divorc(?:e|ed|ing)|widow(?:ed|er)?(?!'s\s+walk)|single\s+(?:parent|mother|father|mom|dad))\b",
            r"\b(?:small\s+)?(?:kids|children|toddlers|teenagers|teens)\b",
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
            r"\b(?:seniors?(?!\s+(?:housing|exemption|center|citizen\s+exemption))|elderly|retirees?|retired|retirement-aged|older\s+(?:adults?|owners?|people|homeowners?|couples?)|aging\s+(?:owners?|homeowners?|in\s+place)|young\s+(?:buyers?|owners?|people|adults?)|millennials?|boomers?|gen[- ]?[xz])\b",
            rf"\b{_PEOPLE}\s+(?:over|under|aged|age)\s+\d{{2}}\b",
            r"\b\d{2}\+\s+(?:community|living|only|adults?)\b",
        ],
    ),
    (
        "FEHA_SOURCE_OF_INCOME",
        "block",
        "Source-of-income or public-assistance reference (CA FEHA).",
        [
            r"\b(?:section\s+8|housing\s+vouchers?|no\s+vouchers?|public\s+assistance|on\s+welfare|welfare\s+(?:recipients?|benefits?|checks?)|ssi\s+(?:income|recipients?)|disability\s+income|source\s+of\s+income)\b",
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
            r"\b(?:safe|unsafe|dangerous|high[- ]crime|low[- ]crime|crime[- ]free)\s+(?:neighbou?rhoods?|streets?|communit(?:y|ies)|parts?\s+of\s+town)\b",
            r"\b(?:crime\s+(?:rates?|statistics|is\s+low|is\s+high)|low\s+crime|high\s+crime)\b",
            r"\b(?:good|great|excellent|top|best|bad|poor|failing|top-?rated|highly[- ]rated|award-?winning)\s+schools?\b",
            r"\bschools?\s+(?:are|is)\s+(?:good|great|excellent|top|bad|poor|failing|highly\s+rated|top\s+rated)\b",
            r"\bschool\s+(?:district|ratings?|scores?|quality)\b",
            r"\b(?:exclusive|prestigious|upscale|affluent|desirable|undesirable|stable|changing|transitional|up[- ]and[- ]coming|gentrif(?:ying|ied)|working[- ]class|blue[- ]collar|white[- ]collar)\s+(?:neighbou?rhoods?|areas?|communit(?:y|ies)|enclaves?|streets?)\b",
            rf"\b(?:neighbou?rhood|community|area|street)\s+of\s+(?:professionals|executives|retirees|families|young\s+people)\b",
            r"\b(?:the\s+right\s+kind\s+of\s+(?:people|neighbou?rs?)|people\s+like\s+you|your\s+kind|fit\s+in\s+(?:well\s+)?(?:here|with\s+the\s+neighbou?rs))\b",
        ],
    ),
    (
        "DISTRESS_TARGETING",
        "warn",
        "Language implying the owner is being targeted for personal hardship; review for predatory framing (also CA Civ. Code §1695 / §2945 territory).",
        [
            r"\b(?:foreclos(?:ure|ed|ing)|pre-?foreclosure|probate|estate\s+sale|inherit(?:ed|ance)|behind\s+on\s+(?:payments|taxes)|tax\s+lien|hardship|bankrupt(?:cy)?|passed\s+away|deceased|death\s+in\s+the\s+family|recently\s+widowed)\b",
        ],
    ),
    (
        "OCCUPANT_INFERENCE",
        "block",
        "Inference about who lives at the property; dossiers describe the structure only.",
        [
            rf"\b(?:the\s+)?(?:owners?|occupants?|residents?|households?|famil(?:y|ies)|people|folks)\s+(?:(?:is|are|was|were|must\s+be|seem(?:s|ed)?\s+to\s+be)\s+)?(?:probably|likely|apparently|evidently|appears?|seems?|looks?|must\s+be|clearly)\b",
            r"\b(?:toys?|play\s*sets?|swing\s*sets?|trampolines?|strollers?|tricycles?|basketball\s+hoop|religious\s+(?:symbols?|statues?|iconography)|menorah|nativity\s+scene|prayer\s+(?:flags?|rugs?)|pride\s+flag|political\s+signs?|campaign\s+signs?)\b",
            r"\b(?:lives?\s+alone|living\s+alone|home\s+all\s+day|rarely\s+home|never\s+home)\b",
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


# Zero-width / invisible code points that can be used to split a word so a
# regex misses it ("sen\u200biors"). Stripped before matching.
_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff\u00ad\u034f\u180e]")
_TAGS = re.compile(r"<[^>]{0,200}>")


def normalize(text: str) -> str:
    """Canonical form every rule runs against.

    NFKC folds full-width letters, ligatures and compatibility forms; invisible
    joiners are removed; raw HTML tags are stripped so markup can neither hide a
    phrase nor reach the rendered report. Callers get the normalized text back
    from ``screen_text`` so offsets stay consistent.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = _INVISIBLE.sub("", text)
    text = _TAGS.sub("", text)
    return text


def find_violations(text: str) -> List[Dict]:
    """Scan *already normalized* text. Offsets refer to that text."""
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


def _merge_spans(findings: Iterable[Dict]) -> List[Tuple[int, int, str]]:
    """Union overlapping/adjacent block spans so no fragment of either survives."""
    spans = sorted(((f["start"], f["end"], f["rule_id"]) for f in findings if f["severity"] == "block"))
    merged: List[Tuple[int, int, str]] = []
    for start, end, rule in spans:
        if merged and start <= merged[-1][1]:
            pstart, pend, prule = merged[-1]
            rules = prule if rule in prule.split("+") else f"{prule}+{rule}"
            merged[-1] = (pstart, max(pend, end), rules)
        else:
            merged.append((start, end, rule))
    return merged


def redact(text: str, findings: Iterable[Dict]) -> str:
    """Replace every 'block' span (merged) with a bracketed marker, right-to-left."""
    out = text
    for start, end, rules in reversed(_merge_spans(findings)):
        out = out[:start] + f"[REDACTED:{rules}]" + out[end:]
    return out


def screen_fragments(fragments: Iterable[str]) -> Tuple[List[str], List[Dict]]:
    """Screen short free-text items (e.g. vision-model observations).

    Returns the fragments that are clean (normalized) and the findings for the
    ones that were dropped. A blocked fragment is dropped entirely rather than
    redacted — a half-sentence observation has no value and still leaks intent.
    Warnings are kept but reported.
    """
    kept: List[str] = []
    findings: List[Dict] = []
    for raw in fragments:
        text = normalize(str(raw)).strip()
        if not text:
            continue
        found = find_violations(text)
        for f in found:
            findings.append({**f, "fragment": text})
        if any(f["severity"] == "block" for f in found):
            continue
        kept.append(text)
    return kept, findings


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
    """Full firewall pass: normalize, scan, redact, validate rationale, add disclaimers."""
    text = normalize(text)
    findings = find_violations(text)
    redacted = redact(text, findings)
    rationale_findings = validate_lead_rationale(rationale or {})
    final_text, added = ensure_disclaimers(redacted)
    all_findings = findings + rationale_findings
    passed = not any(f["severity"] == "block" for f in all_findings)
    return ComplianceResult(passed=passed, text=final_text, findings=all_findings, disclaimers_added=added)
