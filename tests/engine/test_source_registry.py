"""Unit tests for the opaque-redirect source registry (Section 1 of
docs/superpowers/specs/2026-07-08-job-listing-verification-design.md)."""

from __future__ import annotations

import sqlite3

from jobcannon.engine.source_registry import (
    UNVERIFIABLE_EVIDENCE_CEILING,
    UNVERIFIABLE_EVIDENCE_CONFIRMED,
    UNVERIFIABLE_EVIDENCE_PREFIX,
    is_opaque_redirect_source,
    is_opaque_redirect_url,
    is_unverifiable_candidate,
)

_CONFIG = {
    "verification": {
        "opaque_redirect_sources": [
            {"source_tag": "portal_jooble", "domain": "jooble.org"},
            {"source_tag": "portal_adzuna", "domain": "adzuna.com"},
            {"domain": "engage.indeed.com"},
            {"domain": "cts.indeed.com"},
            {"domain": "indeed.com", "path": "rc/clk"},
            {"domain": "indeed.com", "path": "pagead/clk"},
            {"domain": "click.monster.com"},
        ]
    }
}


class TestIsOpaqueRedirectUrl:
    def test_matches_jooble_domain(self):
        assert is_opaque_redirect_url("https://jooble.org/away/12345", _CONFIG)

    def test_matches_adzuna_subdomain(self):
        assert is_opaque_redirect_url("https://www.adzuna.com/details/999", _CONFIG)

    def test_matches_indeed_rc_clk_path_scoped_entry(self):
        assert is_opaque_redirect_url("https://www.indeed.com/rc/clk/dl?jk=abc", _CONFIG)

    def test_matches_indeed_pagead_clk_path_scoped_entry(self):
        """indeed.com/pagead/clk/dl is the Match-email (multi-job) tracking
        redirect shape (job_finder/parsers/indeed_parser.py:62-64) — a
        distinct path from rc/clk/dl's single-job Alert-email shape, both
        tagged source="indeed" with no distinguishing tag between them."""
        assert is_opaque_redirect_url("https://www.indeed.com/pagead/clk/dl?jk=xyz", _CONFIG)

    def test_rejects_indeed_domain_outside_scoped_paths(self):
        assert not is_opaque_redirect_url("https://www.indeed.com/viewjob?jk=abc", _CONFIG)

    def test_matches_monster_click_domain(self):
        assert is_opaque_redirect_url("https://click.monster.com/xyz", _CONFIG)

    def test_rejects_ats_domain(self):
        assert not is_opaque_redirect_url("https://jobs.lever.co/acme/1", _CONFIG)

    def test_rejects_lookalike_host(self):
        assert not is_opaque_redirect_url("https://jooble.org.evil.example/away/1", _CONFIG)

    def test_empty_url_returns_false(self):
        assert not is_opaque_redirect_url("", _CONFIG)
        assert not is_opaque_redirect_url(None, _CONFIG)

    def test_empty_registry_matches_nothing(self):
        assert not is_opaque_redirect_url("https://jooble.org/away/1", {})


class TestIsOpaqueRedirectSource:
    def test_solo_jooble_is_opaque(self):
        job = {"sources": ["portal_jooble"], "source_urls": ["https://jooble.org/away/1"]}
        assert is_opaque_redirect_source(job, _CONFIG)

    def test_solo_adzuna_is_opaque(self):
        job = {"sources": ["portal_adzuna"], "source_urls": ["https://www.adzuna.com/details/1"]}
        assert is_opaque_redirect_source(job, _CONFIG)

    def test_solo_indeed_tag_less_provenance_is_opaque_via_url(self):
        job = {"sources": ["indeed"], "source_urls": ["https://engage.indeed.com/f/a/xyz"]}
        assert is_opaque_redirect_source(job, _CONFIG)

    def test_solo_monster_is_opaque(self):
        job = {"sources": ["monster"], "source_urls": ["https://click.monster.com/abc"]}
        assert is_opaque_redirect_source(job, _CONFIG)

    def test_jooble_plus_greenhouse_dedup_is_not_opaque(self):
        """The population fix 2a targets: a real ATS sighting merged in via
        dedup must disqualify the job, even though a Jooble sighting is also
        present."""
        job = {
            "sources": ["portal_jooble", "greenhouse"],
            "source_urls": [
                "https://jooble.org/away/1",
                "https://boards.greenhouse.io/acme/jobs/1",
            ],
        }
        assert not is_opaque_redirect_source(job, _CONFIG)

    def test_jooble_plus_adzuna_both_opaque_still_opaque(self):
        job = {
            "sources": ["portal_jooble", "portal_adzuna"],
            "source_urls": [
                "https://jooble.org/away/1",
                "https://www.adzuna.com/details/2",
            ],
        }
        assert is_opaque_redirect_source(job, _CONFIG)

    def test_no_sources_returns_false(self):
        assert not is_opaque_redirect_source({"sources": [], "source_urls": []}, _CONFIG)

    def test_tolerates_json_string_columns(self):
        job = {"sources": '["portal_jooble"]', "source_urls": '["https://jooble.org/away/1"]'}
        assert is_opaque_redirect_source(job, _CONFIG)

    def test_sqlite_row_input(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (sources TEXT, source_urls TEXT)")
        conn.execute(
            "INSERT INTO t VALUES (?, ?)",
            ('["portal_jooble"]', '["https://jooble.org/away/1"]'),
        )
        row = conn.execute("SELECT * FROM t").fetchone()
        assert is_opaque_redirect_source(row, _CONFIG)
        conn.close()


# TestConfigExampleRegistryLoads (private repo) intentionally NOT ported: it
# guards the registry module against drift from the private repo's shipped
# config.example.yaml, which is a host-app concern (API keys, profile
# targets) that has no equivalent file in the engine repo.


class TestUnverifiableEvidenceConstants:
    def test_confirmed_and_ceiling_share_the_prefix(self):
        assert UNVERIFIABLE_EVIDENCE_CONFIRMED.startswith(UNVERIFIABLE_EVIDENCE_PREFIX)
        assert UNVERIFIABLE_EVIDENCE_CEILING.startswith(UNVERIFIABLE_EVIDENCE_PREFIX)

    def test_confirmed_and_ceiling_are_distinct(self):
        assert UNVERIFIABLE_EVIDENCE_CONFIRMED != UNVERIFIABLE_EVIDENCE_CEILING

    def test_unrelated_evidence_does_not_match_prefix(self):
        assert not "not_seen_30_days".startswith(UNVERIFIABLE_EVIDENCE_PREFIX)
        assert not "re_appeared".startswith(UNVERIFIABLE_EVIDENCE_PREFIX)


# ---------- Shared unverifiable-candidate predicate (job-listing-verification Plan 3) ----------


class TestIsUnverifiableCandidate:
    def test_opaque_source_no_direct_url_is_candidate(self):
        job = {
            "sources": ["portal_jooble"],
            "source_urls": ["https://jooble.org/away/1"],
            "direct_url": None,
        }
        assert is_unverifiable_candidate(job, _CONFIG)

    def test_opaque_source_with_direct_url_is_not_candidate(self):
        """Already corroborated — direct_url is set — never a candidate,
        regardless of source."""
        job = {
            "sources": ["portal_jooble"],
            "source_urls": ["https://jooble.org/away/1"],
            "direct_url": "https://jobs.lever.co/acme/1",
        }
        assert not is_unverifiable_candidate(job, _CONFIG)

    def test_non_opaque_source_is_not_candidate(self):
        job = {
            "sources": ["greenhouse"],
            "source_urls": ["https://boards.greenhouse.io/acme/jobs/1"],
            "direct_url": None,
        }
        assert not is_unverifiable_candidate(job, _CONFIG)

    def test_mixed_opaque_and_real_source_is_not_candidate(self):
        """A Jooble/Greenhouse dedup — the population fix 2a targets — is
        never unverifiable, matching is_opaque_redirect_source's own
        equivalent pin."""
        job = {
            "sources": ["portal_jooble", "greenhouse"],
            "source_urls": [
                "https://jooble.org/away/1",
                "https://boards.greenhouse.io/acme/jobs/1",
            ],
            "direct_url": None,
        }
        assert not is_unverifiable_candidate(job, _CONFIG)

    def test_tolerates_missing_direct_url_key(self):
        """A plain dict/Row that simply lacks a direct_url key (rather than
        carrying an explicit None) is treated as NULL, not as an error."""
        job = {"sources": ["portal_jooble"], "source_urls": ["https://jooble.org/away/1"]}
        assert is_unverifiable_candidate(job, _CONFIG)
