"""Regression tests for #223: the persisted dedup_key is what gets enqueued for scoring.

There are two independent dedup_key derivations:

  * ``Job.dedup_key`` -- normalizes the RAW title via ``normalize_title``.
  * ``ParsedJob.from_job(...).dedup_key`` -- normalizes ``clean_title(raw_title)``,
    which additionally strips req-IDs, trailing-location suffixes, dash-suffix
    qualifiers and leading "logo letter" prefixes.

When ``clean_title(raw) != raw``, those two derivations diverge. The row is
persisted under the ParsedJob (cleaned) key, but until #223 the inserting
caller appended ``job.dedup_key`` (raw) to ``new_job_keys``, so
``run_scoring`` looked up a key that was never persisted and silently
skipped the job (only a WARNING was logged).

These tests lock in the invariant that the persisted key is the one used for
scoring lookups at every ingestion append site.
"""

from __future__ import annotations

from unittest.mock import patch

from jobcannon.engine.models import Job
from jobcannon.engine.parsed_job import ParsedJob

# A handful of titles that exercise the four clean_title rules the issue calls
# out. Each one MUST have ``Job.dedup_key != ParsedJob.from_job(job).dedup_key``
# -- otherwise the test below is no longer guarding the divergence and the
# fix is no-op for that input class.
DIVERGENT_TITLES = [
    # dash-suffix qualifier (the Apple case that fired the overnight finding)
    "Staff Data Scientist - Experimentation",
    # paren-wrapped trailing-location suffix
    "Data Scientist (USA-Remote)",
    # plain " - Remote" trailing-location suffix
    "Software Engineer - Remote",
]


# ---------------------------------------------------------------------------
# Site 1 vs Site 2: the two derivations differ for these titles
# ---------------------------------------------------------------------------


class TestDedupKeyDerivationsDiverge:
    """The two dedup_key derivations DO diverge for the issue's title shapes.

    If clean_title is ever loosened so one of these no longer diverges, the
    test breaks loudly so the fix can be re-evaluated for that input class.
    """

    def test_dash_suffix_title_diverges(self):
        """'- Experimentation' is stripped by clean_title but not normalize_title."""
        job = Job(
            title="Staff Data Scientist - Experimentation",
            company="Apple",
            location="Remote",
            source="test",
            source_url="https://example.com/1",
        )
        with (
            patch("jobcannon.engine.parsed_job.load_config", return_value={}),
            patch(
                "jobcannon.engine.parsed_job.get_company_denylist",
                return_value=frozenset(),
            ),
        ):
            parsed = ParsedJob.from_job(job)

        raw_key = job.dedup_key
        persisted_key = parsed.dedup_key
        assert raw_key != persisted_key, (
            f"#223 invariant: clean_title should strip ' - Experimentation' so "
            f"raw_key={raw_key!r} diverges from persisted_key={persisted_key!r}"
        )

    def test_each_divergent_title_actually_diverges(self):
        """Sanity-check the DIVERGENT_TITLES fixture: every entry must diverge."""
        for title in DIVERGENT_TITLES:
            job = Job(
                title=title,
                company="Acme Corp",
                location="Remote",
                source="test",
                source_url=f"https://example.com/{hash(title)}",
            )
            with (
                patch("jobcannon.engine.parsed_job.load_config", return_value={}),
                patch(
                    "jobcannon.engine.parsed_job.get_company_denylist",
                    return_value=frozenset(),
                ),
            ):
                parsed = ParsedJob.from_job(job)
            assert job.dedup_key != parsed.dedup_key, (
                f"DIVERGENT_TITLES fixture is stale: {title!r} no longer "
                f"diverges (raw={job.dedup_key!r} == persisted={parsed.dedup_key!r}). "
                f"Pick a different title that still exercises a clean_title rule."
            )


# ---------------------------------------------------------------------------
# All four ingestion-class append sites enqueue result.dedup_key (persisted),
# not job.dedup_key (raw).
# ---------------------------------------------------------------------------
#
# This is a static guard. The fix lives at four call sites; if any of them
# regress back to ``append(job.dedup_key)``, this test breaks at the file
# scan stage with a clear pointer to the offending file.


class TestNoAppendJobDedupKey:
    """The four append sites must enqueue the PERSISTED key, not the raw one.

    The invariant: ``new_job_keys`` is consumed by ``run_scoring`` via
    ``SELECT ... WHERE dedup_key = ?``. The key must be the one ``upsert_job``
    actually wrote (``result.dedup_key``), not one recomputed from the raw
    ``Job`` (which can diverge via ``clean_title``). A grep regression would
    silently lose ~14% of inserted jobs from inline scoring.
    """

    _APPEND_SITES = [
        "job_finder/web/ingestion_runner.py",
        "job_finder/web/careers_crawler/_persistence.py",
        "job_finder/web/ats_scanner/_run.py",
        "job_finder/web/ats_scanner/_run_html.py",
    ]

    def test_no_site_appends_raw_job_dedup_key(self):
        """No append site should reintroduce ``append(job.dedup_key)``.

        Reads each source file and asserts the divergent-key pattern is
        absent. The persisted key (``result.dedup_key``) is what ``upsert_job``
        returned and is the only safe input for ``run_scoring``.
        """
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        offenders: list[str] = []
        for rel in self._APPEND_SITES:
            content = (repo_root / rel).read_text(encoding="utf-8")
            if "append(job.dedup_key)" in content:
                offenders.append(rel)
        assert not offenders, (
            "#223 regression: the following files append the RAW Job.dedup_key "
            f"to new_job_keys instead of the persisted result.dedup_key: {offenders}. "
            "run_scoring will silently skip every inserted job whose raw title "
            "carries a req-id / dash-suffix / trailing-location / logo-letter marker."
        )
