"""Shared fixtures/helpers for jobcannon.engine tests."""

from __future__ import annotations

from dataclasses import dataclass


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
