"""``run_ats_scan`` wiring guards.

Engine-native replacement for the private repo's
tests/test_ats_scanner_autoheal_trigger.py — that file's live-path
integration test (``test_run_ats_scan_flips_armed_ats_source_to_degraded``)
drives a real ``source_health`` table + the real ``autoheal.health_monitor``
implementation via a fully-migrated DB fixture; neither the migrations
system nor autoheal itself ports to the engine (both are host-owned — see
the Task 3 seam mapping table). What DOES port is the *contract*: run_ats_scan
must still call ``svc.run_detection`` / ``svc.run_heal_pass`` (the optional
ScanServices hooks that replaced the private source's direct
``autoheal.health_monitor.run_detection`` / ``pipeline_runner._run_heal_pass``
imports), with a static no-escape guard mirroring the private test's
``inspect.getsource`` check plus a dynamic fake-services check.

Also covers the Task 3 amendment's single wiring site: run_ats_scan must
propagate ``services.prober_extensions`` into
``jobcannon.engine.ats_prober``'s module-global seam for the scan's
duration and restore the prior value afterward — even when the scan body
raises.
"""

from __future__ import annotations

import contextlib
import inspect
import sqlite3
from unittest.mock import patch

import pytest

from jobcannon.engine import ats_prober, services
from jobcannon.engine.ats_scanner import _run


def _fake_services(**overrides):
    @contextlib.contextmanager
    def factory(*, synchronous="FULL"):
        con = sqlite3.connect(":memory:")
        try:
            yield con
        finally:
            con.close()

    base = dict(
        connection_factory=factory,
        upsert_job=lambda *a, **k: None,
        set_jd_full=lambda *a, **k: None,
        upsert_company=lambda *a, **k: None,
        get_secret=lambda name, *, config=None: None,
        config={},
        jd_storage_max_chars=100_000,
    )
    base.update(overrides)
    return services.ScanServices(**base)


@pytest.fixture(autouse=True)
def _restore_prober_extensions():
    prior = ats_prober._prober_extensions
    yield
    ats_prober.set_prober_extensions(prior)


def _patch_all_phases():
    """Patch out every per-scan phase helper AND the progress-count helpers
    (which run raw SQL against the connection before any phase starts), so
    run_ats_scan's body can execute against a schema-less :memory: connection
    while still exercising the post-scan detection/heal-pass + top-level
    prober_extensions wiring."""
    return (
        patch("jobcannon.engine.ats_scanner._run._count_phase_a_eligible", return_value=0),
        patch("jobcannon.engine.ats_scanner._run.count_playwright_eligible", return_value=0),
        patch("jobcannon.engine.ats_scanner._run._count_phase_c_eligible", return_value=0),
        patch("jobcannon.engine.ats_scanner._run._run_ats_api_scan"),
        patch("jobcannon.engine.ats_scanner._run._run_playwright_scan"),
        patch("jobcannon.engine.ats_scanner._run._run_homepage_discovery_phase"),
        patch("jobcannon.engine.ats_scanner._run._run_html_fallback_scan"),
        patch("jobcannon.engine.ats_scanner._run._score_new_ats_jobs"),
        patch("jobcannon.engine.ats_scanner._run._log_ats_scan_run"),
    )


def test_run_ats_scan_wires_run_detection_and_heal_pass_statically():
    """No-escape static guard: run_ats_scan's engine port must still wire
    svc.run_detection and svc.run_heal_pass. The private test asserted this
    against run_ats_scan's own source; the Task 3 amendment split the body
    into a thin public wrapper (prober_extensions setup/restore) plus
    _run_ats_scan_body (the actual phases) — so the guard covers the union
    of both sources, not just run_ats_scan in isolation."""
    combined_source = inspect.getsource(_run.run_ats_scan) + inspect.getsource(
        _run._run_ats_scan_body
    )
    assert "run_detection" in combined_source, "run_ats_scan must call svc.run_detection"
    assert "run_heal_pass" in combined_source, "run_ats_scan must call svc.run_heal_pass"


def test_run_ats_scan_calls_run_detection_and_heal_pass_with_result():
    """Dynamic check: with every scan phase stubbed out, run_ats_scan still
    invokes the injected run_detection hook and threads its result into both
    the returned summary and the run_heal_pass call — mirroring the private
    source's behavior of running detection unconditionally after the scan
    and gating only the heal attempt on autoheal.heal_enabled (a config
    concern the injected svc.run_heal_pass hook now owns internally)."""
    detection_calls = []
    heal_calls = []

    def fake_run_detection(db_path, config):
        detection_calls.append((db_path, config))
        return ["ats:greenhouse"]

    def fake_run_heal_pass(db_path, config, degraded_sources):
        heal_calls.append((db_path, config, degraded_sources))

    svc = _fake_services(run_detection=fake_run_detection, run_heal_pass=fake_run_heal_pass)
    services.set_services(svc)
    try:
        patches = _patch_all_phases()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            config = {"profile": {"target_titles": []}}
            summary = _run.run_ats_scan("fake.db", config)
    finally:
        services.clear_services()

    assert detection_calls == [("fake.db", config)]
    assert heal_calls == [("fake.db", config, ["ats:greenhouse"])]
    assert summary["degraded_sources"] == ["ats:greenhouse"]


def test_run_ats_scan_skips_detection_and_heal_pass_when_unset():
    """Optional-hook skip semantics (mapping table): unset run_detection /
    run_heal_pass means the scan proceeds with no degraded sources reported
    and no heal attempt, rather than raising."""
    svc = _fake_services()  # run_detection / run_heal_pass default to None
    services.set_services(svc)
    try:
        patches = _patch_all_phases()
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            summary = _run.run_ats_scan("fake.db", {"profile": {"target_titles": []}})
    finally:
        services.clear_services()

    assert summary["degraded_sources"] == []


def test_run_ats_scan_wires_and_restores_prober_extensions():
    """Task 3 amendment: run_ats_scan is the single site that propagates
    services.prober_extensions into ats_prober's module-global seam for the
    scan's duration, restoring the PRIOR value afterward (not None) — so a
    caller that had already registered its own extensions via direct
    set_prober_extensions() for engine-only use is not clobbered."""
    sentinel_prior = object()
    sentinel_new = object()
    ats_prober.set_prober_extensions(sentinel_prior)

    observed_during_scan = []

    def fake_run_ats_api_scan(*a, **k):
        observed_during_scan.append(ats_prober._prober_extensions)

    svc = _fake_services(prober_extensions=sentinel_new)
    services.set_services(svc)
    try:
        with (
            patch("jobcannon.engine.ats_scanner._run._count_phase_a_eligible", return_value=0),
            patch("jobcannon.engine.ats_scanner._run.count_playwright_eligible", return_value=0),
            patch("jobcannon.engine.ats_scanner._run._count_phase_c_eligible", return_value=0),
            patch(
                "jobcannon.engine.ats_scanner._run._run_ats_api_scan",
                side_effect=fake_run_ats_api_scan,
            ),
            patch("jobcannon.engine.ats_scanner._run._run_playwright_scan"),
            patch("jobcannon.engine.ats_scanner._run._run_homepage_discovery_phase"),
            patch("jobcannon.engine.ats_scanner._run._run_html_fallback_scan"),
            patch("jobcannon.engine.ats_scanner._run._score_new_ats_jobs"),
            patch("jobcannon.engine.ats_scanner._run._log_ats_scan_run"),
        ):
            _run.run_ats_scan("fake.db", {"profile": {"target_titles": []}})
    finally:
        services.clear_services()

    assert observed_during_scan == [sentinel_new]
    # Restored to whatever was registered before this scan ran, not None.
    assert ats_prober._prober_extensions is sentinel_prior


def test_run_ats_scan_restores_prober_extensions_on_exception():
    """The restore must happen even when the scan body raises — it lives in
    a finally, not a bare post-call assignment."""
    ats_prober.set_prober_extensions("prior-value")

    svc = _fake_services(prober_extensions="new-value")
    services.set_services(svc)
    try:
        with patch(
            "jobcannon.engine.ats_scanner._run._count_phase_a_eligible",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(RuntimeError, match="boom"):
                _run.run_ats_scan("fake.db", {"profile": {"target_titles": []}})
    finally:
        services.clear_services()

    assert ats_prober._prober_extensions == "prior-value"
