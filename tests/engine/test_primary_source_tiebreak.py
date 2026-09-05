# PORTED from tests/test_primary_source_tiebreak.py @ c6e37b72c6706e6f547c63d0a697b9ef645c2dff (private job-cannon). Ledger L-0605.
"""Tests for the quick-tier LLM tie-breaker (Phase 4).

# PORT-SEAM: ``call_model`` is a required keyword-only injected parameter here
# (design note PR-4 section 1c), not the private module's deferred
# ``from job_finder.web.model_provider import call_model`` import the
# private tests patched -- every test below passes ``call_model=`` directly
# to ``tiebreak_primary_posting`` instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from jobcannon.engine.primary_source_tiebreak import tiebreak_primary_posting


def _verdict(match_index, confident):
    return MagicMock(data={"match_index": match_index, "confident": confident})


def _posting(title, url, **extra):
    return {"title": title, "source_url": url, "description": "x" * 300, **extra}


def test_confident_valid_index_returns_posting():
    postings = [
        _posting("Sr. SWE II", "https://jobs.lever.co/acme/1"),
        _posting("Staff Engineer", "https://jobs.lever.co/acme/2"),
    ]
    fake_call = MagicMock(return_value=_verdict(1, True))
    chosen = tiebreak_primary_posting(
        postings,
        "Staff Engineer (Platform)",
        "Remote",
        "snippet",
        None,
        {},
        call_model=fake_call,
    )
    assert chosen is postings[1]
    kwargs = fake_call.call_args.kwargs
    assert kwargs["tier"] == "quick"
    assert kwargs["purpose"] == "primary_source_tiebreak"


def test_not_confident_stays_loose():
    postings = [_posting("Engineer", "https://jobs.lever.co/acme/1")]
    fake_call = MagicMock(return_value=_verdict(0, False))
    assert (
        tiebreak_primary_posting(postings, "Engineer", "", None, None, {}, call_model=fake_call)
        is None
    )


def test_null_index_stays_loose():
    """The explicit "none of these / can't tell" exit (P13)."""
    postings = [_posting("Engineer", "https://jobs.lever.co/acme/1")]
    fake_call = MagicMock(return_value=_verdict(None, True))
    assert (
        tiebreak_primary_posting(postings, "Engineer", "", None, None, {}, call_model=fake_call)
        is None
    )


def test_out_of_range_index_stays_loose():
    postings = [_posting("Engineer", "https://jobs.lever.co/acme/1")]
    fake_call = MagicMock(return_value=_verdict(5, True))
    assert (
        tiebreak_primary_posting(postings, "Engineer", "", None, None, {}, call_model=fake_call)
        is None
    )


def test_boolean_index_stays_loose():
    """bool is an int subclass — true must not silently index posting 1."""
    postings = [
        _posting("Engineer", "https://jobs.lever.co/acme/1"),
        _posting("Designer", "https://jobs.lever.co/acme/2"),
    ]
    fake_call = MagicMock(return_value=_verdict(True, True))
    assert (
        tiebreak_primary_posting(postings, "Engineer", "", None, None, {}, call_model=fake_call)
        is None
    )


def test_oversized_board_skips_model_call():
    postings = [_posting(f"Role {i}", f"https://jobs.lever.co/acme/{i}") for i in range(41)]
    fake_call = MagicMock()
    assert (
        tiebreak_primary_posting(postings, "Role 3", "", None, None, {}, call_model=fake_call)
        is None
    )
    assert fake_call.call_count == 0


def test_no_linked_candidates_skips_model_call():
    fake_call = MagicMock()
    result = tiebreak_primary_posting(
        [{"title": "Engineer"}], "Engineer", "", None, None, {}, call_model=fake_call
    )
    assert result is None
    assert fake_call.call_count == 0
