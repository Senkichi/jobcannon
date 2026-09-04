# PORTED from tests/test_careers_raw_capture.py @ a7f0f38a85dfa0af4d305c04da833785f723d649 (private job-cannon). Ledger L-0579.
"""Tests for careers raw-HTML capture — Phase B (issue #205).

Verifies that _try_static_extract records a corpus_sample row with
surface='careers' (source keyed per company since D3) and the raw HTML (truncated to 50 000 chars), and updates
source_health with detect=True semantics, when called with a db_path.

Acceptance criteria (from issue #205):
- A mocked static fetch returning job HTML records a corpus_sample row with
  surface=careers and the raw HTML (truncated).
- source_health is updated for the per-company source key (D3 re-keying).
- detect=True means zero-yield pages after a baseline increment
  consecutive_breaks (unlike the superseded Phase-A detect=False hook).

# PORT-SEAM: the writer above (autoheal.health_monitor.record_extraction ->
# corpus_sample / source_health) is DIES -- single-user-desktop
# (L-0138/L-0139/L-0140) -- same precedent as
# tests/engine/test_autoheal_email_capture.py (L-0562). What DOES carry from
# the landed public code is the seam call itself
# (jobcannon.engine.careers_crawler._autoheal_seam.record_careers_capture,
# called from _try_static_extract): it is always invoked, is a no-op when
# ScanServices.record_careers_extraction is unwired (the harness default),
# and forwards exactly the source key / surface / truncated-HTML / structural
# counts the (unported) health monitor would have consumed. The db_path
# kwarg _try_static_extract took above is also gone (L-0469:
# svc.connection_factory() is zero-arg).
#
# KEPT/ADAPTED (2, new names -- not 1:1 renames):
# - test_capture_seam_unwired_returns_jobs_without_raising <-
#   test_static_tier_no_db_path_does_not_raise.
# - test_capture_seam_call_contract_generic_path <- absorbs the
#   source-key / surface / HTML-truncation assertions from
#   test_static_tier_records_corpus_sample and
#   test_static_tier_html_truncated_to_50000, checked at the seam-call
#   boundary (a fake record_careers_extraction) instead of via corpus_sample
#   row reads.
#
# DROPPED (private-only surface, listed in the PR body):
# - test_static_tier_records_corpus_sample (corpus_sample row shape --
#   superseded by the adapted call-contract test above; DIES writer).
# - test_static_tier_updates_source_health (source_health row -- DIES, no
#   seam-level equivalent; source_health bookkeeping lives entirely inside
#   the unported health monitor, not in record_careers_capture).
# - test_static_tier_detect_true_counts_breaks_after_baseline
#   (consecutive_breaks counting -- DIES, same reason: the break-counter
#   arithmetic is the health monitor's own state, not observable from the
#   seam call, which only forwards detect=True as a flag).
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import pytest  # PORT-SEAM: new fixture import (db_path fixture below)

from jobcannon.engine import services  # PORT-SEAM: seam services (L-0469)
from jobcannon.engine.careers_crawler._static_tier import _try_static_extract
from tests.engine.helpers.ats_scan_services import (
    make_scan_services,
)  # PORT-SEAM: shared fake-services builder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAREERS_URL = "https://example.com/careers"

# HTML with enough visible text to pass the static-page ratio check and
# enough job links for target_titles=[] (match-all) to return results.
_JOB_HTML = (
    "<html><body>"
    "<h1>Open Positions at Acme Corp</h1>"
    "<p>" + "We are always looking for talented people to join our team. " * 15 + "</p>"
    "<ul>"
    '<li><a href="/jobs/software-engineer-001">Software Engineer</a></li>'
    '<li><a href="/jobs/product-manager-002">Product Manager</a></li>'
    "</ul>"
    "</body></html>"
)


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def db_path(tmp_path):  # PORT-SEAM: new fixture (db_path)
    return str(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# PORT-SEAM: renamed <- test_static_tier_no_db_path_does_not_raise (see module docstring)
def test_capture_seam_unwired_returns_jobs_without_raising(db_path):
    """No record_careers_extraction wired (the harness default) -> the tier
    still returns results; the seam call is a silent no-op."""
    services.set_services(make_scan_services(db_path))

    with patch("requests.get", return_value=_mock_response(_JOB_HTML)):
        result = _try_static_extract(
            _CAREERS_URL, [], []
        )  # PORT-SEAM: db_path kwarg dropped (L-0469)

    # Extraction still works — jobs returned (target_titles=[] matches all)
    assert result is not None
    assert len(result) > 0

    # PORT-SEAM: corpus_sample row read dropped -- see module docstring DROPPED note


# PORT-SEAM: absorbs corpus_sample/source_health assertions (see module docstring)
def test_capture_seam_call_contract_generic_path(db_path):
    """With record_careers_extraction wired, the generic (no-override) path
    forwards: the per-company source key, surface='careers', HTML truncated
    to 50 000 chars, job_count=structural candidate count (not the
    title-filtered count), detect=True, legacy_count=None, extractor='generic',
    filtered_count=len(jobs)."""
    captured: dict = {}

    # PORT-SEAM: fake seam callable replaces sqlite3 row reads
    def fake_record_careers_extraction(
        conn,
        source,
        surface,
        html_text,
        *,
        job_count,
        detect,
        legacy_count,
        extractor,
        filtered_count,
    ):
        captured.update(
            conn=conn,
            source=source,
            surface=surface,
            html_text=html_text,
            job_count=job_count,
            detect=detect,
            legacy_count=legacy_count,
            extractor=extractor,
            filtered_count=filtered_count,
        )

    # PORT-SEAM: absorbs test_static_tier_detect_true_counts_breaks_after_baseline's
    # source_health read (see module docstring DROPPED note)
    services.set_services(
        make_scan_services(db_path, record_careers_extraction=fake_record_careers_extraction)
    )

    # PORT-SEAM: absorbs test_static_tier_html_truncated_to_50000 (see module docstring)
    big_html = _JOB_HTML + "<!-- padding -->" + ("x" * 60000)

    with patch("requests.get", return_value=_mock_response(big_html)):
        jobs = _try_static_extract(
            _CAREERS_URL, [], []
        )  # PORT-SEAM: db_path kwarg dropped (L-0469)

    # PORT-SEAM: absorbs test_static_tier_records_corpus_sample's corpus_sample
    # row read (see module docstring DROPPED note)
    assert jobs is not None and len(jobs) == 2

    assert captured["source"] == "careers:example.com"
    assert captured["surface"] == "careers"
    assert captured["html_text"] == big_html[:50000]
    assert len(captured["html_text"]) <= 50000
    assert captured["detect"] is True
    assert captured["extractor"] == "generic"
    assert captured["legacy_count"] is None
    assert captured["filtered_count"] == len(jobs)
    # Structural count (pre-title-filter candidates) >= the filtered count.
    assert captured["job_count"] >= captured["filtered_count"]

    # conn is a real, usable sqlite3 connection (record_careers_capture
    # commits on it after the seam call returns).
    assert isinstance(captured["conn"], sqlite3.Connection)
