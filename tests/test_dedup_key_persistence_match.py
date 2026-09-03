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
        # PORT-SEAM (L-0008): private source neutralized I-10 by patching
        # load_config/get_company_denylist to empty. The ported engine's
        # _denylist_provider already defaults to None -> empty denylist, so
        # no patch is needed to get the same "clean" state.
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
            # PORT-SEAM (L-0008): see note above -- no denylist patch needed.
            parsed = ParsedJob.from_job(job)
            assert job.dedup_key != parsed.dedup_key, (
                f"DIVERGENT_TITLES fixture is stale: {title!r} no longer "
                f"diverges (raw={job.dedup_key!r} == persisted={parsed.dedup_key!r}). "
                f"Pick a different title that still exercises a clean_title rule."
            )


# DROPPED class TestNoAppendJobDedupKey (port L-group jobcannon/engine): this
# was a static host-layer wiring guard over four private application call
# sites (job_finder/web/ingestion_runner.py, careers_crawler/_persistence.py,
# ats_scanner/_run.py, ats_scanner/_run_html.py) -- none of which are part of
# this port group (jobcannon/engine is the pure library layer; those are
# host/ingestion wiring owned elsewhere). Re-add an equivalent guard once/if
# those call sites land in jobcannon under their own ledger rows.
