"""ADAPTED from job_finder/web/nightly_monitor/_signatures.py
@ e1f47695b07f928e6c91cc64767c97a99645d68f (private job-cannon).
Ledger L-0471.

# PORT-SEAM: the signature registry stays config-driven ({field, severity}
# style predicates), but the matching target changes shape. Private matched
# LITERAL SUBSTRINGS against free-text log lines read from app.log; there is
# no app.log on this host -- jobcannon.host.health_recorder.record_scan_health
# writes one structured jsonb payload per row instead. Signatures here are
# therefore (field, op, value, severity) predicates evaluated against a
# scan_health_log payload (or an equivalent procrastinate-failure dict), not
# a regex/substring match. This is a full rewrite of the matching mechanism,
# not an import-rewrite: exempted from verbatim fidelity-diff comparison
# (same precedent as jobcannon/host/model_provider.py's module docstring)
# and verified instead by tests/host/test_nightly_signatures.py. The
# data-driven contract itself -- no signature strings live in code -- stays.

Data-driven: no signature strings live in code; the registry is a
caller-supplied ``list[dict]`` of ``{field, op, value, severity}``
predicates (config source: jobcannon.host.nightly.config, a later env-var
addition once a concrete registry is needed -- the sampler that calls this
module currently ships with an EMPTY default registry, matching the "no LLM
spend / no issues filed" posture of this unit's dark rollout).

``worst_severity`` carries over UNCHANGED from the private original -- it is
a pure function over a hit list's ``severity`` field and never depended on
how a hit's text was matched.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SEVERITY_ORDER = {"info": 0, "anomaly": 1, "fail": 2}
_VALID_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "contains", "truthy", "falsy")


def _get_field(payload: Mapping[str, Any], field: str) -> Any:
    """Dotted-path lookup into a nested jsonb payload (e.g. ``"detect.blocked"``)."""
    node: Any = payload
    for part in field.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _predicate_matches(actual: Any, op: str, value: Any) -> bool:
    if op == "truthy":
        return bool(actual)
    if op == "falsy":
        return not actual
    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "contains":
        try:
            return value in actual
        except TypeError:
            return False
    # gt/gte/lt/lte: only meaningful for a comparable (numeric) actual value.
    if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if op == "gt":
        return actual > value
    if op == "gte":
        return actual >= value
    if op == "lt":
        return actual < value
    if op == "lte":
        return actual <= value
    return False


def match_signatures(payload: dict, signatures: list[dict]) -> list[dict]:
    """One hit per matching ``{field, op, value, severity}`` predicate.

    Each hit is ``{"field": ..., "op": ..., "value": ..., "severity": ...,
    "actual": ...}`` — ``actual`` is the payload value the predicate matched
    against, carried through for the checkpoint packet / morning report
    (mirrors the private hit's ``line`` field: evidence a human or a later
    review stage can read without re-querying the source row).

    A malformed predicate entry (missing ``field``/``op``/``severity``, or
    an unrecognized ``op``) is skipped rather than raising — a bad registry
    entry must not crash the sampler tick.
    """
    hits: list[dict] = []
    if not payload or not signatures:
        return hits
    for sig in signatures:
        field = sig.get("field")
        op = sig.get("op")
        severity = sig.get("severity")
        if not field or op not in _VALID_OPS or severity not in _SEVERITY_ORDER:
            continue
        actual = _get_field(payload, field)
        if _predicate_matches(actual, op, sig.get("value")):
            hits.append(
                {
                    "field": field,
                    "op": op,
                    "value": sig.get("value"),
                    "severity": severity,
                    "actual": actual,
                }
            )
    return hits


def worst_severity(hits: list[dict]) -> str | None:
    """Highest severity present among hits (info < anomaly < fail), or None."""
    if not hits:
        return None
    return max(hits, key=lambda h: _SEVERITY_ORDER.get(h.get("severity"), 0)).get("severity")
