"""
ParsedJob and UnresolvedParsedJob — typed contracts for parser-owned job data.

Type system choice (D-01 decision):
    Plain dataclasses with __post_init__/classmethod validators are used rather
    than attrs. Rationale: attrs is not yet a project dependency; adding it
    requires touching pyproject.toml (high-conflict file under parallel
    dispatch) and uv.lock. Plain dataclasses satisfy all contract requirements
    with zero new deps. If Phase 47.02 reveals a need for attrs-specific
    features (post-construction mutation guards, __attrs_post_init__ hooks),
    revisit then.

Invariants enforced here:
    I-07  locations_structured non-empty when locations_raw non-empty → raises
          LocationShapeError
    I-08  title does not match _TITLE_LOCATION_BLEED_RE (Blue State paren
          shape) → UnresolvedParsedJob(reason="title_metadata_blob")
    I-09  title does not contain a locations_raw token after a paren-close →
          UnresolvedParsedJob(reason="title_cross_field_bleed")
    I-10  company not in configured denylist → raises DenylistedCompanyError
    I-13  jd_full either NULL or above content-density floor → UnresolvedParsedJob
          (reason="jd_full_junk") with jd_full=None; other fields preserved
    I-14  title is not a result-count / category-landing tile → raises
          ListingTileError (hard drop; a count tile is not a posting)
    I-15  salary not implausible (P1.6, D-3/D-9) → UnresolvedParsedJob
          (reason="salary_implausible") when a source supplied a salary
          observation the single normalizer could not salvage (resolution
          'implausible') and the canonical pair is therefore NULL. The evidence
          is retained in salary_observations; the NULL canonical re-enters
          enrichment automatically (salary_min IS NULL selection) and the row
          surfaces on /admin/review until a plausible pair resolves.
    I-16  title positively satisfies the title contract (the fail-closed
          inversion) → UnresolvedParsedJob(reason="title_invalid_shape") when the
          CLEANED title still violates ``title_contract_violation`` (embedded
          date / CTA chrome / trailing arrow / control chars) after clean_title
          had its repair pass. Unlike the I-08/I-09 blocklist, this is a positive
          contract: an UNRECOGNIZED shape defaults to quarantine, not clean. The
          contract is versioned (TITLE_HYGIENE_VERSION) and re-applied to every
          existing row by ``_run_title_resweep_if_stale`` so rule changes heal the
          whole corpus, mirroring the dedup NORMALIZER_VERSION re-key.
    I-17  (FOLDED INTO I-18) the title<->body zero-overlap signal
          (``title_jd_mismatch``) was originally scoped as a TITLE check but a
          live-corpus dry-run proved it fires on a GARBAGE jd_full (block /
          Wikipedia / landing pages stored as the JD), not a wrong title. It is
          now wired as a jd-content signal inside I-18 (emitting
          ``jd_full_offsite``), exactly as that deferral note anticipated.
    I-18  jd_full positively is THIS job's posting (the fail-closed jd-content
          contract) → UnresolvedParsedJob with jd_full=None and reason
          ``jd_full_offsite`` (wrong page: Wikipedia / bot wall / listing index /
          404, OR zero title-stem overlap) or ``jd_full_expired`` (dead posting:
          filled / closed / expired). Runs AFTER I-13 on the surviving body via
          ``jd_content_reject``; the AMBIGUOUS middle is left to the background LLM
          adjudicator. Versioned (JD_CONTENT_VERSION) and re-applied to every row
          by ``_run_jd_content_resweep_if_stale`` so rule changes heal the whole
          corpus, mirroring I-16's title re-sweep.

``unresolved_reasons`` vocabulary (the quarantine surface, m078): the codes
``ParsedJob.from_job`` can emit are ``title_metadata_blob`` (I-08), ``title_
cross_field_bleed`` (I-09), ``jd_full_junk`` (I-13), ``salary_implausible``
(I-15), ``title_invalid_shape`` (I-16), ``title_non_posting`` (I-16, funnel
entries), and ``jd_full_offsite`` / ``jd_full_expired`` (I-18). ``data_enricher``
additionally manages ``location_missing``, clears ``salary_implausible`` once a
later pass resolves a plausible salary, and clears the I-18 jd-content codes once
a clean body is re-fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from jobcannon.engine.jd_content_contract import _is_jd_junk as _is_jd_junk
from jobcannon.engine.jd_content_contract import jd_content_reject
from jobcannon.engine.normalizers import derive_dedup_key, normalize_company
from jobcannon.engine.careers_crawler._title_contract import title_contract_violation
from jobcannon.engine.careers_crawler._title_filters import (
    clean_title,
    is_listing_tile,
    is_metadata_blob,
)
from jobcannon.engine.location_canonical import JobLocation
from jobcannon.engine.runtime_config import get_runtime_config
from jobcannon.engine.url_canonical import canonicalize_url

if TYPE_CHECKING:
    from jobcannon.engine.models import Job

# Host-injectable denylist seam. The private repo read this from config.yaml
# (get_company_denylist); the engine takes an injected provider instead.
# No provider registered => empty denylist => the check passes everything.
_denylist_provider: Callable[[], frozenset[str]] | None = None


def set_denylist_provider(provider: Callable[[], frozenset[str]] | None) -> None:
    global _denylist_provider
    _denylist_provider = provider


# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class LocationShapeError(ValueError):
    """I-07: locations_raw is non-empty but locations_structured is empty."""


class DenylistedCompanyError(ValueError):
    """I-10: company name appears in the configured denylist."""


class ListingTileError(ValueError):
    """I-14: title is a result-count / category-landing tile.

    A count tile ("84 Data Scientist Jobs", "1,200+ openings") is a category
    landing page, not a single applyable posting. Unlike the metadata-blob
    validators (I-08/I-09), which flag→UnresolvedParsedJob for human triage,
    this is a HARD DROP — a tile has zero triage value, so we raise and the
    row never enters the pipeline (sibling of ``DenylistedCompanyError``).
    """


# ---------------------------------------------------------------------------
# I-08 regex: title location bleed (Blue State paren-close shape)
# ---------------------------------------------------------------------------

# Matches:
#   ") CA"              — paren-close, optional space, 2-letter state code
#   ") New York, NY"    — Paren)City, ST shape
_TITLE_LOCATION_BLEED_RE = re.compile(
    r"\)\s*[A-Z]{2}\b"  # ") XX" — paren + optional ws + 2-letter state
    r"|"
    r"\)[A-Za-z ]+,\s*[A-Z]{2}\b",  # ")City, ST" — paren + city + comma + state
)

# ---------------------------------------------------------------------------
# I-13: jd_full content density gate
# ---------------------------------------------------------------------------

# _is_jd_junk lives in jd_content_contract (imported at module top) and is
# re-exported under this name here so existing
# ``from jobcannon.engine.parsed_job import _is_jd_junk`` call sites keep
# working without changes.

# ---------------------------------------------------------------------------
# I-09 helper: cross-field title/locations_raw bleed
# ---------------------------------------------------------------------------


def _has_title_cross_field_bleed(title: str, locations_raw: list[str]) -> bool:
    """Return True if title contains a locations_raw token after a paren-close.

    I-09 fires only when:
    - A paren-close character appears in the title, AND
    - At least one alphabetic token (2+ chars) from any locations_raw entry
      appears in the portion of the title after the last paren-close.

    Example:
        title="Software Engineer) San Francisco", locations_raw=["San Francisco, CA"]
        → True (token "San" and "Francisco" appear after ")")
    """
    if not locations_raw or ")" not in title:
        return False
    after_paren = title.split(")", 1)[-1].lower()
    for loc in locations_raw:
        for token in re.findall(r"[A-Za-z]{2,}", loc):
            if token.lower() in after_paren:
                return True
    return False


# ---------------------------------------------------------------------------
# ParsedJob
# ---------------------------------------------------------------------------


@dataclass
class ParsedJob:
    """Typed contract for all parser-owned columns of the jobs table (§8.2.1).

    Fields map 1:1 to the "parser" category in db/column_categories.py.
    Construct via ParsedJob.from_job() to run I-07..I-13 validators.
    Direct construction bypasses validators — only do this in unit tests or
    when you've already applied the validators independently.
    """

    # ── Core identity ───────────────────────────────────────────────────────
    title: str
    company: str
    # derived from (company, title) — not caller-supplied; use from_job()
    dedup_key: str

    # ── Location (flat legacy + structured m066 columns) ────────────────────
    location: str = ""
    locations_raw: list[str] = field(default_factory=list)
    locations_structured: list[JobLocation] = field(default_factory=list)
    workplace_type: str = "UNSPECIFIED"
    primary_country_code: str | None = None

    # ── Sources ─────────────────────────────────────────────────────────────
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_urls_raw: list[str] = field(default_factory=list)
    source_id: str | None = None

    # ── Salary ──────────────────────────────────────────────────────────────
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    salary_period: str = "unknown"
    # Trust-ranked reconciliation metadata (P1.5, D-4). ``salary_provenance`` is
    # the writer class (PROVENANCE_RANK key: ats_structured/jd_regex/llm_extract/
    # email_snippet/feed_string) that produced this pair; None for legacy/unranked
    # callers (treated as rank 0 by the reconciler so anything can overwrite).
    # ``salary_observations`` is the lossless append-log of what each source
    # asserted (D-1); upsert_job appends incoming observations to the stored array.
    salary_provenance: str | None = None
    salary_observations: list[dict] = field(default_factory=list)

    # ── Content ─────────────────────────────────────────────────────────────
    description: str | None = None
    jd_full: str | None = None
    description_reformatted: str | None = None

    # ── Metadata ────────────────────────────────────────────────────────────
    posted_date: datetime | None = None
    posted_date_precision: str | None = None  # 'exact' | 'approximate' | 'proxy'

    # ── Scoring (None at ingest; populated by scorer pipeline) ──────────────
    scoring_provider: str | None = None

    # ── Triage (empty on a clean ParsedJob) ─────────────────────────────────
    unresolved_reasons: list[str] = field(default_factory=list)

    # -----------------------------------------------------------------------

    @classmethod
    def from_job(
        cls,
        job: Job,
        *,
        source_meta: dict | None = None,
    ) -> ParsedJob | UnresolvedParsedJob:
        """Construct a ParsedJob (or UnresolvedParsedJob) from a Job instance.

        ``source_meta`` is an optional dict carrying fields that the Job model
        does not carry (e.g. structured location data, enriched jd_full):

            locations_raw: list[str]             — raw location strings
            locations_structured: list[JobLocation] — structured equivalents
            jd_full: str | None                  — enriched job description
            sources: list[str]                   — accumulated source labels
            source_urls: list[str]               — canonical source URLs
            source_urls_raw: list[str]           — forensic original URLs

        Validator routing (I-07..I-17):

            I-14 (listing tile)             → raises ListingTileError
            I-08 (title_metadata_blob)      → UnresolvedParsedJob, does NOT raise
            I-16 (title_invalid_shape /
                  title_non_posting)        → UnresolvedParsedJob, does NOT raise
            I-09 (title_cross_field_bleed)  → UnresolvedParsedJob, does NOT raise
            I-10 (denylist)                 → raises DenylistedCompanyError
            I-07 (location shape)           → raises LocationShapeError
            I-13 (jd_full junk)             → UnresolvedParsedJob with jd_full=None
            I-15 (salary_implausible)       → UnresolvedParsedJob, does NOT raise

        Reasons from I-08 / I-16 / I-09 / I-13 / I-15 accumulate in
        ``unresolved_reasons``. If I-10 or I-07 raise, no UnresolvedParsedJob is
        returned.

        Title cleaning (``clean_title``) and metadata-blob detection
        (``is_metadata_blob``) also run here (Phase 48.01), universally
        across every ingestion path.
        """
        sm: dict = source_meta or {}

        locations_raw: list[str] = sm.get("locations_raw", [])
        locations_structured: list[JobLocation] = sm.get("locations_structured", [])
        jd_full: str | None = sm.get("jd_full")
        sources: list[str] = sm.get("sources", [job.source])

        # ── Source-URL canonicalization (Phase 49.01, D-06/F-05) ────────────
        # Canonicalize at construction so every ingestion path — including the
        # "touched" branch of upsert_job — stores canonical source_urls with
        # the raw originals preserved in source_urls_raw for forensics. The
        # caller may pre-supply source_urls_raw; otherwise the pre-canonical
        # input IS the forensic original.
        raw_source_urls: list[str] = sm.get("source_urls", [job.source_url])
        source_urls: list[str] = [canonicalize_url(u)[0] for u in raw_source_urls]
        source_urls_raw: list[str] = sm.get("source_urls_raw", list(raw_source_urls))

        unresolved_reasons: list[str] = []
        raw_title: str = job.title

        # ── Title cleaning + metadata-blob detection (Phase 48.01) ──────────
        # Layering (both run on raw_title; see comment for why):
        #
        #   1. is_metadata_blob — catches long concatenated blobs, phrase
        #      markers ("job title", "apply by", etc.), dollar amounts, and
        #      req-ID pipe patterns.  Runs on the raw title BEFORE clean_title
        #      normalises it, because clean_title strips req-ID markers via
        #      _REQID_PREFIX_RE before is_metadata_blob can see them.
        #
        #   2. I-08 (_TITLE_LOCATION_BLEED_RE) — catches the Blue State
        #      paren-close shape (")NY", ")CA").  These titles are too short
        #      to trip is_metadata_blob.  Also runs on the raw title BEFORE
        #      clean_title strips the state-code suffix via _NOSEP_TRAIL_LOC_RE,
        #      which would otherwise remove exactly what I-08 needs to detect.
        #
        #   3. clean_title normalises trailing location/state-code text for all
        #      downstream storage: title field, dedup_key, and I-09.
        #
        # Both I-08 and is_metadata_blob map to the same reason code
        # 'title_metadata_blob'; the distinction is an implementation detail.

        # I-14: result-count / category-landing tile — HARD DROP.
        # Runs before the flag-only blob checks (and before clean_title, which
        # leaves the leading-count + listing-noun shape intact). A count tile is
        # categorically not a posting and carries zero human-triage value, so we
        # raise rather than persist an UnresolvedParsedJob.
        if is_listing_tile(raw_title):
            raise ListingTileError(
                f"Title {raw_title!r} is a result-count / category-landing tile (I-14)"
            )

        if is_metadata_blob(raw_title):
            unresolved_reasons.append("title_metadata_blob")

        # I-08: title location bleed (Blue State paren-close shape)
        if "title_metadata_blob" not in unresolved_reasons and _TITLE_LOCATION_BLEED_RE.search(
            raw_title
        ):
            unresolved_reasons.append("title_metadata_blob")

        cleaned_title: str = clean_title(raw_title)

        # I-16: positive title contract (fail-closed). clean_title has already
        # had its chance to REPAIR the title (e.g. strip a trailing
        # "<Mon D, YYYY> View Job ->" card tail); anything it could not salvage
        # into a clean atomic title — or that is a clean-looking non-posting
        # funnel entry — is quarantined here with the returned reason code
        # (title_invalid_shape | title_non_posting). This inverts the old
        # fail-open default: an unrecognized junk shape now defaults to
        # UnresolvedParsedJob instead of being treated as clean. Skipped only if
        # the title already tripped the metadata-blob path (same triage value).
        if "title_metadata_blob" not in unresolved_reasons:
            _title_reason = title_contract_violation(cleaned_title)
            if _title_reason is not None:
                unresolved_reasons.append(_title_reason)

        # I-09: title cross-field bleed (location token after paren-close)
        if _has_title_cross_field_bleed(cleaned_title, locations_raw):
            if "title_cross_field_bleed" not in unresolved_reasons:
                unresolved_reasons.append("title_cross_field_bleed")

        # I-10: company denylist — raises DenylistedCompanyError.
        # Match on normalize_company (not raw .lower().strip()) so legal-entity
        # suffix variants and aggregator re-posters fire: a denylist
        # entry of "Virtual Vocations" rejects a stored brand of
        # "Virtual Vocations Inc" — both normalize to "virtual vocations".
        # get_company_denylist returns already-normalized entries.
        denylist = _denylist_provider() if _denylist_provider is not None else frozenset()
        if normalize_company(job.company) in denylist:
            raise DenylistedCompanyError(f"Company {job.company!r} is in the configured denylist")

        # I-07: location shape — raises LocationShapeError
        if locations_raw and not locations_structured:
            raise LocationShapeError(
                f"locations_raw has {len(locations_raw)} entries but "
                f"locations_structured is empty (I-07 violation)"
            )

        # I-13: jd_full content density gate (length / shell-prefix).
        clean_jd_full: str | None = jd_full
        if clean_jd_full is not None and _is_jd_junk(clean_jd_full):
            unresolved_reasons.append("jd_full_junk")
            clean_jd_full = None  # row still written, but jd_full cleared

        # I-18: jd_full content contract (fail-closed). The body that survived the
        # density gate must positively be THIS job's posting. jd_content_reject is
        # the deterministic, high-precision floor — it quarantines a body that is a
        # wrong page (Wikipedia / bot wall / listing index / 404), a dead posting
        # (expired / filled), or — using cleaned_title — shares ZERO of the title's
        # content stems (the formerly-deferred I-17 title<->JD signal, now wired
        # here as a jd-content signal exactly as its deferral note anticipated).
        # The AMBIGUOUS middle is left for the background LLM adjudicator, not the
        # synchronous ingest path. The row is still written; jd_full is cleared so
        # enrichment re-fetches a clean body and the score never sees the garbage.
        if clean_jd_full is not None:
            _jd_rej = jd_content_reject(clean_jd_full, cleaned_title, dict(get_runtime_config()))
            if _jd_rej is not None:
                unresolved_reasons.append(_jd_rej[0])
                clean_jd_full = None

        # Salary observations (lossless append-log seed, D-1). Resolved here so the
        # I-15 quarantine detector below can inspect each source's salvage verdict.
        # 'salary_observation' (singular) is the single observation a capture site
        # built for this sighting; else fall back to the Job's observation list.
        salary_observations: list[dict] = (
            [sm["salary_observation"]]
            if sm.get("salary_observation")
            else list(sm.get("salary_observations") or job.salary_observations)
        )

        # I-15 (P1.6, D-3/D-9): the source asserted a salary observation but the
        # single normalizer could not salvage it (resolution 'implausible'), so the
        # capture site left the canonical pair NULL. Quarantine the row via
        # unresolved_reasons — the retained observation surfaces on /admin/review
        # and the NULL canonical re-enters enrichment automatically (the selection
        # query already keys off salary_min IS NULL). Detection is the salvage
        # verdict the capture site stamped onto each observation (never re-derived
        # here — D-2 single normalizer), gated on the canonical pair being NULL.
        if (
            job.salary_min is None
            and job.salary_max is None
            and any(obs.get("resolution") == "implausible" for obs in salary_observations)
        ):
            unresolved_reasons.append("salary_implausible")

        # Derive canonical dedup_key from validated company + cleaned title via
        # the single derivation entry point (D-8) so it matches Job.dedup_key,
        # the upsert lookup, and the retroactive re-key byte-for-byte.
        dedup_key = derive_dedup_key(job.company, cleaned_title)

        # Denormalize structured location fields from locations_structured[0]
        workplace_type = (
            locations_structured[0].workplace_type if locations_structured else "UNSPECIFIED"
        )
        primary_country_code = (
            locations_structured[0].country_code if locations_structured else None
        )

        # source_id: Job stores "" as the empty sentinel; convert to None
        source_id: str | None = job.source_id if job.source_id else None

        common_kwargs: dict = {
            "title": cleaned_title,
            "company": job.company,
            "dedup_key": dedup_key,
            "location": job.location,
            "locations_raw": locations_raw,
            "locations_structured": locations_structured,
            "workplace_type": workplace_type,
            "primary_country_code": primary_country_code,
            "sources": sources,
            "source_urls": source_urls,
            "source_urls_raw": source_urls_raw,
            "source_id": source_id,
            "salary_min": job.salary_min,
            "salary_max": job.salary_max,
            # Salary metadata (Phase 49.02): sourced from the Job (parsers set it
            # where determinable; defaults USD/unknown). source_meta may override
            # for direct ParsedJob construction paths.
            "salary_currency": sm.get("salary_currency", job.salary_currency),
            "salary_period": sm.get("salary_period", job.salary_period),
            # Trust-ranked reconciliation metadata (P1.5/P1.4, D-1/D-4). Capture
            # sites that know their writer class set source_meta['salary_provenance']
            # (e.g. ATS scanners -> 'ats_structured'). Feed/SERP sources (P1.4)
            # instead tag the Job itself via salary_capture_fields, so when
            # source_meta carries nothing we fall back to the Job's fields. Absent
            # both it stays None (unranked). ``salary_observations`` was resolved
            # above (seeded from the singular 'salary_observation' or the Job's
            # list) so the I-15 detector and the persisted log share one source.
            "salary_provenance": sm.get("salary_provenance", job.salary_provenance),
            "salary_observations": salary_observations,
            "description": job.description,
            "jd_full": clean_jd_full,
            "posted_date": job.posted_date,
            "posted_date_precision": job.posted_date_precision,
            "unresolved_reasons": unresolved_reasons,
        }

        if unresolved_reasons:
            return UnresolvedParsedJob(raw_title=raw_title, **common_kwargs)

        return cls(**common_kwargs)


# ---------------------------------------------------------------------------
# UnresolvedParsedJob — sibling type (NOT a subclass of ParsedJob)
# ---------------------------------------------------------------------------


@dataclass
class UnresolvedParsedJob:
    """A job that failed one or more I-08 / I-09 / I-13 validators.

    NOT a subclass of ParsedJob — the union ``ParsedJob | UnresolvedParsedJob``
    is kept explicit so callers cannot accidentally treat an unresolved row as
    clean. Carries the same fields as ParsedJob, plus:

        raw_title: str       — the original pre-clean title (relevant when
                               title was the failing field, e.g. I-08 / I-09)
        unresolved_reasons: list[str]  — non-empty reason codes

    The row is still written by upsert_job (Phase 47.02) with
    ``unresolved_reasons`` persisted to the DB. It surfaces on /admin/review
    for human triage (Phase 47.06 / 47.07).
    """

    # ── Core identity ───────────────────────────────────────────────────────
    title: str
    company: str
    dedup_key: str

    # ── Location ────────────────────────────────────────────────────────────
    location: str = ""
    locations_raw: list[str] = field(default_factory=list)
    locations_structured: list[JobLocation] = field(default_factory=list)
    workplace_type: str = "UNSPECIFIED"
    primary_country_code: str | None = None

    # ── Sources ─────────────────────────────────────────────────────────────
    sources: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    source_urls_raw: list[str] = field(default_factory=list)
    source_id: str | None = None

    # ── Salary ──────────────────────────────────────────────────────────────
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    salary_period: str = "unknown"
    salary_provenance: str | None = None
    salary_observations: list[dict] = field(default_factory=list)

    # ── Content ─────────────────────────────────────────────────────────────
    description: str | None = None
    jd_full: str | None = None
    description_reformatted: str | None = None

    # ── Metadata ────────────────────────────────────────────────────────────
    posted_date: datetime | None = None
    posted_date_precision: str | None = None  # 'exact' | 'approximate' | 'proxy'

    # ── Scoring ─────────────────────────────────────────────────────────────
    scoring_provider: str | None = None

    # ── Triage-specific fields ───────────────────────────────────────────────
    # non-empty by construction when produced by from_job()
    unresolved_reasons: list[str] = field(default_factory=list)
    raw_title: str = ""  # original pre-clean title from the parser
