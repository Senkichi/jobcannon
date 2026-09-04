# PORTED from tests/test_linkedin_parser.py @ e6ff9bf3c0f5b7505b127e6b6a0ca945382831be (private job-cannon). Ledger L-0556.
"""Tests for linkedin_parser.py — LinkedIn job alert email parsing.

Covers:
- Meta-email notification filter (DQ-04)
- Normal job alert parsing
"""

from jobcannon.engine.email_parsers.linkedin_parser import _is_meta_email, parse_linkedin_alert


class TestNotificationFilter:
    """LinkedIn notification emails are rejected before parsing (DQ-04)."""

    def test_notification_email_rejected(self):
        body = "You'll receive notifications when new jobs match your search criteria."
        result = parse_linkedin_alert(body)
        assert result == []

    def test_notification_email_case_insensitive(self):
        body = "YOU'LL RECEIVE NOTIFICATIONS WHEN NEW JOBS MATCH..."
        result = parse_linkedin_alert(body)
        assert result == []

    def test_normal_job_alert_not_rejected(self):
        body = (
            "Senior Data Scientist\n"
            "Acme Corp\n"
            "San Francisco, CA\n\n"
            "View job: https://www.linkedin.com/comm/jobs/view/12345/tracking\n"
            "-" * 40
        )
        result = parse_linkedin_alert(body)
        assert len(result) >= 1
        assert result[0].title == "Senior Data Scientist"

    def test_is_meta_email_detects_notification(self):
        preamble = "You'll receive notifications when new jobs match your alert."
        assert _is_meta_email(preamble) is True

    def test_is_meta_email_passes_normal_alert(self):
        preamble = "Senior Data Scientist at Acme Corp in San Francisco"
        assert _is_meta_email(preamble) is False

    def test_new_digest_format_with_count_preamble_not_meta(self):
        """LinkedIn new AI-powered digest: count preamble + real job listings → not meta."""
        sep = "-" * 57
        body = "\n".join(
            [
                "Your job alert for product data scientist in San Francisco",
                "",
                "30+ new jobs match your preferences.",
                "Manage alerts: https://www.linkedin.com/comm/jobs/alerts",
                "",
                "Results from the new AI-powered job search",
                "",
                "Data Scientist, People Innovation",
                "OpenAI",
                "San Francisco, CA",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/4382507866/",
                sep,
            ]
        )
        assert _is_meta_email(body) is False

    def test_new_digest_format_parses_jobs(self):
        """The full new digest format should yield job objects, not be skipped."""
        sep = "-" * 57
        body = "\n".join(
            [
                "Your job alert for product data scientist in San Francisco",
                "",
                "30+ new jobs match your preferences.",
                "Manage alerts: https://www.linkedin.com/comm/jobs/alerts",
                "",
                "Results from the new AI-powered job search",
                "",
                "Data Scientist, People Innovation",
                "OpenAI",
                "San Francisco, CA",
                "",
                "This company is actively hiring",
                "View job: https://www.linkedin.com/comm/jobs/view/4382507866/tracking",
                sep,
                "",
                "Senior Data Scientist",
                "Netflix",
                "United States",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/4382524355/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 2
        assert result[0].title == "Data Scientist, People Innovation"
        assert result[0].company == "OpenAI"
        assert result[1].title == "Senior Data Scientist"


class TestFooterContaminationFilter:
    """Navigation/footer lines in a block must not become job titles (DQ-05)."""

    def test_see_all_jobs_header_filtered(self):
        """'See all jobs' navigation link above a job listing should be stripped."""
        sep = "-" * 40
        body = "\n".join(
            [
                "See all jobs on LinkedIn: https://www.linkedin.com/comm/jobs/search-results/?keywords=analytics",
                "",
                "Senior Data Scientist",
                "Netflix",
                "United States",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/4382524355/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Data Scientist"
        assert result[0].company == "Netflix"

    def test_view_all_jobs_header_filtered(self):
        """'View all jobs' navigation link above a job listing should be stripped."""
        sep = "-" * 40
        body = "\n".join(
            [
                "View all jobs: https://www.linkedin.com/jobs/search-results/?keywords=data+scientist",
                "",
                "Staff Data Scientist",
                "Acme Corp",
                "San Francisco, CA",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/111222333/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Staff Data Scientist"

    def test_url_only_line_filtered(self):
        """A bare URL line at the top of a block should be stripped."""
        sep = "-" * 40
        body = "\n".join(
            [
                "https://www.linkedin.com/comm/jobs/some-stray-link",
                "",
                "Data Analyst",
                "Stripe",
                "Remote",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/999888777/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Data Analyst"

    def test_url_in_title_after_filter_returns_none(self):
        """A title that still contains a URL after filtering is rejected entirely."""
        sep = "-" * 40
        body = "\n".join(
            [
                "Check this out: https://example.com/some-embedded-url in the title",
                "Some Company",
                "Remote",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/123456789/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert result == []


class TestHTMLInPlainText:
    """LinkedIn injects HTML tags into text/plain body for section headers.

    The 'New jobs from your other alerts' section embeds raw <strong> tags
    as section titles.  These must be stripped so the actual job title, company,
    and location are parsed from the correct lines.
    """

    def test_strong_tag_section_header_stripped(self):
        """<strong>…</strong> section header above a job block must not become the title."""
        sep = "-" * 57
        body = "\n".join(
            [
                "Your job alert for senior data scientist",
                "",
                "30+ new jobs match your preferences.",
                "",
                "Senior Data Scientist, Analytics",
                "Discord",
                "San Francisco Bay Area",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/4388983970/tracking",
                sep,
                "",
                "New jobs from your other alerts",
                "",
                '<strong class="font-bold" style="font-weight: 600;">product data scientist</strong>',
                "",
                "Data Scientist, Customer Analytics",
                "Cresta",
                "United States",
                "View job: https://www.linkedin.com/comm/jobs/view/4364044356/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 2
        assert result[0].title == "Senior Data Scientist, Analytics"
        assert result[0].company == "Discord"
        assert result[1].title == "Data Scientist, Customer Analytics"
        assert result[1].company == "Cresta"
        assert result[1].location == "United States"

    def test_strong_tag_with_query_description(self):
        """<strong> header containing search query text is filtered."""
        sep = "-" * 57
        body = "\n".join(
            [
                '<strong class="font-bold" style="font-weight: 600;">'
                "senior data scientist posted in the last week"
                "</strong>",
                "",
                "Data Scientist, Platform and B2B Products",
                "OpenAI",
                "San Francisco, CA",
                "",
                "This company is actively hiring",
                "View job: https://www.linkedin.com/comm/jobs/view/4306142538/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Data Scientist, Platform and B2B Products"
        assert result[0].company == "OpenAI"
        assert result[0].location == "San Francisco, CA"

    def test_strong_tag_jobs_in_area(self):
        """<strong>Data Scientist</strong> jobs in Bay Area is filtered."""
        sep = "-" * 57
        body = "\n".join(
            [
                '<strong class="font-bold" style="font-weight: 600;">'
                "Data Scientist</strong> jobs in San Francisco Bay Area",
                "",
                "Senior Data Scientist",
                "Abridge",
                "San Francisco",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/1234567/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Data Scientist"
        assert result[0].company == "Abridge"

    def test_html_only_block_produces_no_valid_job(self):
        """A block where the only real content is HTML produces no valid job
        with a recognizable title — HTML line is filtered, leaving insufficient data."""
        sep = "-" * 40
        body = "\n".join(
            [
                '<div style="color: red;">Garbage Title</div>',
                "View job: https://www.linkedin.com/comm/jobs/view/999/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert result == []

    def test_html_title_falls_through_to_sanity_check(self):
        """If an HTML line somehow survives filtering, the sanity check rejects it."""
        # Simulate a novel HTML pattern that doesn't match existing filters
        # but does contain < and > and style=
        sep = "-" * 40
        body = "\n".join(
            [
                '<span style="font-size:12px">Novel HTML Pattern</span>',
                "Some Company",
                "Remote",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/999/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        # The <span> line is filtered by the generic HTML check, leaving
        # only "Some Company" and "Remote" — not a real job block
        assert len(result) <= 1  # At most a residual 2-line block
        if result:
            assert "<" not in result[0].title


class TestFacetSuggestionsSectionHeaders:
    """jobs-noreply 'Expand your search' emails have section category names
    ('Medical jobs', 'AI/ML jobs', 'Remote jobs') that bleed into job blocks.

    Without filtering, these become title and rotate all fields by one position:
      title='Medical jobs', company=actual_title, location=actual_company.
    """

    def test_medical_jobs_section_header_stripped(self):
        sep = "-" * 57
        body = "\n".join(
            [
                "Expand your search",
                "",
                "Recommendations based on your activity.",
                "",
                "Medical jobs",
                "https://www.linkedin.com/jobs/search?q=medical",
                "",
                "Senior Data Scientist, Search Health",
                "Google",
                "Mountain View",
                "View job: https://www.linkedin.com/comm/jobs/view/4388984872/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Data Scientist, Search Health"
        assert result[0].company == "Google"
        assert result[0].location == "Mountain View"

    def test_ai_ml_jobs_section_header_stripped(self):
        sep = "-" * 57
        body = "\n".join(
            [
                "AI/ML jobs",
                "https://www.linkedin.com/jobs/search?q=ai",
                "",
                "Senior AI Scientist",
                "Intuit",
                "Mountain View, CA",
                "View job: https://www.linkedin.com/comm/jobs/view/4401738126/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior AI Scientist"
        assert result[0].company == "Intuit"

    def test_remote_jobs_section_header_stripped(self):
        sep = "-" * 57
        body = "\n".join(
            [
                "Remote jobs",
                "https://www.linkedin.com/jobs/search?q=remote",
                "",
                "Senior Data Scientist II",
                "Alma",
                "United States",
                "View job: https://www.linkedin.com/comm/jobs/view/4389267750/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Data Scientist II"
        assert result[0].company == "Alma"

    def test_medical_devices_jobs_header_stripped(self):
        sep = "-" * 57
        body = "\n".join(
            [
                "Medical Devices jobs",
                "https://www.linkedin.com/jobs/search?q=medical+devices",
                "",
                "Senior Data Scientist",
                "Medtronic",
                "San Jose, CA",
                "View job: https://www.linkedin.com/comm/jobs/view/9999999/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Data Scientist"
        assert result[0].company == "Medtronic"

    def test_clinical_jobs_header_stripped(self):
        """Generic '<topic> jobs' pattern catches arbitrary categories."""
        sep = "-" * 57
        body = "\n".join(
            [
                "Clinical jobs",
                "https://www.linkedin.com/jobs/search?q=clinical",
                "",
                "Research Scientist",
                "Latent",
                "San Francisco",
                "View job: https://www.linkedin.com/comm/jobs/view/4399584320/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Research Scientist"
        assert result[0].company == "Latent"

    def test_full_facet_email_multi_section(self):
        """Full jobs-noreply email with multiple sections parses all jobs correctly."""
        sep = "-" * 57
        body = "\n".join(
            [
                "Expand your search",
                "",
                "Recommendations based on your activity.",
                "",
                "Remote jobs",
                "https://www.linkedin.com/jobs/search?q=remote",
                "",
                "Senior Data Scientist II",
                "Alma",
                "United States",
                "View job: https://www.linkedin.com/comm/jobs/view/4389267750/tracking",
                sep,
                "",
                "Senior Data Scientist, Consumer",
                "Reddit, Inc.",
                "United States",
                "View job: https://www.linkedin.com/comm/jobs/view/4307901770/tracking",
                sep,
                "",
                "View all jobs: https://www.linkedin.com/jobs/search?q=remote",
                "",
                "Medical jobs",
                "https://www.linkedin.com/jobs/search?q=medical",
                "",
                "Senior Data Scientist, Search Health",
                "Google",
                "Mountain View",
                "View job: https://www.linkedin.com/comm/jobs/view/4388984872/tracking",
                sep,
                "",
                "Research Scientist",
                "Latent",
                "San Francisco",
                "View job: https://www.linkedin.com/comm/jobs/view/4399584320/tracking",
                sep,
                "",
                "View all jobs: https://www.linkedin.com/jobs/search?q=medical",
                "",
                "AI/ML jobs",
                "https://www.linkedin.com/jobs/search?q=ai",
                "",
                "Senior AI Scientist",
                "Intuit",
                "94041",
                "View job: https://www.linkedin.com/comm/jobs/view/4401738126/tracking",
                sep,
                "",
                "This email was intended for Alex Martin",
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 5
        titles = [j.title for j in result]
        companies = [j.company for j in result]
        assert "Senior Data Scientist II" in titles
        assert "Senior Data Scientist, Consumer" in titles
        assert "Senior Data Scientist, Search Health" in titles
        assert "Research Scientist" in titles
        assert "Senior AI Scientist" in titles
        # Verify no field rotation — companies must be actual company names
        assert "Alma" in companies
        assert "Reddit, Inc." in companies
        assert "Google" in companies
        assert "Latent" in companies
        assert "Intuit" in companies
        # Section headers must NOT appear as any field
        for j in result:
            assert "Medical jobs" not in j.title
            assert "Medical jobs" not in j.company
            assert "AI/ML jobs" not in j.title
            assert "Remote jobs" not in j.title


class TestJobsSimilarFilter:
    """'Jobs similar to X at Y' recommendation links must be filtered."""

    def test_jobs_similar_line_filtered(self):
        sep = "-" * 57
        body = "\n".join(
            [
                "Jobs similar to Staff Data Scientist at Toast "
                "https://www.linkedin.com/comm/jobs/view/4337163287",
                "",
                "Senior Manager, Advanced Analytics",
                "Airbnb",
                "San Francisco",
                "View job: https://www.linkedin.com/comm/jobs/view/4399999999/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Manager, Advanced Analytics"
        assert result[0].company == "Airbnb"

    def test_jobs_similar_line_stripped_from_block(self):
        """'Jobs similar' line is stripped; remaining lines form a valid job."""
        sep = "-" * 40
        body = "\n".join(
            [
                "Jobs similar to Lead Analyst at Scale AI",
                "Data Analyst",
                "Stripe",
                "San Francisco",
                "",
                "View job: https://www.linkedin.com/comm/jobs/view/111/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Data Analyst"
        assert result[0].company == "Stripe"


class TestFooterNoiseFilter:
    """Footer and upsell lines must not contaminate job blocks."""

    def test_try_premium_upsell_filtered(self):
        sep = "-" * 57
        body = "\n".join(
            [
                "Jobs where you'd be a top applicant",
                "Based on your profile, the job criteria, and recruiter feedback",
                "Unlock personalized recommendations and message recruiters directly.",
                "Try Premium for $0",
                "http://www.linkedin.com/premium/products/",
                "",
                "Senior Data Scientist",
                "Netflix",
                "Los Gatos, CA",
                "View job: https://www.linkedin.com/comm/jobs/view/555/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Senior Data Scientist"
        assert result[0].company == "Netflix"

    def test_edit_alert_filtered(self):
        sep = "-" * 40
        body = "\n".join(
            [
                "Edit alert https://www.linkedin.com/jobs/jam/manage/12345",
                "",
                "Data Analyst",
                "Stripe",
                "San Francisco",
                "View job: https://www.linkedin.com/comm/jobs/view/777/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Data Analyst"

    def test_connection_count_filtered(self):
        """'1 connection' metadata line must not become part of job fields."""
        sep = "-" * 40
        body = "\n".join(
            [
                "Staff Engineer",
                "Acme Corp",
                "San Francisco",
                "",
                "1 connection",
                "View job: https://www.linkedin.com/comm/jobs/view/888/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "Staff Engineer"
        assert result[0].company == "Acme Corp"
        assert result[0].location == "San Francisco"

    def test_apply_with_resume_filtered(self):
        """'Apply with resume & profile' metadata must not affect parsing."""
        sep = "-" * 40
        body = "\n".join(
            [
                "ML Engineer",
                "DeepMind",
                "London, UK",
                "",
                "Apply with resume & profile",
                "View job: https://www.linkedin.com/comm/jobs/view/999/tracking",
                sep,
            ]
        )
        result = parse_linkedin_alert(body)
        assert len(result) == 1
        assert result[0].title == "ML Engineer"
        assert result[0].company == "DeepMind"
