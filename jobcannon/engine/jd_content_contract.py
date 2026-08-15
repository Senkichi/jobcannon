"""Positive jd_full content contract — "is this text the description of THIS job?"

WHY THIS EXISTS (the same architectural inversion as ``_title_contract``)
-------------------------------------------------------------------------
The historical jd_full gate (``_jd_full._is_jd_junk``, I-13) is a *fail-open
denylist*: ``len < 200`` OR the first 200 chars ``.startswith()`` one of 7
hardcoded prefixes. Anything long that does not *begin* with those 7 strings is
treated as a valid job description, scored, and surfaced. A live-corpus audit
(13,664 rows) found that lets through **~14–22%** non-JD bodies — Wikipedia
articles, "REQUEST DENIED" bot walls, expired-posting pages, careers-landing
chrome, job-listing index pages, even a 29 KB site-maintenance notice — and that
**~1,400–2,500 of them had already been scored** against that garbage.

Enumerating bad shapes cannot win (the junk is far too heterogeneous) and
over-fires on real JDs (a JD that mentions "cookies" or "cloudflare" is not
junk). So this module inverts the default to **fail-closed**: a stored jd_full is
trustworthy only if it positively looks like a single job posting for its own
title. Unknown ⇒ quarantine (clear + re-enrich), never silently scored.

THREE-OUTCOME VERDICT (so the LLM only runs on the genuine residual)
--------------------------------------------------------------------
``classify_jd_content`` returns one of:

* ``REJECT`` — deterministic, HIGH precision. The body is provably not this job's
  posting: a wrong page (Wikipedia / bot wall / block page / listing index /
  404), a dead posting (expired / filled), or a substantial body that shares
  ZERO of the title's content stems (the I-17 ``title_jd_mismatch`` signal,
  finally wired here as a *jd-content* signal exactly as its deferral note
  anticipated). Safe to act on with no LLM and no human.
* ``CLEAN`` — deterministic, HIGH confidence: a JD-shape signal is present AND the
  body is grounded in the title/company AND it is substantial. The common case.
* ``AMBIGUOUS`` — everything else. Resolved by a cheap local-LLM tie-breaker
  ("is this the JD for <title> at <company>?") run by the background adjudicator,
  NOT on the hot ingest path and NOT during the synchronous startup re-sweep.

ENFORCEMENT (single points, mirrored from the title contract)
-------------------------------------------------------------
* ``jd_content_reject`` runs inside the sole sanctioned writer ``set_jd_full``
  and at the ``ParsedJob.from_job`` ingest gate — the deterministic floor that
  can never store obvious junk. Both callers pass the job's ``title`` when they
  have it (the enrichment write path always does), so the title cross-field
  reject (I-17 ``title_zero_overlap``) fires at the write chokepoint, falling
  back to content-only signals when no title is available. A leading JSON
  configuration blob with an empty or missing ``job_description`` is also
  rejected here, so a long Eightfold/Netflix micro-site config payload cannot
  satisfy the length gate while carrying no prose.
* ``classify_jd_content`` (the full 3-way) runs in the background adjudicator
  and the versioned re-sweep, so the AMBIGUOUS residual is LLM-resolved off the
  hot path and a rule improvement heals the whole corpus on a
  ``JD_CONTENT_VERSION`` bump.

The module is PURE (regex + JSON structural checks + the shared ``normalizers``
token helpers) so it is deterministic, unit-testable, and importable from
``db/`` without a web cycle.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from jobcannon.engine.normalizers import body_mentions_any_stem, significant_tokens

# ---------------------------------------------------------------------------
# Version watermark (D-8). BUMP whenever the rules below change such that an
# already-stored jd_full could newly pass or newly fail. Bumping re-arms the
# standing re-sweep so the whole corpus is re-validated under the new version.
# Mirrors NORMALIZER_VERSION / TITLE_HYGIENE_VERSION.
# ---------------------------------------------------------------------------
JD_CONTENT_VERSION: int = 4

# Reason codes emitted into jobs.unresolved_reasons (the m078 quarantine surface).
# Distinct from I-13's ``jd_full_junk`` (the length/density gate, owned by
# ``_jd_full``): these are content-provenance failures owned by THIS contract and
# recomputed by its re-sweep.
JD_OFFSITE: str = "jd_full_offsite"
JD_EXPIRED: str = "jd_full_expired"
JD_TRUNCATED: str = "jd_full_truncated"

#: All jd-content reason codes the re-sweep owns + recomputes.
JD_CONTENT_REASON_CODES: frozenset[str] = frozenset({JD_OFFSITE, JD_EXPIRED, JD_TRUNCATED})

# ---------------------------------------------------------------------------
# Tunables (validated against the live 13,664-row corpus via scripts/jd_*).
# ---------------------------------------------------------------------------
_HEAD_WINDOW: int = 400  # chars examined for "leads-with-junk" signals
_CLEAN_MIN_CHARS: int = 600  # a confidently-CLEAN body must be at least this long
_XFIELD_MIN_CHARS: int = 300  # min body before the title zero-overlap check can fire
_XFIELD_MIN_TOKENS: int = 2  # min significant title tokens before zero-overlap fires

# ---------------------------------------------------------------------------
# HIGH-PRECISION REJECT signals. Each was confirmed near-zero-false-positive on
# the live corpus. Keep this set tight and high-precision — it is NOT an
# open-ended junk blocklist; the AMBIGUOUS→LLM path is where uncertainty goes.
# ---------------------------------------------------------------------------

#: Block / challenge / encyclopedic markers — only meaningful when they LEAD the
#: body (checked in the first _HEAD_WINDOW chars), so a JD that merely mentions
#: "cloudflare" or "javascript" deep in prose is not flagged.
_HEAD_BLOCK_RE = re.compile(
    r"from wikipedia, the free encyclopedia"
    r"|request denied"
    r"|are you a robot"
    r"|verify you are (?:human|not a robot)"
    r"|attention required"
    r"|just a moment"
    r"|checking your browser"
    r"|you have been blocked"
    r"|access (?:to this page )?(?:has been )?denied"
    r"|enable javascript"
    r"|please enable (?:js|javascript|cookies)"
    r"|unusual traffic from your"
    r"|complete the security check"
    r"|ddos protection",
    re.IGNORECASE,
)

#: A job-listing INDEX captured as a posting: "399 ... jobs in Boston",
#: "1,000+ Chief Clinical Officer jobs in United States", "# 9 Fox Motors Jobs in
#: United States". The count (optionally comma-grouped / "+"-suffixed) + "jobs in
#: <place>" header is the structural tell and does not occur in a single
#: posting's body.
_LISTING_COUNT_RE = re.compile(
    r"\b\d[\d,]{0,4}\+?\s+[\w\s,&/+.\-]{0,40}?\bjobs\s+in\b",
    re.IGNORECASE,
)

#: 404 / page-not-found offsite captures — head-only (a real JD does not LEAD
#: with these). "404" must appear in an explicit error context: a bare "404"
#: matches legitimate content ("404 Total Employees", a "$404" rate), so it is
#: NOT accepted on its own.
_NOT_FOUND_RE = re.compile(
    r"\b404\s+(?:error|not\s+found|page)"
    r"|\b(?:error|http)\s+404\b"
    r"|\bpage\s+not\s+found\b"
    r"|the\s+page\s+you\s+(?:requested|are\s+looking\s+for)"
    r"|\bpage\s+(?:cannot|can(?:'|’)?t)\s+be\s+found\b"
    r"|\b410\s+gone\b",
    re.IGNORECASE,
)

#: Unfilled CMS/site-builder placeholder scaffold (a careers page whose template
#: was never customized with real content) — head-only, same discipline as
#: _HEAD_BLOCK_RE. NOT checked against the whole body: a real, already-scored JD
#: can carry this exact text as leftover widget noise (a "related content"/video
#: carousel the employer never cleaned up) FAR into the page — confirmed on the
#: live corpus (PNC "Watch the video... Widget title goes here. Your engaging
#: subtitle goes here" at char 1678, and four UnitedHealth/Petco rows with
#: "Lorem Ipsum is simply dummy text" past char 5000 — all genuine, grounded,
#: shape-bearing JDs). Only diagnostic when it LEADS the page, i.e. the page IS
#: the unfilled template rather than a real JD with template noise appended.
_CMS_PLACEHOLDER_RE = re.compile(
    r"your engaging (?:footer )?subtitle goes here"
    r"|widget title goes here"
    r"|lorem ipsum is simply dummy text",
    re.IGNORECASE,
)

#: "Zero results for your search" chrome from a careers-search widget — head-only.
#: Deliberately requires "your search" co-occurring (not just "no jobs"/"no
#: positions") because a real JD can legitimately say "there are no jobs to
#: display" about an unrelated RELATED-JOBS widget elsewhere on its own page
#: (confirmed on a live Target row, head char 356) without itself being junk.
_NO_SEARCH_RESULTS_RE = re.compile(
    r"no jobs? (?:for|match(?:ing)?) your search"
    r"|no (?:results|positions|openings) (?:found )?for your search",
    re.IGNORECASE,
)

#: Dead-posting markers (anywhere). Phrases are full page-template sentences that
#: a live posting's own body never contains about itself.
_EXPIRED_RE = re.compile(
    r"\bthis\s+(?:job|position|posting|role|listing|vacancy|opening)\s+(?:is|has\s+been)\s+"
    r"(?:no\s+longer\s+available|no\s+longer\s+active|filled|closed|expired)"
    r"|\bthis\s+(?:job|position|posting)\s+is\s+no\s+longer\b"
    r"|\bno\s+longer\s+accepting\s+applications\b"
    r"|the\s+job\s+you\s+are\s+trying\s+to\s+apply\s+for\s+has\s+been\s+filled"
    r"|\bthis\s+job\s+has\s+closed\b"
    r"|\bposition\s+has\s+been\s+filled\b"
    r"|\bjob\s+expired\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# POSITIVE JD-shape signal — at least one section/affordance a real posting has.
# Necessary (not sufficient) for a deterministic CLEAN; the grounding + length
# checks supply the rest of the confidence.
# ---------------------------------------------------------------------------
_JD_POSITIVE_RE = re.compile(
    r"\bresponsibilities\b"
    r"|\bqualifications\b"
    r"|\brequirements\b"
    r"|what\s+you(?:'|’)?(?:ll|\s+will)\s+do"
    r"|what\s+we(?:'|’)?(?:re|\s+are)\s+looking\s+for"
    r"|about\s+(?:the|this)\s+role"
    r"|we(?:'|’)?(?:re|\s+are)\s+looking\s+for"
    r"|minimum\s+qualifications"
    r"|preferred\s+qualifications"
    r"|who\s+you\s+are"
    r"|your\s+(?:impact|role|responsibilities)"
    r"|in\s+this\s+role"
    r"|the\s+ideal\s+candidate"
    r"|what\s+you(?:'|’)?(?:ll|\s+will)\s+bring"
    r"|you\s+will\s+be\s+responsible"
    r"|key\s+(?:responsibilities|duties)"
    r"|essential\s+(?:functions|duties)"
    r"|job\s+(?:description|summary|duties)"
    r"|role\s+(?:overview|summary)"
    r"|day[\s-]to[\s-]day"
    r"|duties\s+include",
    re.IGNORECASE,
)


# --- jd_full completeness thresholds ---
# Minimum characters for a job description to be accepted as the full jd_full.
# A body below this floor, or ending in a trailing ellipsis/…, is treated as a
# truncated snippet and routed back to enrichment.
#
# ADAPTATION (declared in the port record): upstream these defaults and the
# resolver live in the app-level config module. The engine cannot import that
# layer (boundary: pure engine, no host config), and this contract module is
# the only engine consumer, so the resolver is inlined here verbatim.
DEFAULT_JD_FULL_MIN_CHARS = 200
DEFAULT_JD_FULL_REJECT_TRAILING_ELLIPSIS = True


def get_jd_full_thresholds(config: dict | None = None) -> tuple[int, bool]:
    """Resolve jd_full completeness thresholds from config.

    Config-shape defense only (issue #37): a null/non-dict ``enrichment`` or
    ``jd_full`` section, or a ``min_chars`` leaf that doesn't coerce to int,
    degrades to the module defaults instead of raising. This does not change
    the resolved value for any config shape that already resolved
    successfully — it only defines behavior for shapes that previously threw
    (``AttributeError`` / ``ValueError`` / ``TypeError``) inside the
    ``jd_content_reject`` storage/ingest gate.

    Returns:
        (min_chars, reject_trailing_ellipsis) with safe defaults.
    """
    if config is None:
        config = {}
    jd_cfg = (config.get("enrichment") or {}).get("jd_full") or {}
    if not isinstance(jd_cfg, dict):
        jd_cfg = {}
    try:
        min_chars = int(jd_cfg.get("min_chars", DEFAULT_JD_FULL_MIN_CHARS))
    except (TypeError, ValueError):
        min_chars = DEFAULT_JD_FULL_MIN_CHARS
    reject_ellipsis = bool(
        jd_cfg.get("reject_trailing_ellipsis", DEFAULT_JD_FULL_REJECT_TRAILING_ELLIPSIS)
    )
    if min_chars < 1:
        min_chars = DEFAULT_JD_FULL_MIN_CHARS
    return min_chars, reject_ellipsis


#: Trailing ellipsis/… (optionally followed by whitespace). A body ending like
#: this is almost always a search-result snippet, not a full JD.
_TRAILING_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)\s*$")


# Leading non-whitespace characters that indicate a serialized JSON payload.
_JSON_START_CHARS: frozenset[str] = frozenset({"{", "["})


def _is_json_config_blob(jd_full: str) -> bool:
    """Return True when *jd_full* is a leading JSON object with no real prose.

    Eightfold/Netflix micro-sites sometimes serve the entire page body as a
    configuration JSON blob (theme, fonts, supported locales, and an empty
    ``job_description``). The blob clears the length gate while containing no
    actual job description, so the scorer sees a long "JD" with no signal.

    Detection is intentionally tight:
      * the stripped body must start with ``{`` or ``[``;
      * the leading JSON value must parse and dominate the body (so a JD that
        merely contains an inline JSON block is not rejected);
      * for an object, there must be no non-empty ``job_description`` or
        ``description`` field. A real posting served as JSON with a genuine
        prose description is left for the normal contract.
    """
    stripped = jd_full.strip()
    if not stripped or stripped[0] not in _JSON_START_CHARS:
        return False
    try:
        obj, end = json.JSONDecoder().raw_decode(stripped, 0)
    except json.JSONDecodeError:
        return False

    # The JSON value must be the bulk of the body. Anything after the first
    # complete value that is more than trailing whitespace is real prose, not a
    # pure serialized blob.
    if end < len(stripped) - 20:
        return False

    if not isinstance(obj, dict):
        # Leading arrays / scalars that dominate the body are also not prose.
        return True

    for key, val in obj.items():
        if key.lower() in ("job_description", "description"):
            if isinstance(val, str) and val.strip():
                # Real prose description inside a JSON object -> not a config blob.
                return False
            # Empty/missing description in a leading JSON object -> config blob.
    return True


def _is_jd_truncated(
    jd_full: str | None,
    config: dict | None = None,
    *,
    check_min: bool = True,
) -> tuple[str, str] | None:
    """Return (JD_TRUNCATED, signal) if the body is a truncated snippet.

    Two independent signals, both config-driven:
      * ``too_short`` — stripped length is below ``enrichment.jd_full.min_chars``.
      * ``trailing_ellipsis`` — body ends with ``...`` or ``…`` and
        ``enrichment.jd_full.reject_trailing_ellipsis`` is true.
    """
    if not jd_full:
        return None
    stripped = jd_full.strip()
    min_chars, reject_ellipsis = get_jd_full_thresholds(config)
    if check_min and len(stripped) < min_chars:
        return (JD_TRUNCATED, "too_short")
    if reject_ellipsis and _TRAILING_ELLIPSIS_RE.search(stripped):
        return (JD_TRUNCATED, "trailing_ellipsis")
    return None


class JdVerdict(Enum):
    """Outcome of the jd-content contract."""

    CLEAN = "clean"
    REJECT = "reject"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class JdContentResult:
    """A jd-content verdict plus the forensic signal that produced it.

    ``reason`` is the ``unresolved_reasons`` code (``jd_full_offsite`` /
    ``jd_full_expired``) when ``verdict is REJECT``, else None. ``signal`` is a
    short human-readable tag for logging and the dry-run (e.g. ``"head_block``
    ``_or_wiki"``, ``"title_zero_overlap"``, ``"shape+grounded"``).
    """

    verdict: JdVerdict
    reason: str | None
    signal: str


def jd_content_reject(
    jd_full: str | None,
    title: str | None = None,
    config: dict | None = None,
) -> tuple[str, str] | None:
    """Deterministic HIGH-precision reject check.

    Returns ``(reason_code, signal)`` if the body is provably not this job's
    posting, else None. The content-only signals (wiki / block / listing / 404 /
    expired / truncated) need no title and are safe to enforce at every write
    (``set_jd_full``). The title zero-overlap signal additionally requires
    *title* and a substantial body; it is the wired I-17 ``title_jd_mismatch``.

    Pure and side-effect free — the identical call backs the storage gate, the
    ingest gate, and the re-sweep.
    """
    if not jd_full:
        return None
    stripped = jd_full.strip()

    truncated = _is_jd_truncated(stripped, config, check_min=True)
    if truncated is not None:
        return truncated

    low = stripped.lower()
    head = low[:_HEAD_WINDOW]

    if _HEAD_BLOCK_RE.search(head):
        return (JD_OFFSITE, "head_block_or_wiki")
    if _LISTING_COUNT_RE.search(head):
        return (JD_OFFSITE, "listing_index")
    if _NOT_FOUND_RE.search(head):
        return (JD_OFFSITE, "not_found")
    if _CMS_PLACEHOLDER_RE.search(head):
        return (JD_OFFSITE, "cms_placeholder")
    if _NO_SEARCH_RESULTS_RE.search(head):
        return (JD_OFFSITE, "no_search_results")
    if _EXPIRED_RE.search(low):
        return (JD_EXPIRED, "expired_or_filled")

    # Serialized configuration / markup with no prose job description.
    # A long JSON blob (Eightfold/Netflix micro-site config, empty
    # ``job_description``) fools the length gate; reject at the content layer
    # so the scorer never sees it.
    if _is_json_config_blob(stripped):
        return (JD_OFFSITE, "json_config_blob")

    # I-17 wired: a substantial body that shares ZERO of the title's content
    # stems is the silent wrong-page case (the body is about something else).
    if title and len(stripped) >= _XFIELD_MIN_CHARS:
        tokens = significant_tokens(title)
        if len(tokens) >= _XFIELD_MIN_TOKENS and not body_mentions_any_stem(tokens, low):
            return (JD_OFFSITE, "title_zero_overlap")
    return None


def has_recognizable_jd_shape(text: str | None) -> bool:
    """Public: does text contain a recognizable JD section signal
    (responsibilities/qualifications/'what you'll do'/...)?

    Single point of enforcement for the positive JD-shape vocabulary
    (``_JD_POSITIVE_RE``) so callers outside this module (the structural-axes
    jd-quality scorer) never duplicate the regex. ``classify_jd_content``
    below is refactored to call this wrapper for the identical check.
    """
    if not text:
        return False
    return bool(_JD_POSITIVE_RE.search(text.lower()))


def classify_jd_content(
    jd_full: str | None,
    title: str | None = None,
    company: str | None = None,
    config: dict | None = None,
) -> JdContentResult:
    """Full three-way jd-content verdict (REJECT / CLEAN / AMBIGUOUS).

    Used by the fetch-path gate and the versioned re-sweep, which both hold the
    job's title and company for cross-field grounding. The storage/ingest gates
    use the cheaper ``jd_content_reject`` directly.

    CLEAN requires ALL of: a positive JD-shape signal, grounding in the job's own
    TITLE (a content stem of the title appears in the body), and a substantial
    length. Title grounding (not company) is deliberate: a company *About*/
    marketing page is grounded by the company name yet is not the posting, so
    company-only grounding is treated as weak evidence and routed to the LLM. When
    no title is available, the company name is the only fallback. Anything short
    of CLEAN — but not a deterministic REJECT — is AMBIGUOUS for the LLM tie-breaker.

    ``company`` is currently unused by the deterministic split (kept in the
    signature because the LLM adjudicator the AMBIGUOUS path feeds needs it, and
    callers already have it to hand).
    """
    rej = jd_content_reject(jd_full, title, config)
    if rej is not None:
        return JdContentResult(JdVerdict.REJECT, rej[0], rej[1])
    if not jd_full:
        return JdContentResult(JdVerdict.AMBIGUOUS, None, "empty")

    stripped = jd_full.strip()
    low = stripped.lower()
    has_shape = has_recognizable_jd_shape(low)
    substantial = len(stripped) >= _CLEAN_MIN_CHARS

    ground_tokens = significant_tokens(title) if title else significant_tokens(company or "")
    grounded = body_mentions_any_stem(ground_tokens, low)

    if has_shape and grounded and substantial:
        return JdContentResult(JdVerdict.CLEAN, None, "shape+grounded")
    return JdContentResult(JdVerdict.AMBIGUOUS, None, "needs_adjudication")


# ---------------------------------------------------------------------------
# re-homed from job_finder/db/_jd_full.py (plan Task 1 Step 6 amendment):
# the I-13 length/density gate, ported verbatim along with the two constants
# its body reads. Both constants are plain literals with no external deps.
# ---------------------------------------------------------------------------
_MIN_JD_LENGTH: int = 200  # characters, post-strip

# Shell / auth-wall prefix patterns mirroring the tg_jobs_jd_full_junk trigger
# (m078).  Applied case-insensitively to the first 200 stripped chars.
# SINGLE SOURCE OF TRUTH — imported by m078_contract_invariants and
# pre_m078_remediation; do NOT duplicate these values elsewhere.
_JD_JUNK_PREFIXES: tuple[str, ...] = (
    "sign in",
    "loading",
    "open roles at",
    "skip to content",
    "cookie",
    "privacy policy",
    "404",
)


def _is_jd_junk(text: str) -> bool:
    """Return True if jd_full content fails the I-13 density gate.

    Two failure modes:
    - Text shorter than ``_MIN_JD_LENGTH`` after stripping whitespace.
    - Text whose first 200 chars (lowercased) start with a junk prefix.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_JD_LENGTH:
        return True
    prefix = stripped[:200].lower()
    return any(prefix.startswith(p) for p in _JD_JUNK_PREFIXES)
