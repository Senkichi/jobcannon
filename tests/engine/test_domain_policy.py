"""Unit tests for jobcannon.engine.domain_policy.

Covers: is_blocked_domain(), domain_priority(), PRIORITY_DOMAINS type assertion,
and LinkedIn exclusion from BLOCKED_DOMAINS.
"""

from jobcannon.engine.domain_policy import (
    BLOCKED_DOMAINS,
    PRIORITY_DOMAINS,
    domain_priority,
    is_aggregator_or_job_board,
    is_blocked_domain,
)

# ---------------------------------------------------------------------------
# BLOCKED_DOMAINS membership
# ---------------------------------------------------------------------------


class TestBlockedDomainsMembership:
    # Exact-value frozenset membership (subset comparison), not a substring/URL
    # check -- is_blocked_domain() below does the real hostname-boundary
    # matching. Written as `{"x"} <= BLOCKED_DOMAINS` rather than `"x" in
    # BLOCKED_DOMAINS`: CodeQL's py/incomplete-url-substring-sanitization query
    # pattern-matches any `"domain-like literal" in <expr>`, regardless of
    # container type, and false-positived on the `in` form here.
    def test_glassdoor_com_blocked(self):
        assert {"glassdoor.com"} <= BLOCKED_DOMAINS

    def test_glassdoor_co_uk_blocked(self):
        assert {"glassdoor.co.uk"} <= BLOCKED_DOMAINS

    def test_indeed_com_blocked(self):
        assert {"indeed.com"} <= BLOCKED_DOMAINS

    def test_ziprecruiter_blocked(self):
        assert {"ziprecruiter.com"} <= BLOCKED_DOMAINS

    def test_dice_blocked(self):
        assert {"dice.com"} <= BLOCKED_DOMAINS

    def test_linkedin_NOT_blocked(self):
        """LinkedIn must NOT be in BLOCKED_DOMAINS — fetch_linkedin_jd() handles it."""
        assert "linkedin.com" not in BLOCKED_DOMAINS

    def test_blocked_domains_is_frozenset(self):
        assert isinstance(BLOCKED_DOMAINS, frozenset)


# ---------------------------------------------------------------------------
# PRIORITY_DOMAINS type
# ---------------------------------------------------------------------------


class TestPriorityDomainsType:
    def test_priority_domains_is_list(self):
        """PRIORITY_DOMAINS must be a list — domain_priority() uses enumerate() on it."""
        assert isinstance(PRIORITY_DOMAINS, list)

    def test_priority_domains_not_empty(self):
        assert len(PRIORITY_DOMAINS) > 0

    def test_greenhouse_has_higher_priority_than_linkedin(self):
        """ATS platforms should be higher priority (lower index) than LinkedIn."""
        idx_greenhouse = PRIORITY_DOMAINS.index("greenhouse.io")
        idx_linkedin = PRIORITY_DOMAINS.index("linkedin.com/jobs")
        assert idx_greenhouse < idx_linkedin


# ---------------------------------------------------------------------------
# is_blocked_domain()
# ---------------------------------------------------------------------------


class TestIsBlockedDomain:
    def test_glassdoor_full_url(self):
        assert is_blocked_domain("https://www.glassdoor.com/job/12345") is True

    def test_glassdoor_co_uk_full_url(self):
        assert is_blocked_domain("https://www.glassdoor.co.uk/job/12345") is True

    def test_indeed_full_url(self):
        assert is_blocked_domain("https://www.indeed.com/viewjob?jk=abc") is True

    def test_ziprecruiter_full_url(self):
        assert is_blocked_domain("https://www.ziprecruiter.com/jobs/some-job") is True

    def test_dice_full_url(self):
        assert is_blocked_domain("https://www.dice.com/jobs/detail/abc") is True

    def test_linkedin_NOT_blocked(self):
        """LinkedIn URLs must pass through (handled by fetch_linkedin_jd)."""
        assert is_blocked_domain("https://www.linkedin.com/jobs/view/12345") is False

    def test_greenhouse_not_blocked(self):
        assert is_blocked_domain("https://boards.greenhouse.io/company/jobs/123") is False

    def test_case_insensitivity(self):
        """URL matching is case-insensitive."""
        assert is_blocked_domain("https://GLASSDOOR.COM/job/123") is True

    def test_empty_string_returns_false(self):
        assert is_blocked_domain("") is False

    def test_subdomain_match(self):
        """Subdomain variants of blocked domains are also blocked."""
        assert is_blocked_domain("https://jobs.indeed.com/view/123") is True

    def test_unrelated_domain_not_blocked(self):
        assert is_blocked_domain("https://www.example.com/jobs/data-scientist") is False

    def test_lookalike_host_prefix_not_blocked(self):
        """A host that merely CONTAINS a blocked domain as a substring — not a
        real subdomain — must NOT be blocked (py/incomplete-url-substring-
        sanitization guard)."""
        assert is_blocked_domain("https://notindeed.com/jobs") is False

    def test_blocked_domain_embedded_in_attacker_host_not_blocked(self):
        """A blocked domain appearing as a prefix of an unrelated registrable
        domain (not a real subdomain of it) must NOT be blocked."""
        assert is_blocked_domain("https://indeed.com.evil.example/jobs") is False


# ---------------------------------------------------------------------------
# domain_priority()
# ---------------------------------------------------------------------------


class TestDomainPriority:
    def test_greenhouse_has_priority_below_100(self):
        assert domain_priority("https://boards.greenhouse.io/company/jobs/123") < 100

    def test_lever_has_priority_below_100(self):
        assert domain_priority("https://jobs.lever.co/company/abc") < 100

    def test_unknown_domain_returns_100(self):
        assert domain_priority("https://www.example.com/jobs/engineer") == 100

    def test_empty_url_returns_100(self):
        assert domain_priority("") == 100

    def test_greenhouse_higher_priority_than_builtin(self):
        """Greenhouse (ATS) should rank higher (lower int) than builtin.com."""
        p_greenhouse = domain_priority("https://boards.greenhouse.io/company/jobs/1")
        p_builtin = domain_priority("https://builtin.com/job/company/role/123")
        assert p_greenhouse < p_builtin

    def test_priority_ordering_is_consistent_with_list(self):
        """domain_priority index must match position in PRIORITY_DOMAINS."""
        for expected_idx, domain in enumerate(PRIORITY_DOMAINS):
            url = f"https://{domain}/some/path"
            assert domain_priority(url) == expected_idx

    def test_priority_domain_not_spoofed_by_query_param(self):
        """A priority domain embedded in an unrelated URL's query string must
        NOT grant ATS priority (py/incomplete-url-substring-sanitization guard)."""
        assert domain_priority("https://evil.example/redirect?url=greenhouse.io/apply") == 100

    def test_priority_domain_not_spoofed_by_lookalike_host(self):
        """A look-alike host that merely embeds a priority domain must not match."""
        assert domain_priority("https://greenhouse.io.evil.example/apply") == 100

    def test_linkedin_jobs_path_matches_composite_entry(self):
        """The composite 'linkedin.com/jobs' entry still matches host+path pairs."""
        idx = domain_priority("https://www.linkedin.com/jobs/view/123")
        assert idx == PRIORITY_DOMAINS.index("linkedin.com/jobs")

    def test_linkedin_non_jobs_path_does_not_match_jobs_entry(self):
        """linkedin.com URLs outside /jobs must not match the 'linkedin.com/jobs' entry."""
        assert domain_priority("https://www.linkedin.com/company/acme") == 100

    def test_linkedin_jobs_path_prefix_without_segment_boundary_does_not_match(self):
        """A path that merely STARTS WITH 'jobs' as a string (e.g. 'jobs-xyz', not
        the '/jobs' segment) must not match the 'linkedin.com/jobs' entry."""
        assert domain_priority("https://www.linkedin.com/jobsxyz/view/123") == 100


# ---------------------------------------------------------------------------
# is_aggregator_or_job_board() — the careers-page-discovery negative gate
# ---------------------------------------------------------------------------


class TestIsAggregatorOrJobBoard:
    """The union predicate careers_scraper consults: BLOCKED_DOMAINS PLUS the
    non-ATS job boards (linkedin, builtin, ...) that is_blocked_domain omits."""

    def test_linkedin_flagged(self):
        """LinkedIn is flagged here even though is_blocked_domain lets it pass."""
        assert is_aggregator_or_job_board("https://www.linkedin.com/company/acme/jobs/") is True

    def test_builtin_flagged(self):
        """builtin.com is a job board, not an employer's own careers site."""
        assert is_aggregator_or_job_board("https://builtin.com/jobs/acme") is True

    def test_glassdoor_flagged(self):
        assert is_aggregator_or_job_board("https://www.glassdoor.com/Jobs/acme-jobs.htm") is True

    def test_indeed_flagged(self):
        assert is_aggregator_or_job_board("https://www.indeed.com/cmp/acme/jobs") is True

    def test_ziprecruiter_flagged(self):
        assert is_aggregator_or_job_board("https://www.ziprecruiter.com/co/acme/jobs") is True

    def test_workingnomads_flagged(self):
        assert is_aggregator_or_job_board("https://www.workingnomads.com/jobs") is True

    def test_ycombinator_flagged(self):
        assert is_aggregator_or_job_board("https://www.ycombinator.com/companies/acme") is True

    def test_employer_own_site_not_flagged(self):
        """A company's own domain/path is never an aggregator."""
        assert is_aggregator_or_job_board("https://acme.com/careers") is False

    def test_employer_careers_subdomain_not_flagged(self):
        assert is_aggregator_or_job_board("https://careers.acme.com/") is False

    def test_ats_host_not_flagged(self):
        """Legit ATS hosts are not aggregators (they're handled separately)."""
        assert is_aggregator_or_job_board("https://boards.greenhouse.io/acme/jobs/1") is False

    def test_subdomain_match(self):
        assert is_aggregator_or_job_board("https://jobs.indeed.com/view/123") is True

    def test_case_insensitive(self):
        assert is_aggregator_or_job_board("https://WWW.LINKEDIN.COM/JOBS/") is True

    def test_empty_string_returns_false(self):
        assert is_aggregator_or_job_board("") is False

    def test_lookalike_host_not_flagged(self):
        """A host that merely embeds a flagged domain as a substring (not a real
        subdomain) must NOT be flagged — py/incomplete-url-substring guard."""
        assert is_aggregator_or_job_board("https://notlinkedin.com/jobs") is False

    def test_flagged_domain_embedded_in_attacker_host_not_flagged(self):
        assert is_aggregator_or_job_board("https://linkedin.com.evil.example/jobs") is False

    def test_diverges_from_is_blocked_domain_on_linkedin(self):
        """Documents the deliberate divergence: is_blocked_domain must keep
        letting LinkedIn through (for JD fetching), while this predicate blocks
        it (for careers_url discovery)."""
        url = "https://www.linkedin.com/jobs/view/123"
        assert is_blocked_domain(url) is False
        assert is_aggregator_or_job_board(url) is True
