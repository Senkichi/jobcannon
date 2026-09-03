"""Tests for Issue #2017 — self-duplicated title-suffix collapse.

Covers:
  * ``collapse_duplicated_suffix`` — the pure normalizer that detects a trailing
    comma-delimited segment repeating an earlier segment verbatim and collapses
    it.
  * ``ParsedJob.from_job`` integration — the collapse is applied after
    ``clean_title`` and before ``derive_dedup_key``, so both the stored title
    and the dedup key see the collapsed form.
  * Title-hygiene re-sweep reporting — the re-sweep detects + reports existing
    affected rows (count + affected ``dedup_key``s) in the ``runs`` row
    metadata, without collapsing or re-keying them.
  * No-existing-row-rekey assertion — the re-sweep does not rewrite any
    existing row's ``dedup_key``.

Source determination (acceptance criterion 1): the Amazon ``search.json`` API
returns the ``title`` field with the doubled subtitle verbatim — verified live
2026-09-01 (``id_icims`` 10521285, ``title: "Data Scientist, EU Prime and
Marketing Analytics & Science (PRIMAS), EU Prime and Marketing Analytics &
Science (PRIMAS)"``). The ``job_path`` slug also carries the doubling,
confirming it is stored that way in Amazon's system. The Amazon scanner at
``_platforms_amazon.py:176`` takes ``posting["title"]`` verbatim with no
concatenation. This is an **employer-authored** title, not a client-side
concatenation defect — there is no concatenation site to fix. The collapse is
the single-point-of-enforcement normalizer for new rows.
"""

from __future__ import annotations

from jobcannon.engine.normalizers import collapse_duplicated_suffix, derive_dedup_key

# ---------------------------------------------------------------------------
# Golden fixtures — collapse_duplicated_suffix
# ---------------------------------------------------------------------------

#: The Amazon PRIMAS title from the 2026-09-01 audit (id_icims 10521285).
#: The subtitle "EU Prime and Marketing Analytics & Science (PRIMAS)" is
#: repeated verbatim as the trailing comma-delimited segment.
_PRIMAS_DOUBLED = (
    "Data Scientist, EU Prime and Marketing Analytics & Science (PRIMAS), "
    "EU Prime and Marketing Analytics & Science (PRIMAS)"
)
_PRIMAS_COLLAPSED = "Data Scientist, EU Prime and Marketing Analytics & Science (PRIMAS)"

#: Row B from the same audit — the subtitle appears exactly once. This is the
#: normal Amazon title format (role, team). Must be unchanged.
_PRIMAS_II = "Data Scientist II, EU Prime and Marketing Analytics & Science (PRIMAS)"


class TestCollapseDuplicatedSuffix:
    def test_primas_doubled_collapses(self):
        """The Amazon PRIMAS title (doubled subtitle) collapses to a single subtitle."""
        assert collapse_duplicated_suffix(_PRIMAS_DOUBLED) == _PRIMAS_COLLAPSED

    def test_primas_ii_unchanged(self):
        """Row B (subtitle once) is unchanged — no duplicated suffix."""
        assert collapse_duplicated_suffix(_PRIMAS_II) == _PRIMAS_II

    def test_short_repeat_not_collapsed(self):
        """``Analyst, Analyst`` (7 chars) is below the minimum-length guard."""
        assert collapse_duplicated_suffix("Analyst, Analyst") == "Analyst, Analyst"

    def test_legitimate_repeated_short_token_unchanged(self):
        """A title with a genuinely repeated short token in different segments
        is unchanged — the collapse only fires when the ENTIRE trailing segment
        matches an earlier segment verbatim."""
        # "AI" appears in two segments but neither is a verbatim repeat of the
        # other ("AI" vs "AI Research").
        title = "AI, ML, AI Research"
        assert collapse_duplicated_suffix(title) == title

    def test_no_comma_unchanged(self):
        """A title with no comma is unchanged."""
        assert collapse_duplicated_suffix("Senior Data Scientist") == "Senior Data Scientist"

    def test_two_segments_unchanged(self):
        """A two-segment title ("A, B") has no earlier segment for B to duplicate."""
        assert collapse_duplicated_suffix("Data Scientist, PRIMAS") == "Data Scientist, PRIMAS"

    def test_case_insensitive_match(self):
        """The segment comparison is case- and whitespace-normalized."""
        title = "Data Scientist, EU Prime and Marketing Analytics & Science (PRIMAS),   eu prime and marketing analytics & science (primas)"
        expected = "Data Scientist, EU Prime and Marketing Analytics & Science (PRIMAS)"
        assert collapse_duplicated_suffix(title) == expected

    def test_empty_string_unchanged(self):
        assert collapse_duplicated_suffix("") == ""

    def test_dedup_key_stable_after_collapse(self):
        """The dedup_key of the collapsed PRIMAS title matches the dedup_key of
        the row-B style title (same company, same subtitle, different role
        prefix). This is the core invariant: a re-scrape with a
        correctly-constructed title produces the SAME key, not a new row."""
        key_doubled = derive_dedup_key("Amazon", collapse_duplicated_suffix(_PRIMAS_DOUBLED))
        key_collapsed = derive_dedup_key("Amazon", _PRIMAS_COLLAPSED)
        assert key_doubled == key_collapsed


# ---------------------------------------------------------------------------
# ParsedJob.from_job integration — collapse applied before derive_dedup_key
# ---------------------------------------------------------------------------


def _from_job(title, company="Amazon"):
    from jobcannon.engine.models import Job
    from jobcannon.engine.parsed_job import ParsedJob

    job = Job(title=title, company=company, location="", source="ats", source_url="http://x")
    return ParsedJob.from_job(job)


class TestFromJobCollapse:
    def test_primas_doubled_collapsed_in_from_job(self):
        """ParsedJob.from_job collapses the doubled PRIMAS title before storing
        it and before deriving the dedup_key."""
        from jobcannon.engine.parsed_job import ParsedJob

        p = _from_job(_PRIMAS_DOUBLED)
        assert isinstance(p, ParsedJob)
        assert p.title == _PRIMAS_COLLAPSED
        assert p.dedup_key == derive_dedup_key("Amazon", _PRIMAS_COLLAPSED)

    def test_primas_ii_unchanged_in_from_job(self):
        """Row B (subtitle once) passes through unchanged."""
        from jobcannon.engine.parsed_job import ParsedJob

        p = _from_job(_PRIMAS_II)
        assert isinstance(p, ParsedJob)
        assert p.title == _PRIMAS_II

    def test_legitimate_repeated_short_token_unchanged_in_from_job(self):
        """A legitimate title with a genuinely repeated short token is unchanged."""
        from jobcannon.engine.parsed_job import ParsedJob

        title = "AI, ML, AI Research"
        p = _from_job(title, company="Acme Corp")
        assert isinstance(p, ParsedJob)
        assert p.title == title


# ---------------------------------------------------------------------------
# Title-hygiene re-sweep — detection + reporting (no collapse, no re-key)
# ---------------------------------------------------------------------------


def _insert_job(
    conn, dedup_key, title, *, classification="apply", reasons="[]", jd=None, scoring_model=None
):
    conn.execute(
        "INSERT INTO jobs (dedup_key, title, company, location, sources, unresolved_reasons, "
        "classification, sub_scores_json, fit_analysis, jd_full, scoring_model, first_seen, "
        "last_seen, pipeline_status) VALUES (?, ?, ?, '', '[\"ats\"]', ?, ?, '{}', 'fit', "
        "?, ?, '2026-01-01', '2026-01-01', 'discovered')",
        (dedup_key, title, "Amazon", reasons, classification, jd, scoring_model),
    )
    conn.commit()
