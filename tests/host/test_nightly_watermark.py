"""Tests for jobcannon/host/nightly/watermark.py (issue #421).

fetch_since_watermark is the shared "rows with id > cursor, bounded, plus
new high-water id" read sampler._tick used to spell out twice inline. These
tests are DB-free: the helper only calls unwrap_raw(conn).execute(query,
params).fetchall(), so a fake connection records the query/params and
returns scripted rows. sampler._tick's own watermark-advance policy
(drained-tick-only for the procrastinate cursor) stays in the sampler tests.
"""

from __future__ import annotations

from jobcannon.host.nightly.watermark import fetch_since_watermark

_QUERY = "SELECT id, payload FROM t WHERE id > %(since_id)s ORDER BY id LIMIT %(limit)s"


class _FakeRaw:
    """Records execute() calls and returns scripted rows from fetchall()."""

    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        return self

    def fetchall(self):
        return self._rows


class _FakeFacade:
    """EngineCompatConnection-shaped wrapper: unwrap_raw must reach `.raw`."""

    def __init__(self, raw):
        self.raw = raw


def test_returns_rows_and_high_water_id():
    raw = _FakeRaw([{"id": 41, "payload": {"a": 1}}, {"id": 57, "payload": {"b": 2}}])
    rows, new_watermark = fetch_since_watermark(raw, _QUERY, since_id=40, limit=200)
    assert rows == [{"id": 41, "payload": {"a": 1}}, {"id": 57, "payload": {"b": 2}}]
    assert new_watermark == 57


def test_empty_fetch_returns_since_id_unchanged():
    raw = _FakeRaw([])
    rows, new_watermark = fetch_since_watermark(raw, _QUERY, since_id=40, limit=200)
    assert rows == []
    assert new_watermark == 40


def test_binds_since_id_limit_and_extra_named_params():
    raw = _FakeRaw([{"id": 7}])
    fetch_since_watermark(
        raw,
        "SELECT id FROM t WHERE status = ANY(%(statuses)s) AND id > %(since_id)s "
        "ORDER BY id LIMIT %(limit)s",
        since_id=3,
        limit=50,
        params={"statuses": ["succeeded", "failed"]},
    )
    assert raw.calls == [
        (
            "SELECT id FROM t WHERE status = ANY(%(statuses)s) AND id > %(since_id)s "
            "ORDER BY id LIMIT %(limit)s",
            {"since_id": 3, "limit": 50, "statuses": ["succeeded", "failed"]},
        )
    ]


def test_unwraps_facade_connection():
    raw = _FakeRaw([{"id": 9}])
    rows, new_watermark = fetch_since_watermark(_FakeFacade(raw), _QUERY, since_id=0, limit=10)
    assert rows == [{"id": 9}]
    assert new_watermark == 9
    assert len(raw.calls) == 1
