"""Tests for parallel detail fetching in run_platform_scan (issue #1028)."""

from jobcannon.engine.ats_platforms._registry import PlatformScanner, run_platform_scan


class TestParallelDetailFetch:
    """Tests for the parallel detail fetch feature added in issue #1028."""

    def test_detail_fetch_callable_invoked_for_matched_postings(self):
        """When scanner has detail_fetch, it's called for each matched posting."""
        call_log = []

        def fake_detail_fetch(posting: dict) -> dict:
            call_log.append(posting["id"])
            return {"__fetched_description": f"desc_{posting['id']}"}

        def fake_posting_to_job(posting: dict, slug: str) -> dict:
            return {
                "title": posting["title"],
                "description": posting.get("__fetched_description", ""),
            }

        scanner = PlatformScanner(
            name="test",
            company_source="Test",
            fetch_postings=lambda slug, max_pages=None: [
                {"id": "1", "title": "Job 1"},
                {"id": "2", "title": "Job 2"},
                {"id": "3", "title": "Excluded"},
            ],
            title_of=lambda p: p["title"],
            posting_to_job=fake_posting_to_job,
            detail_fetch=fake_detail_fetch,
        )

        results, _ = run_platform_scan(
            scanner,
            "test-slug",
            target_titles=["Job"],
            exclusions=["Excluded"],
        )

        # detail_fetch called for matched postings only (Job 1, Job 2)
        assert set(call_log) == {"1", "2"}
        # Results include fetched descriptions
        assert len(results) == 2
        assert results[0]["description"] == "desc_1"
        assert results[1]["description"] == "desc_2"

    def test_detail_fetch_none_skips_parallel_path(self):
        """When scanner has no detail_fetch, serial path is used."""
        call_log = []

        def fake_posting_to_job(posting: dict, slug: str) -> dict:
            call_log.append(posting["id"])
            return {"title": posting["title"], "description": "serial_desc"}

        scanner = PlatformScanner(
            name="test",
            company_source="Test",
            fetch_postings=lambda slug, max_pages=None: [
                {"id": "1", "title": "Job 1"},
                {"id": "2", "title": "Job 2"},
            ],
            title_of=lambda p: p["title"],
            posting_to_job=fake_posting_to_job,
            detail_fetch=None,  # No parallel fetch
        )

        results, _ = run_platform_scan(
            scanner,
            "test-slug",
            target_titles=["Job"],
            exclusions=[],
        )

        # posting_to_job called directly (serial path)
        assert len(call_log) == 2
        assert results[0]["description"] == "serial_desc"

    def test_detail_fetch_failure_degrades_one_posting(self):
        """One posting's failed detail fetch doesn't affect others."""
        call_log = []

        def fake_detail_fetch(posting: dict) -> dict:
            call_log.append(posting["id"])
            if posting["id"] == "2":
                raise Exception("fetch failed")
            return {"__fetched_description": f"desc_{posting['id']}"}

        def fake_posting_to_job(posting: dict, slug: str) -> dict:
            return {
                "title": posting["title"],
                "description": posting.get("__fetched_description", "fallback"),
            }

        scanner = PlatformScanner(
            name="test",
            company_source="Test",
            fetch_postings=lambda slug, max_pages=None: [
                {"id": "1", "title": "Job 1"},
                {"id": "2", "title": "Job 2"},
                {"id": "3", "title": "Job 3"},
            ],
            title_of=lambda p: p["title"],
            posting_to_job=fake_posting_to_job,
            detail_fetch=fake_detail_fetch,
        )

        results, _ = run_platform_scan(
            scanner,
            "test-slug",
            target_titles=["Job"],
            exclusions=[],
        )

        # All three postings processed
        assert len(results) == 3
        # Failed posting gets fallback description
        assert results[0]["description"] == "desc_1"
        assert results[1]["description"] == "fallback"
        assert results[2]["description"] == "desc_3"

    def test_output_order_preserves_input_order(self):
        """Parallel fetch preserves input order for deterministic output."""

        def fake_detail_fetch(posting: dict) -> dict:
            return {"__fetched_description": f"desc_{posting['id']}"}

        def fake_posting_to_job(posting: dict, slug: str) -> dict:
            return {
                "title": posting["title"],
                "description": posting.get("__fetched_description", ""),
            }

        scanner = PlatformScanner(
            name="test",
            company_source="Test",
            fetch_postings=lambda slug, max_pages=None: [
                {"id": "3", "title": "Job 3"},
                {"id": "1", "title": "Job 1"},
                {"id": "2", "title": "Job 2"},
            ],
            title_of=lambda p: p["title"],
            posting_to_job=fake_posting_to_job,
            detail_fetch=fake_detail_fetch,
        )

        results, _ = run_platform_scan(
            scanner,
            "test-slug",
            target_titles=["Job"],
            exclusions=[],
        )

        # Output order matches input order
        assert results[0]["title"] == "Job 3"
        assert results[1]["title"] == "Job 1"
        assert results[2]["title"] == "Job 2"

    def test_no_detail_fetch_when_no_matched_postings(self):
        """detail_fetch is not called when no postings match title filter."""
        call_log = []

        def fake_detail_fetch(posting: dict) -> dict:
            call_log.append(posting["id"])
            return {"__fetched_description": "desc"}

        def fake_posting_to_job(posting: dict, slug: str) -> dict:
            return {"title": posting["title"], "description": ""}

        scanner = PlatformScanner(
            name="test",
            company_source="Test",
            fetch_postings=lambda slug, max_pages=None: [
                {"id": "1", "title": "Excluded Job"},
                {"id": "2", "title": "Another Excluded"},
            ],
            title_of=lambda p: p["title"],
            posting_to_job=fake_posting_to_job,
            detail_fetch=fake_detail_fetch,
        )

        results, _ = run_platform_scan(
            scanner,
            "test-slug",
            target_titles=["Wanted"],
            exclusions=[],
        )

        # No matched postings, so detail_fetch never called
        assert len(call_log) == 0
        assert len(results) == 0
