"""DB-free coverage for jobcannon.db.pool.unwrap_raw (issue #334).

unwrap_raw is the shared form of the ``conn.raw if hasattr(conn, "raw")
else conn`` idiom that ``record_score_audit`` / ``select_audit_candidates``
(jobcannon/db/_score_audits.py) and the nightly modules' private ``_raw()``
helpers used to inline. These tests pin both arms of the contract -- a bare
psycopg-shaped connection passes through by identity, and anything exposing
``.raw`` (the EngineCompatConnection facade connection_factory() yields, or
a facade-shaped test double) unwraps to it -- without needing live Postgres.
"""

from __future__ import annotations

from jobcannon.db.pool import EngineCompatConnection, unwrap_raw


class _BareConn:
    """Bare-connection stand-in: no ``.raw`` attribute."""


class _FacadeDouble:
    """Facade-shaped test double: duck-typed ``.raw``, deliberately NOT an
    EngineCompatConnection -- the contract is hasattr-based, not isinstance."""

    def __init__(self, raw):
        self.raw = raw


def test_bare_connection_passes_through_by_identity():
    conn = _BareConn()
    assert unwrap_raw(conn) is conn


def test_engine_compat_connection_unwraps_to_raw():
    inner = _BareConn()
    facade = EngineCompatConnection(inner)
    assert unwrap_raw(facade) is inner


def test_duck_typed_raw_attribute_unwraps():
    inner = _BareConn()
    assert unwrap_raw(_FacadeDouble(inner)) is inner
