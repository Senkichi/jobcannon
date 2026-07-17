"""Shared fixtures/helpers for jobcannon.engine tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@pytest.fixture(autouse=True)
def reset_scan_memo(monkeypatch):
    """Clear the run_platform_scan raw-postings memo and in-flight locks before every test.

    ``_scan_memo`` is a plain module-level dict in ``ats_platforms._registry``
    keyed only on ``(scanner.name, slug, max_pages)`` — NOT on scanner object
    identity — so tests that reuse a name/slug pair (e.g. "test"/"test-slug")
    across different fake scanners leak cached raw postings between them
    without this reset.
    """
    monkeypatch.setattr("jobcannon.engine.ats_platforms._registry._scan_memo", {})
    monkeypatch.setattr("jobcannon.engine.ats_platforms._registry._scan_memo_inflight", {})


@pytest.fixture(autouse=True)
def reset_scan_services():
    """Clear the host-injected ScanServices bundle before/after every test.

    Same leakage risk as ``_scan_memo`` / ``runtime_config._provider`` above:
    ``jobcannon.engine.services._active`` is a plain module-level global, so
    a test that registers services without cleaning up (e.g. an assertion
    failure before its own ``clear_services()``) would leak them into
    whichever test runs next.
    """
    from jobcannon.engine import services

    services.clear_services()
    yield
    services.clear_services()


@pytest.fixture(autouse=True)
def reset_runtime_config_provider():
    """Clear the host-injected runtime-config provider before/after every test.

    Same leakage risk as ``_scan_memo`` above: ``runtime_config._provider`` is
    a plain module-level global, so a test that registers a provider without
    this reset would leak it into whichever test runs next.
    """
    from jobcannon.engine import runtime_config

    runtime_config.set_config_provider(None)
    yield
    runtime_config.set_config_provider(None)


@dataclass
class FakeModelResult:
    """Duck-types job_finder.web.model_provider.ModelResult's shape.

    The engine does not import that class — model_provider is a host-side
    provider cascade, not ported here (see Phase 1A plan Task 4: job_scorer
    now takes an injected ``call_model`` callable instead). job_scorer only
    reads ``.data`` / ``.schema_valid`` / ``.provider`` / ``.model`` /
    ``.degenerate`` off whatever ``call_model`` returns (see
    ``_coerce_assessment`` and ``score_job`` in jobcannon.engine.job_scorer),
    so any object exposing these attributes satisfies the contract — this is
    the ported test suite's stand-in for the private repo's ModelResult
    dataclass, field-for-field identical.
    """

    data: dict
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "fake-model"
    provider: str = "fake"
    schema_valid: bool = True
    degenerate: bool = False
