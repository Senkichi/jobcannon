"""Tests for the PlatformScanner registry and shared scan driver.

Exercises the driver against a mock ``PlatformScanner`` so the spine is
verified in isolation from any platform's HTTP shape. Per-platform
fetchers stay tested via the existing scanner-specific test files.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest
import requests

from jobcannon.engine import runtime_config
from jobcannon.engine.ats_platforms._registry import (
    BoardGoneError,
    PlatformScanner,
    _get_cached_postings,
    _http_get_json,
    _store_cached_postings,
    run_platform_scan,
)
from tests.engine.helpers.ats_session import ats_session_method

# _scan_memo isolation is handled globally by tests/engine/conftest.py's
# reset_scan_memo autouse fixture. In the private source repo run_platform_scan
# is also exercised via the real platform scanners by test_workday_scanner.py,
# test_ats_scanner.py, and test_ats_raw_capture.py; those files stay behind
# here (Flask app-factory / DB-migration coupled — see the PR body's "tests
# not ported" list), so this file is currently the only engine-side exerciser.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_scanner(
    *,
    postings: list[dict] | None = None,
    title_key: str = "title",
    posting_to_job: Callable | None = None,
    name: str = "mock",
    company_source: str = "Mock",
) -> PlatformScanner:
    """Build a PlatformScanner whose fetch returns a fixed list."""

    def _fetch(_slug, max_pages=None):
        return list(postings or [])

    def _title_of(p):
        return p.get(title_key, "")

    def _default_posting_to_job(p, slug):
        return {
            "title": p.get(title_key, ""),
            "company_source": company_source,
            "location": p.get("location", ""),
            "description": "",
            "source_url": f"https://mock/{slug}/{p.get('id', '')}",
            "salary_min": None,
            "salary_max": None,
            "comp_json": None,
        }

    return PlatformScanner(
        name=name,
        company_source=company_source,
        fetch_postings=_fetch,
        title_of=_title_of,
        posting_to_job=posting_to_job or _default_posting_to_job,
    )


def _with_counting_fetch(scanner: PlatformScanner, counter: dict) -> PlatformScanner:
    """Wrap a scanner's fetch_postings to increment counter['n'] per call."""
    inner = scanner.fetch_postings

    def _fetch(slug, max_pages=None):
        counter["n"] += 1
        return inner(slug, max_pages=max_pages)

    return dataclasses.replace(scanner, fetch_postings=_fetch)


# ---------------------------------------------------------------------------
# run_platform_scan
# ---------------------------------------------------------------------------


class TestRunPlatformScan:
    def test_empty_postings_returns_empty_list(self):
        scanner = _make_mock_scanner(postings=[])
        results, skipped = run_platform_scan(scanner, "anyslug", ["foo"], [])
        assert results == []
        assert skipped == 0

    def test_all_postings_matched_returns_all(self):
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Staff Data Scientist", "id": "2"},
            ]
        )
        results, skipped = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 2
        assert results[0]["title"] == "Senior Data Scientist"
        assert results[0]["source_url"] == "https://mock/acme/1"
        assert results[1]["title"] == "Staff Data Scientist"

    def test_title_filter_excludes_non_matching(self):
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Marketing Manager", "id": "2"},
            ]
        )
        results, skipped = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"

    def test_exclusion_filter_drops_excluded(self):
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Junior Data Scientist", "id": "2"},
            ]
        )
        results, skipped = run_platform_scan(scanner, "acme", ["data scientist"], ["junior"])
        assert len(results) == 1
        assert results[0]["title"] == "Senior Data Scientist"
        assert skipped == 1

    def test_posting_to_job_returning_none_skips(self):
        def _to_job(posting, _slug):
            if posting.get("title", "").startswith("Senior"):
                return None
            return {"title": posting.get("title"), "company_source": "Mock"}

        scanner = _make_mock_scanner(
            postings=[
                {"title": "Senior Data Scientist", "id": "1"},
                {"title": "Staff Data Scientist", "id": "2"},
            ],
            posting_to_job=_to_job,
        )
        results, skipped = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 1
        assert results[0]["title"] == "Staff Data Scientist"

    def test_empty_target_titles_allows_all_through(self):
        """With empty target_titles, the title gate accepts every title."""
        scanner = _make_mock_scanner(
            postings=[
                {"title": "Anything Goes", "id": "1"},
                {"title": "Anything Else", "id": "2"},
            ]
        )
        results, skipped = run_platform_scan(scanner, "acme", [], [])
        assert len(results) == 2

    def test_fetch_returning_generator_works(self):
        """fetch_postings can return any iterable; driver lists it."""

        def _fetch(_slug, max_pages=None):
            yield {"title": "Data Scientist", "id": "1"}
            yield {"title": "Data Engineer", "id": "2"}

        scanner = PlatformScanner(
            name="gen",
            company_source="Gen",
            fetch_postings=_fetch,  # type: ignore[arg-type]
            title_of=lambda p: p.get("title", ""),
            posting_to_job=lambda p, _s: {"title": p["title"]},
        )
        results, skipped = run_platform_scan(scanner, "acme", ["data scientist"], [])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# run_platform_scan — raw-postings memo cache
# ---------------------------------------------------------------------------


class TestRunPlatformScanCache:
    """Same-tenant listing bursts should share one fetch_postings call."""

    def test_second_call_same_company_reuses_cached_postings(self):
        counter = {"n": 0}
        scanner = _with_counting_fetch(
            _make_mock_scanner(postings=[{"title": "Data Scientist", "id": "1"}], name="workday"),
            counter,
        )
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        assert counter["n"] == 1

    def test_cache_stores_raw_postings_not_filtered_results(self):
        """A second call with different target_titles must filter fresh,
        not replay the first call's filtered results."""
        counter = {"n": 0}
        scanner = _with_counting_fetch(
            _make_mock_scanner(
                postings=[
                    {"title": "Data Scientist", "id": "1"},
                    {"title": "Marketing Manager", "id": "2"},
                ],
                name="workday",
            ),
            counter,
        )
        results1, _ = run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        results2, _ = run_platform_scan(scanner, "citi.wd5/2", ["marketing"], [])
        assert [r["title"] for r in results1] == ["Data Scientist"]
        assert [r["title"] for r in results2] == ["Marketing Manager"]
        assert counter["n"] == 1

    def test_different_slug_same_scanner_triggers_new_fetch(self):
        counter = {"n": 0}
        scanner = _with_counting_fetch(
            _make_mock_scanner(postings=[{"title": "Data Scientist", "id": "1"}], name="workday"),
            counter,
        )
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        run_platform_scan(scanner, "acme.wd1/1", ["data scientist"], [])
        assert counter["n"] == 2

    def test_different_scanner_same_slug_triggers_new_fetch(self):
        counter = {"n": 0}
        scanner_a = _with_counting_fetch(
            _make_mock_scanner(postings=[{"title": "Data Scientist", "id": "1"}], name="workday"),
            counter,
        )
        scanner_b = _with_counting_fetch(
            _make_mock_scanner(
                postings=[{"title": "Data Scientist", "id": "1"}],
                name="successfactors",
            ),
            counter,
        )
        run_platform_scan(scanner_a, "sameslug", ["data scientist"], [])
        run_platform_scan(scanner_b, "sameslug", ["data scientist"], [])
        assert counter["n"] == 2

    def test_ttl_expiry_triggers_fresh_fetch(self, monkeypatch):
        counter = {"n": 0}
        scanner = _with_counting_fetch(
            _make_mock_scanner(postings=[{"title": "Data Scientist", "id": "1"}], name="workday"),
            counter,
        )
        clock = {"t": 0.0}
        monkeypatch.setattr(
            "jobcannon.engine.ats_platforms._registry.time.monotonic", lambda: clock["t"]
        )
        runtime_config.set_config_provider(lambda: {"ats": {"scan_memo_ttl_s": 1}})
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        clock["t"] += 2  # past the configured 1-second TTL
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        assert counter["n"] == 2

    def test_board_gone_error_never_cached(self):
        counter = {"n": 0}

        def _fetch(slug, max_pages=None):
            counter["n"] += 1
            raise BoardGoneError(404, slug)

        scanner = dataclasses.replace(_make_mock_scanner(name="workday"), fetch_postings=_fetch)
        with pytest.raises(BoardGoneError):
            run_platform_scan(scanner, "gone.wd5/1", ["data scientist"], [])
        with pytest.raises(BoardGoneError):
            run_platform_scan(scanner, "gone.wd5/1", ["data scientist"], [])
        assert counter["n"] == 2

    def test_force_fresh_bypasses_read_and_writes_result(self):
        """force_fresh skips the memo read, fetches, and still writes the memo."""
        counter = {"n": 0}
        postings_generations = [
            [{"title": "Cached Data Scientist", "id": "1"}],
            [{"title": "Fresh Data Scientist", "id": "2"}],
        ]

        def _fetch(slug, max_pages=None):
            counter["n"] += 1
            return list(postings_generations[counter["n"] - 1])

        scanner = dataclasses.replace(_make_mock_scanner(name="workday"), fetch_postings=_fetch)

        # First call: normal cache miss
        results1, _ = run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        assert [r["title"] for r in results1] == ["Cached Data Scientist"]

        # Second call with force_fresh: must re-fetch and overwrite the memo
        results2, _ = run_platform_scan(
            scanner, "citi.wd5/2", ["data scientist"], [], force_fresh=True
        )
        assert [r["title"] for r in results2] == ["Fresh Data Scientist"]

        # Third call without force_fresh: should use the memo written by the
        # force_fresh call, not fetch again
        results3, _ = run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        assert [r["title"] for r in results3] == ["Fresh Data Scientist"]

        assert counter["n"] == 2

    def test_force_fresh_never_returns_cached_board_within_ttl(self):
        """Even with a fresh memo entry, force_fresh always re-fetches."""
        counter = {"n": 0}
        scanner = _with_counting_fetch(
            _make_mock_scanner(postings=[{"title": "Data Scientist", "id": "1"}], name="workday"),
            counter,
        )
        # Normal call populates the memo
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])
        # force_fresh inside the TTL window must still fetch
        run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [], force_fresh=True)
        assert counter["n"] == 2


# ---------------------------------------------------------------------------
# run_platform_scan — thundering herd
# ---------------------------------------------------------------------------


class TestRunPlatformScanThunderingHerd:
    """Rigorous concurrent-access test for run_platform_scan's per-key lock.

    Uses threading.Barrier to force N threads to call run_platform_scan for
    the SAME (scanner, slug, max_pages) at the same instant, with a
    fetch_postings factory that sleeps ~0.05s so a missing/no-op lock lets
    overlapping fetches actually happen. A fast mocked fetch completes before
    a preemption window ever opens, which is exactly why the existing cache
    tests kept passing while the real fetch was unguarded outside the lock.
    Mirrors TestCareersPageMemoLockConcurrency.
    """

    def test_concurrent_same_key_fetches_once(self):
        n_threads = 10
        barrier = threading.Barrier(n_threads)

        compute_count = 0
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        postings = [{"title": "Data Scientist", "id": "1"}]

        def _fetch(slug, max_pages=None):
            nonlocal compute_count, active, max_active
            with counter_lock:
                compute_count += 1
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)  # force a preemption window mid-fetch
            with counter_lock:
                active -= 1
            return postings

        scanner = dataclasses.replace(_make_mock_scanner(name="workday"), fetch_postings=_fetch)

        def worker():
            barrier.wait()
            run_platform_scan(scanner, "citi.wd5/2", ["data scientist"], [])

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert compute_count == 1, (
            "expected per-key lock to serialize concurrent run_platform_scan "
            f"calls for the same key (compute_count == 1); got {compute_count} "
            "-- fetch_postings ran more than once, meaning multiple threads "
            "fetched the same board concurrently instead of one fetching and "
            "the others reusing the cached result"
        )
        assert max_active == 1, (
            "expected at most one thread inside fetch_postings at a time "
            f"(max_active == 1); got max_active={max_active} -- overlapping "
            "unlocked fetches were observed, proving the per-key lock is not "
            "actually guarding concurrent access"
        )


# ---------------------------------------------------------------------------
# _http_get_json
# ---------------------------------------------------------------------------


class TestHttpGetJson:
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_success_returns_parsed_json(self, mock_get):
        mock_get = ats_session_method(mock_get, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"jobs": [{"id": "1"}]}
        mock_get.return_value = mock_resp

        result = _http_get_json("https://x", log_label="scan_x", slug="foo")
        assert result == {"jobs": [{"id": "1"}]}

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_non_200_returns_none(self, mock_get):
        mock_get = ats_session_method(mock_get, "get")
        mock_get.return_value = MagicMock(status_code=404)
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_exception_returns_none(self, mock_get):
        mock_get = ats_session_method(mock_get, "get")
        mock_get.side_effect = Exception("connection refused")
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_json_parse_error_returns_none(self, mock_get):
        mock_get = ats_session_method(mock_get, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.side_effect = ValueError("bad json")
        mock_get.return_value = mock_resp
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None

    @patch("jobcannon.engine.ats_platforms._registry.time.sleep")
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_timeout_without_retry_returns_none(self, mock_get, mock_sleep):
        mock_get = ats_session_method(mock_get, "get")
        mock_get.side_effect = requests.exceptions.Timeout("read timeout")
        assert _http_get_json("https://x", log_label="scan_x", slug="foo") is None
        assert mock_get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("jobcannon.engine.ats_platforms._registry.time.sleep")
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_timeout_with_retry_retries_once_then_succeeds(self, mock_get, mock_sleep):
        mock_get = ats_session_method(mock_get, "get")
        success_resp = MagicMock(status_code=200)
        success_resp.json.return_value = {"ok": True}
        mock_get.side_effect = [requests.exceptions.Timeout("first attempt"), success_resp]

        result = _http_get_json(
            "https://x",
            log_label="scan_x",
            slug="foo",
            retry_on_timeout=True,
        )
        assert result == {"ok": True}
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(2)

    @patch("jobcannon.engine.ats_platforms._registry.time.sleep")
    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_timeout_with_retry_gives_up_after_second_attempt(self, mock_get, mock_sleep):
        mock_get = ats_session_method(mock_get, "get")
        mock_get.side_effect = [
            requests.exceptions.Timeout("first"),
            requests.exceptions.Timeout("second"),
        ]
        result = _http_get_json(
            "https://x",
            log_label="scan_x",
            slug="foo",
            retry_on_timeout=True,
        )
        assert result is None
        assert mock_get.call_count == 2
        mock_sleep.assert_called_once_with(2)

    @patch("jobcannon.engine.ats_platforms._registry.get_session")
    def test_params_and_headers_forwarded(self, mock_get):
        mock_get = ats_session_method(mock_get, "get")
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp
        _http_get_json(
            "https://x",
            log_label="scan_x",
            slug="foo",
            params={"offset": 0, "limit": 100},
            headers={"Accept": "application/json"},
        )
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {"offset": 0, "limit": 100}
        assert kwargs["headers"] == {"Accept": "application/json"}


# ---------------------------------------------------------------------------
# run_platform_scan — detail_fetch parallelization
# ---------------------------------------------------------------------------


class TestRunPlatformScanDetailFetch:
    """Tests for the parallel detail fetch feature (Workday / SmartRecruiters)."""

    def test_detail_fetch_concurrency_config_knob_respects_bound(self):
        """The detail_fetch_concurrency config knob is clamped to 1-6."""
        # Test floor of 1 (not 4) — operators must be able to throttle to 1
        runtime_config.set_config_provider(lambda: {"ats": {"detail_fetch_concurrency": 1}})
        scanner = _make_mock_scanner(
            postings=[{"title": "Data Scientist", "id": "1"}],
            name="workday",
        )
        # Add a detail_fetch that records the concurrency
        concurrency_tracker = {"max_concurrent": 0, "active": 0, "lock": threading.Lock()}

        def _make_detail_fetch(tracker):
            def _detail_fetch(posting):
                with tracker["lock"]:
                    tracker["active"] += 1
                    tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
                time.sleep(0.05)  # Simulate work
                with tracker["lock"]:
                    tracker["active"] -= 1
                return {"__fetched_description": "desc"}

            return _detail_fetch

        scanner = dataclasses.replace(scanner, detail_fetch=_make_detail_fetch(concurrency_tracker))
        run_platform_scan(scanner, "test", ["data scientist"], [])
        # With concurrency=1, max concurrent should be 1
        assert concurrency_tracker["max_concurrent"] == 1

    def test_detail_fetch_concurrency_clamps_to_range(self):
        """Config values outside 1-6 are clamped.

        Each detail_fetch call blocks on a threading.Condition until the
        observed peak concurrency reaches the expected ceiling (or a
        generous timeout elapses), instead of racing a fixed sleep window
        against real thread-start latency. A fixed short sleep (the
        original design) is flaky on loaded CI runners: nothing guarantees
        all `expected_concurrency` worker threads are scheduled and past
        their startup overhead within the sleep window, so the observed
        peak can undercount even when the implementation is correct. The
        condition rendezvous makes the assertion deterministic — it
        resolves as soon as enough peers arrive, and the timeout is only a
        fail-safe for genuine regressions.
        """
        test_cases = [
            (0, 1),  # Below floor → 1
            (-5, 1),  # Negative → 1
            (1, 1),  # Minimum valid → 1
            (4, 4),  # Default → 4
            (6, 6),  # Maximum valid → 6
            (10, 6),  # Above ceiling → 6
            (100, 6),  # Way above ceiling → 6
        ]

        for config_value, expected_concurrency in test_cases:
            runtime_config.set_config_provider(
                lambda config_value=config_value: {
                    "ats": {"detail_fetch_concurrency": config_value}
                }
            )
            # 8 postings so concurrency bounds up to the ceiling (6) are
            # actually observable — a single posting can never show > 1
            # concurrent regardless of the configured bound.
            scanner = _make_mock_scanner(
                postings=[{"title": "Data Scientist", "id": str(i)} for i in range(8)],
                name="workday",
            )

            condition = threading.Condition()
            state = {"active": 0, "max_active": 0}

            def _make_detail_fetch(condition, state, expected):
                def _detail_fetch(posting):
                    with condition:
                        state["active"] += 1
                        state["max_active"] = max(state["max_active"], state["active"])
                        condition.notify_all()
                        # max_active is monotonic, so once the ceiling has
                        # been observed once, later waves resolve instantly.
                        condition.wait_for(lambda: state["max_active"] >= expected, timeout=2)
                        state["active"] -= 1
                    return {"__fetched_description": "desc"}

                return _detail_fetch

            scanner = dataclasses.replace(
                scanner,
                detail_fetch=_make_detail_fetch(condition, state, expected_concurrency),
            )
            run_platform_scan(scanner, "test", ["data scientist"], [])
            assert state["max_active"] == expected_concurrency, (
                f"config={config_value}, expected={expected_concurrency}, got={state['max_active']}"
            )

    def test_detail_fetch_respects_concurrency_bound(self):
        """Recorded-concurrency test: bound is respected, overlap proven."""
        # Create 8 postings with a slow detail fetch
        postings = [{"title": f"Job {i}", "id": str(i)} for i in range(8)]
        scanner = _make_mock_scanner(postings=postings, name="workday")

        # Set concurrency to 3
        runtime_config.set_config_provider(lambda: {"ats": {"detail_fetch_concurrency": 3}})

        concurrency_tracker = {"max_concurrent": 0, "active": 0, "lock": threading.Lock()}

        def _make_detail_fetch(tracker):
            def _detail_fetch(posting):
                with tracker["lock"]:
                    tracker["active"] += 1
                    tracker["max_concurrent"] = max(tracker["max_concurrent"], tracker["active"])
                time.sleep(0.1)  # Simulate slow network
                with tracker["lock"]:
                    tracker["active"] -= 1
                return {"__fetched_description": "desc"}

            return _detail_fetch

        scanner = dataclasses.replace(scanner, detail_fetch=_make_detail_fetch(concurrency_tracker))
        run_platform_scan(scanner, "test", ["job"], [])

        # With 8 postings and concurrency=3, we should see at most 3 concurrent
        # (proves the executor is actually parallelizing, not serial)
        assert concurrency_tracker["max_concurrent"] == 3

    def test_detail_fetch_does_not_mutate_cached_postings(self):
        """The parallel fetch must not mutate cached posting dicts."""
        # First call: populate cache
        postings = [
            {"title": "Job 1", "id": "1"},
            {"title": "Job 2", "id": "2"},
        ]
        scanner = _make_mock_scanner(postings=postings, name="workday")

        def _detail_fetch(posting):
            return {"__fetched_description": f"desc for {posting['id']}"}

        scanner = dataclasses.replace(scanner, detail_fetch=_detail_fetch)

        # First call should populate cache
        results1, _ = run_platform_scan(scanner, "test", ["job"], [])

        # Second call with same slug should reuse cached postings
        # If the first call mutated the cached dicts, the second call would
        # see the mutated state (with __fetched_description already present)
        results2, _ = run_platform_scan(scanner, "test", ["job"], [])

        # Both calls should produce the same results
        assert len(results1) == len(results2) == 2
        assert results1[0]["title"] == results2[0]["title"] == "Job 1"
        assert results1[1]["title"] == results2[1]["title"] == "Job 2"

    def test_detail_fetch_failure_is_logged(self, caplog):
        """Failed detail fetches are logged at DEBUG level."""

        def _failing_detail_fetch(posting):
            raise ValueError("simulated fetch failure")

        scanner = _make_mock_scanner(
            postings=[{"title": "Job 1", "id": "1"}],
            name="workday",
        )
        scanner = dataclasses.replace(scanner, detail_fetch=_failing_detail_fetch)

        with caplog.at_level(logging.DEBUG):
            run_platform_scan(scanner, "test", ["job"], [])

        # Should have logged the failure
        assert any("detail_fetch failed" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# _scan_memo_lock — concurrent access safety
# ---------------------------------------------------------------------------


class _ReentrancyTrackingDict(dict):
    """dict subclass whose get()/__setitem__() are NOT single atomic C ops.

    Real ``dict.get()`` / ``dict[key] = value`` are GIL-atomic for a single
    key in CPython, so a naive concurrency test against the real
    ``_scan_memo`` dict cannot fail even with ``_scan_memo_lock`` removed
    entirely — a prior audit judged exactly that test vacuous. This
    subclass turns get()/__setitem__() into Python-level, multi-step,
    GIL-releasing operations (bump an "active" counter, sleep, do the real
    op, drop the counter) so a thread can be preempted mid-operation.
    ``_scan_memo_lock`` is supposed to serialize every call into
    ``_get_cached_postings``/``_store_cached_postings``; if it does, at most
    one thread is ever "active" inside get()/__setitem__() at a time —
    regardless of the real dict's GIL-atomicity. If the lock is removed
    from production code, barrier-forced concurrent callers race straight
    into this dict's critical section together and ``max_active`` exceeds 1.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._counter_lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def _enter(self):
        with self._counter_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _exit(self):
        with self._counter_lock:
            self.active -= 1

    def get(self, *args, **kwargs):
        self._enter()
        try:
            time.sleep(0.05)  # force a preemption window mid-operation
            return super().get(*args, **kwargs)
        finally:
            self._exit()

    def __setitem__(self, key, value):
        self._enter()
        try:
            time.sleep(0.05)  # force a preemption window mid-operation
            super().__setitem__(key, value)
        finally:
            self._exit()


class TestScanMemoLockConcurrency:
    """Rigorous concurrent-access test for _scan_memo_lock (Tests section).

    Uses threading.Barrier to force N threads into _get_cached_postings /
    _store_cached_postings at the same instant, against a dict subclass
    (_ReentrancyTrackingDict) engineered to make a lost interleave
    observable even though the real dict's single-key get/set are
    GIL-atomic on their own. Commenting out ``with _scan_memo_lock:`` in
    ``_registry.py`` makes this test fail deterministically; a version of
    this test against the plain real dict would NOT fail under that same
    mutation, which is exactly why that naive shape was rejected in review.
    """

    def test_concurrent_get_and_store_are_serialized_by_scan_memo_lock(self, monkeypatch):
        tracking_dict = _ReentrancyTrackingDict()
        monkeypatch.setattr("jobcannon.engine.ats_platforms._registry._scan_memo", tracking_dict)

        n_threads = 8
        barrier = threading.Barrier(n_threads)

        def worker(i):
            barrier.wait()
            if i % 2 == 0:
                _get_cached_postings("lever", "acme", None)
            else:
                _store_cached_postings("lever", "acme", None, [{"id": i}])

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert tracking_dict.max_active == 1, (
            "expected _scan_memo_lock to serialize every _get_cached_postings/"
            "_store_cached_postings call (max_active == 1); got "
            f"max_active={tracking_dict.max_active} -- multiple threads entered "
            "the tracked dict's get()/__setitem__() at the same time, proving "
            "the lock is not actually guarding concurrent access"
        )


# ---------------------------------------------------------------------------
# Platform SCANNER constants — surface check
# ---------------------------------------------------------------------------


class TestPlatformScannerConstants:
    """Confirm each platform module exports a well-formed SCANNER."""

    @pytest.mark.parametrize(
        "module_path,expected_name,expected_company_source",
        [
            ("jobcannon.engine.ats_platforms._platforms_lever", "lever", "Lever"),
            (
                "jobcannon.engine.ats_platforms._platforms_greenhouse",
                "greenhouse",
                "Greenhouse",
            ),
            ("jobcannon.engine.ats_platforms._platforms_ashby", "ashby", "Ashby"),
            ("jobcannon.engine.ats_platforms._platforms_workday", "workday", "Workday"),
            (
                "jobcannon.engine.ats_platforms._platforms_smartrecruiters",
                "smartrecruiters",
                "SmartRecruiters",
            ),
            (
                "jobcannon.engine.ats_platforms._platforms_recruitee",
                "recruitee",
                "Recruitee",
            ),
            ("jobcannon.engine.ats_platforms._platforms_breezy", "breezy", "Breezy"),
            ("jobcannon.engine.ats_platforms._platforms_jazzhr", "jazzhr", "JazzHR"),
            ("jobcannon.engine.ats_platforms._platforms_pinpoint", "pinpoint", "Pinpoint"),
            ("jobcannon.engine.ats_platforms._platforms_personio", "personio", "Personio"),
            ("jobcannon.engine.ats_platforms._platforms_bamboohr", "bamboohr", "BambooHR"),
            (
                "jobcannon.engine.ats_platforms._platforms_teamtailor",
                "teamtailor",
                "Teamtailor",
            ),
        ],
    )
    def test_scanner_constant_well_formed(
        self, module_path, expected_name, expected_company_source
    ):
        import importlib

        mod = importlib.import_module(module_path)
        scanner = mod.SCANNER
        assert isinstance(scanner, PlatformScanner)
        assert scanner.name == expected_name
        assert scanner.company_source == expected_company_source
        # The three callables must all exist and be callable.
        assert callable(scanner.fetch_postings)
        assert callable(scanner.title_of)
        assert callable(scanner.posting_to_job)
