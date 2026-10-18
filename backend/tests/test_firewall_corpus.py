"""Firewall corpus: sentences that MUST be blocked and sentences that MUST pass.

Every entry is a realistic string the vision model or a template could emit.
Add a case here whenever a false positive or false negative is found in the
field; the rule change is not done until the corpus is green.
"""

import pytest

from agents.compliance import find_violations, normalize, redact, screen_fragments, screen_text


def _blocked(text: str) -> bool:
    return any(f["severity"] == "block" for f in find_violations(normalize(text)))


# --- Must be BLOCKED --------------------------------------------------------------
MUST_BLOCK = [
    # race / color / national origin
    "This is a mostly white neighborhood.",
    "Predominantly Latino community with strong cultural ties.",
    "A largely Latino-owned street.",
    "Mostly whites live here.",
    "Jews and Muslims live nearby.",
    "This area is popular with Spanish-speaking households.",
    "Many immigrants on this block.",
    "A diverse neighborhood.",
    "Mixed neighborhood in transition.",
    # religion
    "Walking distance to St. Mary's parish school.",
    "Ideal for someone who wants to be near a good church community.",
    "Close to the Catholic school.",
    # familial status / age
    "A perfect home for a growing family.",
    "Perfect starter home for a young couple.",
    "Quiet street, mostly retirees and empty nesters.",
    "Ideal for seniors looking to downsize.",
    "Household seems to have small kids based on the play set.",
    "Owner is probably retired.",
    "Toys in the yard.",
    "Owner appears to be an elderly widow living alone.",
    # disability / source of income
    "No Section 8 tenants nearby.",
    "Not suitable for disabled occupants.",
    # steering
    "The neighborhood has stayed stable and the schools are highly rated.",
    "Located in a safe neighborhood with great schools.",
    "Top-rated school district.",
    "The crime rate is low here.",
    "Upscale neighborhood of professionals.",
    "You would fit in well with the neighbors.",
    # occupant inference
    "Young families appear to live here.",
    "Pride flag on the porch.",
    # evasion
    "Ideal for sen​iors.",
    "Ｉｄｅａｌ ｆｏｒ ｓｅｎｉｏｒｓ.",
    "Great for <b>young</b> families.",
]

# --- Must PASS ----------------------------------------------------------------------
MUST_PASS = [
    # colors / materials
    "White trim is peeling; the black asphalt roof shows granule loss.",
    "Peeling white paint on the fascia; brown stucco is intact.",
    "Indian laurel fig hedge along the east fence.",  # plant, not people
    # legitimate structure language that previously tripped rules
    "The roof area appears sound.",
    "Faithful restoration of the original fascia boards.",
    "Widow's walk on the roof needs railing repair.",
    "Foreign material in the gutters; downspouts clogged.",
    "The welfare of the structure depends on drainage.",
    "A safe area for ladder placement on the south side.",
    "Mormon tea shrub along the driveway.",
    "Property fronts Christian Rd near the cul-de-sac.",
    "Roof is over 25 years old; owner may want a senior exemption review.",
    "Mature landscaping with established oak trees along the driveway.",
    "The home sits in a quiet cul-de-sac near Harmony Grove Village Park.",
    "Detached garage could support a 1-bedroom ADU under current county rules.",
    "Property is adjacent to the Elfin Forest Recreational Reserve.",
    "The single-story layout and wide doorways are notable features.",
    "Lot is 2.4 acres with a well and septic; no HOA.",
    "Low-maintenance xeriscape front yard; mature citrus in rear.",
    "Close to Del Dios Highway and Lake Hodges trails.",
    "Great family room addition visible on the north elevation.",
    "Hairline stucco cracks near the garage header.",
    "Curling asphalt shingles on the south slope.",
    "Wood rot at the base of the porch columns.",
    "Chimney cap missing; flashing lifted at the ridge.",
    "Driveway has settlement cracks; drainage slopes toward the garage.",
    "The structure appears to be a 1970s ranch with a later addition.",
    "Wheelchair ramp at the side entry is in good condition.",  # warn, not block
    "Near the church on the corner, the sidewalk is heaved.",  # warn, not block
    "Two-story addition on the north side.",
]


@pytest.mark.parametrize("text", MUST_BLOCK)
def test_must_block(text):
    assert _blocked(text), f"FALSE NEGATIVE: {text!r} passed the firewall"


@pytest.mark.parametrize("text", MUST_PASS)
def test_must_pass(text):
    found = [f for f in find_violations(normalize(text)) if f["severity"] == "block"]
    assert not found, f"FALSE POSITIVE: {text!r} blocked by {[f['rule_id'] + ':' + f['matched_text'] for f in found]}"


def test_overlapping_spans_merge_completely():
    res = screen_text("Young families appear to live here. Roof ok.")
    first_line = res.text.split("\n")[0]
    assert "Young" not in first_line and "families" not in first_line, first_line
    assert first_line.startswith("[REDACTED:")


def test_redact_adjacent_spans():
    text = "no kids adults only"
    out = redact(text, find_violations(text))
    assert "kids" not in out and "adults" not in out


def test_normalize_strips_invisible_and_tags():
    assert normalize("sen​iors") == "seniors"
    assert normalize("<script>alert(1)</script>ok") == "alert(1)ok"
    assert normalize("Ｓeniors") == "Seniors"


def test_screen_fragments_drops_blocked_keeps_clean():
    kept, findings = screen_fragments([
        "curling shingles on south slope",
        "toys in the yard suggest young children",
        "<img src=x onerror=alert(1)> peeling fascia",
        "",
    ])
    assert kept == ["curling shingles on south slope", "peeling fascia"]
    assert any(f["severity"] == "block" for f in findings)
    assert all("fragment" in f for f in findings)
