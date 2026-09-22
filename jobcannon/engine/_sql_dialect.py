"""Shared SQLite-dialect SQL fragments for engine-authored SQL (#401).

Engine SQL is written in SQLite dialect on purpose: ``tests/engine/``
exercises this exact SQL directly against a bare ``sqlite3`` connection with
no translation layer, and Postgres-only syntax (e.g. ``make_interval``'s
``=>`` token) is a SQLite parse error there. ``jobcannon/db/compat.py``'s
``engine_sql_to_host()`` — reached through ``EngineCompatConnection`` — is
the sole Postgres-translation seam for these shapes.

This module is the single emitter for the canonical interval fragment below,
previously hand-copied into ``ats_scanner/_run.py``'s
``_dormancy_gate_clause()``, ``careers_crawler/__init__.py``'s lane-1 query,
``careers_crawler/_bench_predicate.py``, ``ats_scanner/_scan_selection.py``,
and ``ats_scanner/_scan_log.py``. Centralizing it here means a future
"now minus N days" predicate cannot silently pick a variant the compat
rewrite does not recognize (the pre-negated-parameter shape that caused
public #380). The module is a leaf (no imports) so every engine package —
including ``careers_crawler/__init__.py``, which must stay import-inert at
module scope for the ``ats_platforms`` <-> ``_title_filters`` cycle — can
import it safely at module level.
"""


def sqlite_now_minus_days() -> str:
    """Canonical engine-dialect "now minus N days" expression.

    Returns ``datetime('now', ? || ' days')`` — the ONLY interval
    shape ``jobcannon/db/compat.py``'s ``_DATETIME_REWRITES[0]`` translates
    for Postgres (to ``now() - make_interval(days => %s)``). Bind the
    fragment's single ``?`` placeholder with a plain positive day count —
    the caller's freshness/decay/retention window as an ``int`` — at the
    position the fragment occupies in the surrounding query's parameter
    tuple.

    Do NOT fold the sign into the bound parameter: the pre-negated variant
    ``datetime('now', ? || ' days')`` bound with ``f"-{days}"`` matches no
    rewrite rule and reaches Postgres as a literal ``datetime(...)`` call,
    which does not exist there (public #380 — the defect class this helper
    exists to prevent). Other ``datetime('now', ...)`` shapes are separate
    concerns with their own rewrites: bare ``datetime('now')`` ->
    ``now()``, and bare-column ``datetime(<col>)`` -> ``<col>`` (see
    ``db/compat.py``'s module docstring for both).
    """
    return "datetime('now', '-' || ? || ' days')"
