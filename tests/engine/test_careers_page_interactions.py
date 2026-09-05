# PORTED from tests/test_careers_page_interactions.py @ e218953fefd532d071482e3ff859610a46dd179d (private job-cannon). Ledger L-0600.
"""Tests for careers page interaction helpers."""

from unittest.mock import MagicMock

from jobcannon.engine.careers_page_interactions import (
    _is_ats_url,
    click_load_more,
    deduplicate_keywords,
    follow_pagination,
    parse_api_response,
    scroll_for_content,
    setup_api_capture,
    submit_search_form,
)

# ---------------------------------------------------------------------------
# _is_ats_url
# ---------------------------------------------------------------------------


class TestIsAtsUrl:
    def test_recognizes_known_ats_domains(self):
        assert _is_ats_url("https://boards.greenhouse.io/acme/jobs/1") is True
        assert _is_ats_url("https://jobs.lever.co/acme/abc-123") is True

    def test_rejects_unrelated_domain(self):
        assert _is_ats_url("https://example.com/careers") is False

    def test_rejects_lookalike_host(self):
        """A host that merely EMBEDS an ATS domain as a substring — not a real
        subdomain of it — must NOT be classified as an ATS URL
        (py/incomplete-url-substring-sanitization guard). This function gates
        whether a discovered link is excluded as 'already an ATS URL' during
        page-interaction crawling; a false positive here would silently drop
        a legitimate careers link that happens to embed an ATS domain name."""
        assert _is_ats_url("https://boards.greenhouse.io.evil.example/apply") is False


# ---------------------------------------------------------------------------
# deduplicate_keywords
# ---------------------------------------------------------------------------


class TestDeduplicateKeywords:
    def test_removes_superstrings(self):
        result = deduplicate_keywords(
            [
                "Data Scientist",
                "Senior Data Scientist",
                "Staff Data Scientist",
            ]
        )
        assert result == ["Data Scientist"]

    def test_keeps_unrelated_titles(self):
        result = deduplicate_keywords(
            [
                "Data Scientist",
                "ML Engineer",
                "Analytics Lead",
            ]
        )
        assert len(result) == 3

    def test_caps_at_three(self):
        result = deduplicate_keywords(
            [
                "A",
                "B",
                "C",
                "D",
                "E",
            ]
        )
        assert len(result) == 3

    def test_empty_input(self):
        assert deduplicate_keywords([]) == []

    def test_single_title(self):
        assert deduplicate_keywords(["Data Scientist"]) == ["Data Scientist"]

    def test_case_insensitive_containment(self):
        result = deduplicate_keywords(
            [
                "data scientist",
                "Senior Data Scientist",
            ]
        )
        assert result == ["data scientist"]


# ---------------------------------------------------------------------------
# parse_api_response
# ---------------------------------------------------------------------------


class TestParseApiResponse:
    def test_bare_array(self):
        data = [
            {"title": "Data Scientist", "url": "/jobs/1"},
            {"title": "ML Engineer", "url": "/jobs/2"},
            {"title": "Accountant", "url": "/jobs/3"},
        ]
        result = parse_api_response(
            data,
            ["data scientist", "engineer"],
            [],
            "https://example.com",
        )
        assert len(result) == 2
        assert result[0]["title"] == "Data Scientist"
        assert result[0]["url"] == "https://example.com/jobs/1"

    def test_jobs_wrapper(self):
        data = {
            "jobs": [
                {"title": "Software Engineer", "url": "https://example.com/j/1"},
            ]
        }
        result = parse_api_response(data, ["engineer"], [])
        assert len(result) == 1

    def test_results_wrapper(self):
        data = {
            "results": [
                {"name": "Product Manager", "link": "/pm"},
            ]
        }
        result = parse_api_response(
            data,
            ["product manager"],
            [],
            "https://example.com",
        )
        assert len(result) == 1
        assert result[0]["title"] == "Product Manager"
        assert result[0]["url"] == "https://example.com/pm"

    def test_nested_data_wrapper(self):
        data = {
            "data": {
                "jobs": [
                    {"title": "Analyst", "href": "/a/1"},
                ]
            }
        }
        result = parse_api_response(data, ["analyst"], [])
        assert len(result) == 1

    def test_exclusion_filter(self):
        data = [
            {"title": "Senior Engineer", "url": "/j/1"},
            {"title": "Junior Engineer", "url": "/j/2"},
        ]
        result = parse_api_response(data, ["engineer"], ["junior"])
        assert len(result) == 1
        assert "Senior" in result[0]["title"]

    def test_deduplicates_by_url(self):
        data = [
            {"title": "Engineer A", "url": "/j/1"},
            {"title": "Engineer B", "url": "/j/1"},
        ]
        result = parse_api_response(data, ["engineer"], [])
        assert len(result) == 1

    def test_empty_array(self):
        assert parse_api_response([], ["engineer"], []) == []

    def test_non_dict_items_skipped(self):
        # Array must start with a dict for _find_job_array to accept it
        data = [{"title": "Engineer", "url": "/j/1"}, 1, "string", None]
        result = parse_api_response(data, ["engineer"], [])
        assert len(result) == 1

    def test_missing_title_skipped(self):
        data = [{"url": "/j/1"}, {"title": "", "url": "/j/2"}]
        result = parse_api_response(data, [], [])
        assert len(result) == 0

    def test_no_matching_wrapper_key(self):
        data = {"unrelated_key": [{"title": "Engineer"}]}
        result = parse_api_response(data, ["engineer"], [])
        assert len(result) == 0


# ---------------------------------------------------------------------------
# setup_api_capture
# ---------------------------------------------------------------------------


class TestSetupApiCapture:
    def test_captures_xhr_with_api_pattern(self):
        page = MagicMock()
        handlers = []
        page.on.side_effect = lambda event, fn: handlers.append(fn)

        captured = setup_api_capture(page)
        page.on.assert_called_once_with("request", handlers[0])

        # Simulate a matching XHR request
        request = MagicMock()
        request.url = "https://example.com/api/jobs?page=1"
        request.resource_type = "xhr"
        handlers[0](request)

        assert len(captured) == 1
        assert "api/jobs" in captured[0]

    def test_ignores_non_xhr_requests(self):
        page = MagicMock()
        handlers = []
        page.on.side_effect = lambda event, fn: handlers.append(fn)

        captured = setup_api_capture(page)

        request = MagicMock()
        request.url = "https://example.com/api/jobs"
        request.resource_type = "document"
        handlers[0](request)

        assert len(captured) == 0

    def test_ignores_non_api_urls(self):
        page = MagicMock()
        handlers = []
        page.on.side_effect = lambda event, fn: handlers.append(fn)

        captured = setup_api_capture(page)

        request = MagicMock()
        request.url = "https://example.com/static/main.js"
        request.resource_type = "xhr"
        handlers[0](request)

        assert len(captured) == 0


# ---------------------------------------------------------------------------
# click_load_more
# ---------------------------------------------------------------------------


class TestClickLoadMore:
    def test_clicks_load_more_button(self):
        page = MagicMock()
        button = MagicMock()
        button.text_content.return_value = "Load More"

        page.query_selector_all.side_effect = lambda sel: [button] if sel == "button" else []

        # After first click, no more buttons
        click_count = 0

        def on_click():
            nonlocal click_count
            click_count += 1
            # After click, button disappears
            button.text_content.return_value = ""

        button.click.side_effect = on_click

        result = click_load_more(page, max_clicks=3)
        assert result is True
        assert click_count == 1

    def test_returns_false_when_no_buttons(self):
        page = MagicMock()
        page.query_selector_all.return_value = []
        assert click_load_more(page) is False

    def test_handles_exception_gracefully(self):
        page = MagicMock()
        page.query_selector_all.side_effect = Exception("DOM error")
        assert click_load_more(page) is False


# ---------------------------------------------------------------------------
# scroll_for_content
# ---------------------------------------------------------------------------


class TestScrollForContent:
    def test_detects_growing_height(self):
        page = MagicMock()
        heights = [1000, 2000, 3000, 3000]  # Grows twice, then stabilizes
        call_idx = [0]

        def eval_fn(script):
            if "scrollTo" in script:
                return None
            idx = call_idx[0]
            call_idx[0] += 1
            return heights[idx]

        page.evaluate.side_effect = eval_fn

        result = scroll_for_content(page, max_scrolls=5)
        assert result is True

    def test_no_growth_returns_false(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda script: None if "scrollTo" in script else 1000

        result = scroll_for_content(page, max_scrolls=3)
        assert result is False

    def test_handles_exception(self):
        page = MagicMock()
        page.evaluate.side_effect = Exception("JS error")
        assert scroll_for_content(page) is False


# ---------------------------------------------------------------------------
# follow_pagination
# ---------------------------------------------------------------------------


class TestFollowPagination:
    def test_finds_rel_next_link(self):
        page = MagicMock()
        page.content.return_value = """
        <html><body>
        <a rel="next" href="/careers?page=2">Next</a>
        </body></html>
        """
        urls = follow_pagination(page, "https://example.com/careers")
        assert len(urls) == 1
        assert urls[0] == "https://example.com/careers?page=2"

    def test_finds_aria_next(self):
        page = MagicMock()
        page.content.return_value = """
        <html><body>
        <a aria-label="Next page" href="/careers?page=2">></a>
        </body></html>
        """
        urls = follow_pagination(page, "https://example.com/careers")
        assert len(urls) == 1

    def test_finds_numbered_pages(self):
        page = MagicMock()
        page.content.return_value = """
        <html><body>
        <a href="?page=1">1</a>
        <a href="?page=2">2</a>
        <a href="?page=3">3</a>
        </body></html>
        """
        urls = follow_pagination(page, "https://example.com/careers")
        # Page 1 is current (skipped — only >1), so 2 and 3
        assert len(urls) == 2

    def test_respects_max_pages(self):
        page = MagicMock()
        links = "".join(f'<a href="?page={i}">{i}</a>' for i in range(2, 20))
        page.content.return_value = f"<html><body>{links}</body></html>"
        urls = follow_pagination(page, "https://example.com/careers", max_pages=3)
        assert len(urls) == 3

    def test_skips_ats_domain_links(self):
        page = MagicMock()
        page.content.return_value = """
        <html><body>
        <a rel="next" href="https://jobs.lever.co/company?page=2">Next</a>
        </body></html>
        """
        urls = follow_pagination(page, "https://example.com/careers")
        assert len(urls) == 0

    def test_empty_page(self):
        page = MagicMock()
        page.content.return_value = "<html><body></body></html>"
        assert follow_pagination(page, "https://example.com") == []


# ---------------------------------------------------------------------------
# submit_search_form
# ---------------------------------------------------------------------------


class TestSubmitSearchForm:
    def test_submits_visible_search_input(self):
        page = MagicMock()
        element = MagicMock()
        element.is_visible.return_value = True

        # Only match the first selector
        def query_selector(sel):
            if sel == 'input[type="search"]':
                return element
            return None

        page.query_selector.side_effect = query_selector

        result = submit_search_form(page, "Data Scientist")
        assert result is True
        element.fill.assert_any_call("Data Scientist")
        page.keyboard.press.assert_called_once_with("Enter")

    def test_skips_hidden_inputs(self):
        page = MagicMock()
        hidden = MagicMock()
        hidden.is_visible.return_value = False
        page.query_selector.return_value = hidden

        result = submit_search_form(page, "engineer")
        assert result is False

    def test_returns_false_when_no_input(self):
        page = MagicMock()
        page.query_selector.return_value = None
        assert submit_search_form(page, "engineer") is False
