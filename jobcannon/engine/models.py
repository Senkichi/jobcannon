"""Data models for Job Finder."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    """Normalized job representation across all sources.

    description vs jd_full split (D-13): ``description`` is the parser-supplied
    short text (present when a source exposes a summary); the canonical full body
    lives in the jobs table's ``jd_full`` column (often fetched separately and
    promoted via ``set_jd_full``). The split exists because some sources only
    expose short text while others provide the full body via a second fetch.
    ``Job`` itself carries only ``description``; ``jd_full`` is a DB-side column.
    """

    title: str
    company: str
    location: str
    source: str  # "linkedin", "glassdoor", "serpapi", etc.
    source_url: str
    source_id: str = ""  # platform-specific job ID

    salary_min: int | None = None
    salary_max: int | None = None
    # Salary metadata (Phase 49.02). Defaults match the m081 column defaults so
    # legacy Job() construction stays backward-compatible; per-source parsers set
    # them where determinable. CHECK allowlists are enforced at the DB boundary.
    salary_currency: str = "USD"
    salary_period: str = "unknown"
    # Trust-ranked reconciliation metadata (P1.4, D-1/D-4). Capture sites that
    # delegate to ``salary_normalizer.salary_capture_fields`` populate these:
    # ``salary_provenance`` is the writer class (PROVENANCE_RANK key, e.g.
    # 'feed_string'); ``salary_observations`` is the lossless append-log seed of
    # what the source asserted (retained even when the canonical pair is NULLed,
    # D-3). ``ParsedJob.from_job`` carries both into the upsert append-log when no
    # source_meta override is supplied. Default empty/None keeps legacy Job()
    # construction (and the unranked-row contract) backward-compatible.
    salary_provenance: str | None = None
    salary_observations: list[dict] = field(default_factory=list)
    description: str | None = None
    posted_date: datetime | None = None
    # Provenance of posted_date (#363): 'exact' (ATS/API first-posted
    # timestamp), 'approximate' (relative-string parse), 'proxy' (detection-
    # time stand-in, e.g. an alert email's Date header). None when unset —
    # the upsert boundary treats a dated job without a marker as 'proxy'
    # (lowest trust), so only sources audited as exact need to say so.
    posted_date_precision: str | None = None

    # Scoring (populated by scorer)
    score: float = 0.0
    score_breakdown: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.title.strip():
            raise ValueError("Job title cannot be empty")
        if not self.company.strip():
            raise ValueError("Job company cannot be empty")
        # Strip Workday-style legal-entity code prefix from the display value
        # ("HC1316 GE Precision Healthcare LLC" -> "GE Precision Healthcare LLC").
        # Dedup also strips this, but doing it here keeps the persisted
        # job.company display field clean from the source.
        from jobcannon.engine.normalizers import strip_legal_entity_prefix

        self.company = strip_legal_entity_prefix(self.company)

        # Coerce posted_date from string → datetime (private issue #108).
        # ATS-API platforms (Lever, Greenhouse, Ashby, SmartRecruiters, …) emit
        # posted_date as an ISO-8601 string; normalise here so every downstream
        # caller (e.g. upsert_job calling .isoformat()) can rely on the field
        # being a datetime (or None) and never receiving an AttributeError.
        # On an unparseable string we fall back to None rather than raising,
        # because the job itself is valid — only the date is unusable.
        if isinstance(self.posted_date, str):
            try:
                self.posted_date = datetime.fromisoformat(self.posted_date)
            except ValueError:
                self.posted_date = None

        # Pairing invariant (I-14): precision describes posted_date, so it
        # cannot outlive a date that failed to parse (or was never set).
        if self.posted_date is None:
            self.posted_date_precision = None

    # Dedup key
    @staticmethod
    def normalized_dedup_key(company: str, title: str, location: str = "") -> str:
        """Compute the normalized dedup_key for a job.

        Location is INTENTIONALLY EXCLUDED -- same company + same title = same job
        regardless of location text differences.

        Args:
            company: Raw company name.
            title: Raw job title.
            location: Ignored. Kept for backward-compatible call signatures.

        Returns:
            String in format "{normalized_company}|{normalized_title}"
        """
        from jobcannon.engine.normalizers import derive_dedup_key

        return derive_dedup_key(company, title)

    @property
    def dedup_key(self) -> str:
        """Normalized key for deduplication.

        Uses company+title only (location intentionally excluded per user decision:
        same company + same title = same job regardless of location differences).
        Normalizes company suffixes (Inc., LLC) and title abbreviations (Sr.->Senior).
        """
        return Job.normalized_dedup_key(self.company, self.title)
