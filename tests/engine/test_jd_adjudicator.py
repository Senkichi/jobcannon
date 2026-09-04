"""Tests for jobcannon.engine.jd_adjudicator (L-0189, engine half).

Porting note (mirrors tests/engine/test_job_scorer.py's own porting note): the
private repo mocked the provider cascade by patching the module-level
``job_finder.web.jd_adjudicator.call_model`` import. The engine has no such
import — ``call_model`` is a required keyword-only parameter of
``adjudicate_jd`` — so every test below passes a fake callable (or
``FakeModelResult``-returning callable) directly as ``call_model=`` instead of
patching an import.

# PORT-SEAM: ported from tests/test_jd_adjudicator.py @ 0cbf333a's
# "adjudicate_jd — call wiring" section (test_adjudicate_true through
# test_adjudicate_pins_short_timeout). No DB needed -- adjudicate_jd's `conn`
# param is an opaque handle only threaded to call_model, never touched
# directly, so these use a plain sentinel object instead of a real
# connection/db fixture.
"""

from __future__ import annotations

from jobcannon.engine.jd_adjudicator import _ADJUDICATION_TIMEOUT_S, adjudicate_jd

from .conftest import FakeModelResult

# Body content is irrelevant to these tests (call_model is faked), but must be
# non-empty -- adjudicate_jd short-circuits to None on falsy jd_full.
_JD = "Acme is looking for a Senior Data Scientist. " * 10

_CONN = object()  # opaque sentinel; never touched, only threaded to call_model


def test_adjudicate_true():
    def fake_call_model(**kwargs):
        return FakeModelResult(data={"is_job_description": True, "confidence": 0.9})

    assert (
        adjudicate_jd(_CONN, "Data Scientist", "Acme", _JD, call_model=fake_call_model, config={})
        is True
    )


def test_adjudicate_false():
    def fake_call_model(**kwargs):
        return FakeModelResult(data={"is_job_description": False})

    assert (
        adjudicate_jd(_CONN, "Data Scientist", "Acme", _JD, call_model=fake_call_model, config={})
        is False
    )


def test_adjudicate_error_returns_none():
    def fake_call_model(**kwargs):
        raise RuntimeError("boom")

    assert (
        adjudicate_jd(_CONN, "Data Scientist", "Acme", _JD, call_model=fake_call_model, config={})
        is None
    )


def test_adjudicate_missing_field_returns_none():
    def fake_call_model(**kwargs):
        return FakeModelResult(data={"confidence": 0.5})  # no is_job_description

    assert (
        adjudicate_jd(_CONN, "Data Scientist", "Acme", _JD, call_model=fake_call_model, config={})
        is None
    )


def test_adjudicate_empty_jd_returns_none():
    def fake_call_model(**kwargs):
        raise AssertionError("call_model must not be called when jd_full is falsy")

    assert (
        adjudicate_jd(_CONN, "Data Scientist", "Acme", None, call_model=fake_call_model, config={})
        is None
    )


def test_adjudicate_pins_short_timeout():
    """The 128-token yes/no must fail fast: adjudicate_jd pins an explicit short
    request timeout instead of inheriting a provider's generous default (Ollama's
    300s). A stuck call would otherwise freeze the whole backfill for minutes
    before recovering."""
    captured: dict = {}

    def fake_call_model(**kwargs):
        captured.update(kwargs)
        return FakeModelResult(data={"is_job_description": True})

    adjudicate_jd(_CONN, "Data Scientist", "Acme", _JD, call_model=fake_call_model, config={})
    assert captured.get("timeout") == _ADJUDICATION_TIMEOUT_S
    # Must be well under the 300s provider default that motivated this fix.
    assert 0 < _ADJUDICATION_TIMEOUT_S <= 120


def test_adjudicate_threads_conn_to_call_model():
    """`conn` is kept and threaded into call_model (cost recording), matching
    job_scorer.score_job's convention verbatim -- not dropped as dead plumbing."""
    captured: dict = {}

    def fake_call_model(**kwargs):
        captured.update(kwargs)
        return FakeModelResult(data={"is_job_description": True})

    adjudicate_jd(_CONN, "Data Scientist", "Acme", _JD, call_model=fake_call_model, config={})
    assert captured.get("conn") is _CONN
