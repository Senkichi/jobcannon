"""Tests for jobcannon/host/nightly/signatures.py (ledger L-0471).

signatures.py's own module docstring names this file as the guard replacing
verbatim fidelity-diff comparison for the matching mechanism (a full
rewrite from substring-match-over-log-lines to predicate-match-over-a-jsonb
payload -- see that module's PORT-SEAM comment). ``worst_severity`` is the
one function claimed byte-identical to the private original; it is covered
here too so a future edit that breaks that claim fails a test, not just a
docstring's word.
"""

from __future__ import annotations

from jobcannon.host.nightly.signatures import match_signatures, worst_severity


def test_match_signatures_empty_inputs_yield_no_hits():
    assert (
        match_signatures({}, [{"field": "status", "op": "eq", "value": "ok", "severity": "fail"}])
        == []
    )
    assert match_signatures({"status": "ok"}, []) == []


def test_match_signatures_eq_and_dotted_field():
    payload = {"status": "error", "detect": {"blocked": True}}
    sigs = [
        {"field": "status", "op": "eq", "value": "error", "severity": "fail"},
        {"field": "detect.blocked", "op": "truthy", "value": None, "severity": "anomaly"},
        {"field": "detect.missing", "op": "truthy", "value": None, "severity": "anomaly"},
    ]
    hits = match_signatures(payload, sigs)
    assert len(hits) == 2
    fields = {h["field"] for h in hits}
    assert fields == {"status", "detect.blocked"}
    status_hit = next(h for h in hits if h["field"] == "status")
    assert status_hit == {
        "field": "status",
        "op": "eq",
        "value": "error",
        "severity": "fail",
        "actual": "error",
    }


def test_match_signatures_numeric_comparisons():
    payload = {"used_pct": 0.92}
    sigs = [{"field": "used_pct", "op": "gte", "value": 0.9, "severity": "anomaly"}]
    assert len(match_signatures(payload, sigs)) == 1
    sigs_below = [{"field": "used_pct", "op": "gte", "value": 0.95, "severity": "anomaly"}]
    assert match_signatures(payload, sigs_below) == []


def test_match_signatures_comparison_ops_reject_non_numeric_actual():
    # actual is a bool/str -- gt/gte/lt/lte must not coerce it, per
    # _predicate_matches's explicit isinstance guard.
    payload = {"flag": True, "name": "x"}
    sigs = [
        {"field": "flag", "op": "gt", "value": 0, "severity": "info"},
        {"field": "name", "op": "lt", "value": 5, "severity": "info"},
    ]
    assert match_signatures(payload, sigs) == []


def test_match_signatures_falsy_and_ne():
    payload = {"error": None, "status": "ok"}
    sigs = [
        {"field": "error", "op": "falsy", "value": None, "severity": "info"},
        {"field": "status", "op": "ne", "value": "error", "severity": "info"},
    ]
    assert len(match_signatures(payload, sigs)) == 2


def test_match_signatures_skips_malformed_predicate():
    payload = {"status": "error"}
    sigs = [
        {"field": "status", "op": "not_a_real_op", "value": "error", "severity": "fail"},
        {"field": "status", "op": "eq", "value": "error", "severity": "not_a_real_severity"},
        {"op": "eq", "value": "error", "severity": "fail"},  # missing field
        {"field": "status", "value": "error", "severity": "fail"},  # missing op
    ]
    assert match_signatures(payload, sigs) == []


def test_match_signatures_contains_op_handles_non_container_actual():
    # contains against a non-container actual must not raise (TypeError
    # caught and treated as no match).
    payload = {"count": 5}
    sigs = [{"field": "count", "op": "contains", "value": "5", "severity": "info"}]
    assert match_signatures(payload, sigs) == []


def test_worst_severity_orders_info_anomaly_fail():
    assert worst_severity([]) is None
    assert (
        worst_severity(
            [
                {"severity": "info"},
                {"severity": "anomaly"},
                {"severity": "info"},
            ]
        )
        == "anomaly"
    )
    assert (
        worst_severity(
            [
                {"severity": "fail"},
                {"severity": "anomaly"},
            ]
        )
        == "fail"
    )
