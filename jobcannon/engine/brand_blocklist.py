"""F8 — brand-name blocklist for speculative ATS probing.

The speculative-probe loop (`ats_scanner._probe.probe_ats_slugs` and
`scripts/f4_reprobe_misses.py`) derives slug candidates from a company's
`name_raw` and queries every supported ATS API for each candidate. For
famous brand-name slugs (`shopify`, `walmart`, `canva`, ...) this produces
brand-collision false positives where a *different*, smaller company has
registered that slug on a small ATS (BambooHR, Recruitee, Pinpoint, ...).

The F4-resume drain (2026-05-21) measured a ~29% false-positive rate (7
of 24 raw hits) on the `ats_probe_status='miss'` cohort. Empirical recon
on those 7 tenants showed they self-identify with the *same* English name
as our DB record (`recruitee/walmart` claims `company_name='Walmart'`;
`bamboohr/canva` claims `og:site_name='Canva'`). There is no name-based
signal that distinguishes the two entities — they share a name. The only
gate that works for this cohort is a curated blocklist.

Scope:
- Applies ONLY to the speculative-probe loop. Explicit `careers_url`
  parsing (`ats_detection.extract_ats_from_url_best`) and the static /
  AI-navigated crawler tiers are unaffected.
- The loop only runs on `ats_probe_status IN ('pending', 'miss')` rows,
  so existing `'hit'` rows are not re-probed and not affected. The
  14 legitimately-famous-named hits currently in the DB (Walmart→Workday,
  Airbnb→Greenhouse, etc.) are preserved as-is. See
  `tests/test_brand_blocklist.py::test_existing_famous_hits_not_re_probed`.

Maintenance:
- The seed list is hand-curated and intentionally narrow. A wider
  Fortune-100 cut was explored 2026-05-21 and rejected — empirical recon
  found 11 existing legit hits (Reddit, Airbnb, Stripe, LinkedIn,
  Pinterest, Lyft, DoorDash, Uber, Disney, Google→DeepMind, Greenhouse
  itself) whose real careers boards ARE inside the speculative-loop
  platforms (Greenhouse / Lever). Blocking those names would preempt
  legit hits if a row ever moves back to `pending` state. The blocklist
  therefore restricts to:
    1. Empirically-confirmed FPs (the 7 from F4-resume 2026-05-21).
    2. Famous brands whose canonical careers system is verified to be
       OUTSIDE the speculative loop (Workday or SmartRecruiters), so
       blocking can never cost us a legit speculative-loop hit. These
       are drawn from existing 'hit' rows in production.
- To add a name: edit `_SEED` below and add a unit test case in
  `tests/test_brand_blocklist.py` documenting which brand-collision
  pattern motivated the addition. Before adding, verify the company is
  NOT already on a speculative-loop ATS in production (grep `companies`
  rows where `ats_probe_status='hit'`).
- A blocked row is marked `ats_probe_status='miss'` with
  `miss_reason='blocked_brand'` so it's visible in the admin UI and the
  scheduler will not re-probe it on every restart.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Seed list: 7 F4-resume-confirmed FPs + Fortune-100-ish famous brands.
#
# Add a name here when you observe a brand-collision FP in production. Always
# add a corresponding test case in tests/test_brand_blocklist.py. Long-name
# brands (≥3 tokens) are low-risk and not worth adding speculatively — focus
# on short single-word famous brands that match small-company slugs.
# ---------------------------------------------------------------------------

_SEED: frozenset[str] = frozenset(
    {
        # === Tier 1: 7 F4-resume confirmed FPs (must-have, 2026-05-21) ===
        # Each of these produced a brand-collision hit on a small-ATS tenant
        # during the F4-resume drain. Manually reverted; blocklist now
        # prevents recurrence on next probe.
        "Shopify",  # pinpoint/shopify is a different small co
        "Atos",  # bamboohr/atos is a different small co (giant Atos is IT services)
        "Circle",  # recruitee/circle is a different small co (not Circle/USDC)
        "Canva",  # bamboohr/canva is a different small co (real Canva uses lifeatcanva.com)
        "LHH",  # pinpoint/lhh is a different small co (real LHH is Adecco subsidiary)
        "Walmart",  # recruitee/walmart is a different small co (real Walmart is Workday)
        "Atrium",  # bamboohr/atrium is a different small co (Inspirations Ltd)
        # === Tier 2: famous brands whose CANONICAL ATS is NOT in the speculative loop ===
        # These are companies with empirical 'hit' rows on Workday or
        # SmartRecruiters in the production DB — both platforms are OUTSIDE
        # the speculative ladder (which covers Lever / Greenhouse / Ashby /
        # Recruitee / Breezy / JazzHR / Pinpoint / Teamtailor / Personio /
        # BambooHR). Blocking can therefore never preempt a legit
        # speculative-loop hit for these companies. Sourced from
        # `companies` rows where ats_probe_status='hit' and ats_platform IN
        # ('workday', 'smartrecruiters').
        "Adobe",  # workday/adobe.wd5/external_experienced
        "Salesforce",  # workday/salesforce.wd12/External_Career_Site
        "Allstate",  # workday/allstate.wd5/allstate_careers
        "General Motors",  # workday/generalmotors.wd5/Careers_GM
        "Centene",  # workday/centene.wd5/centene_external
        "Visa",  # smartrecruiters/Visa
        "AbbVie",  # smartrecruiters/AbbVie
        # === DELIBERATELY NOT INCLUDED ===
        # The following famous brands were considered and REJECTED because
        # production data shows they have a legit ATS presence inside the
        # speculative loop. Blocking them would preempt those legit hits if
        # a row ever moves to 'pending' state:
        #
        #   Reddit         -> greenhouse/reddit          (id 28)
        #   Airbnb         -> greenhouse/airbnb          (id 40)
        #   Pinterest      -> greenhouse/pinterest       (id 60)
        #   Google         -> greenhouse/deepmind        (id 106)
        #   Uber           -> greenhouse/uberfreight     (id 148)
        #   DoorDash       -> greenhouse/doordashusa     (id 150)
        #   LinkedIn       -> lever/linkedin             (id 350)
        #   Lyft           -> greenhouse/lyft            (id 404)
        #   Stripe         -> greenhouse/stripe          (id 878)
        #   Disney         -> greenhouse/disney          (id 3276)
        #   Greenhouse     -> greenhouse/greenhouse      (id 3360)
        #
        # If a true FP appears in production for any of these names,
        # cohort-targeting (NOT a blanket block) is the right response.
    }
)


# ---------------------------------------------------------------------------
# Normalization — single comparison function for both seed entries and DB
# `name_raw` values. Aggressive: strip punctuation, hyphens, whitespace,
# and common corporate suffixes. Single-token comparison gives the same
# result for "Wal-Mart", "WalMart", "Walmart Inc.", "walmart, llc".
# ---------------------------------------------------------------------------

_CORP_SUFFIX = re.compile(
    r"\b(inc|llc|ltd|corp|corporation|limited|holdings|group|gmbh|ag|sa|pty|"
    r"co|nv|bv|ab|oy|plc|spa|srl|kk|kg|company|companies|enterprises)\b\.?",
    re.IGNORECASE,
)
# After suffix strip, drop ALL non-alphanumerics (incl. spaces) so
# "Bank of America" and "Wal-Mart" collapse to single comparison tokens.
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize_brand(name: str) -> str:
    """Collapse a company name to a canonical comparison key.

    Lowercase → strip corporate suffixes → drop all non-alphanumerics.

    Examples:
        >>> _normalize_brand("Wal-Mart")
        'walmart'
        >>> _normalize_brand("Walmart, Inc.")
        'walmart'
        >>> _normalize_brand("Bank of America")
        'bankofamerica'
        >>> _normalize_brand("Bristol-Myers Squibb")
        'bristolmyerssquibb'
    """
    if not name:
        return ""
    n = name.lower()
    n = _CORP_SUFFIX.sub("", n)
    n = _NON_ALNUM.sub("", n)
    return n


# Precompute the normalized set once at import.
_BLOCKED_NORMALIZED: frozenset[str] = frozenset(_normalize_brand(s) for s in _SEED)


def is_blocked_brand(name: str | None) -> bool:
    """Return True if `name` matches a famous-brand entry in the blocklist.

    Used by the speculative-probe loop (`ats_scanner._probe.probe_ats_slugs`,
    `scripts/f4_reprobe_misses.py`, `ats_prober.probe_single_company`) to
    skip companies whose name collides with a known famous brand. See module
    docstring for the cohort-bias rationale.

    Args:
        name: A company `name_raw` value (or None/empty).

    Returns:
        True if the normalized name is in the seed blocklist. False otherwise
        (including for None/empty input).
    """
    if not name:
        return False
    return _normalize_brand(name) in _BLOCKED_NORMALIZED
