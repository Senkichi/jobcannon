"""PR-6 deferral debts, due now that concurrency=2 workers exist:
SKIP LOCKED batch partitioning on the embed sweep, the _get_model
construction lock, and the model negative-cache. No threading needed for
the partition proof: conn_a holds real row locks in an open transaction;
conn_b's sweep must skip them, not block or double-embed."""

import threading

import numpy as np
import pytest

from tests.host.conftest import requires_postgres


class _StubModel:
    def embed(self, texts):
        for t in texts:
            yield np.full(384, float(len(t) % 7) / 10.0, dtype=np.float32)


@requires_postgres
def test_concurrent_embed_sweeps_partition_not_duplicate(
    db_conn_pair, seeded_pending_postings, monkeypatch
):
    from jobcannon.host import embeddings

    monkeypatch.setattr(embeddings, "_get_model", lambda: _StubModel())
    conn_a, conn_b = db_conn_pair
    ids = seeded_pending_postings
    # conn_a locks the first 2 rows in an open (uncommitted) transaction:
    conn_a.execute(
        "SELECT id FROM postings WHERE id = ANY(%s) ORDER BY id LIMIT 2 FOR UPDATE", (ids,)
    ).fetchall()
    # conn_b's sweep must SKIP the locked pair and embed exactly the other 2:
    assert embeddings.embed_pending_postings(conn_b, {}, batch_size=4) == 2
    conn_a.rollback()  # release the locks
    # Second sweep picks up exactly the formerly-locked pair:
    assert embeddings.embed_pending_postings(conn_b, {}, batch_size=4) == 2
    # And a third finds nothing pending (versioned re-sweep satisfied):
    assert embeddings.embed_pending_postings(conn_b, {}, batch_size=4) == 0


def test_get_model_negative_cache_backoff(monkeypatch):
    from jobcannon.host import embeddings

    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("onnxruntime broken")

    monkeypatch.setattr(embeddings, "_construct_model", _boom)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_model_unavailable_until", 0.0)
    with pytest.raises(embeddings.EmbeddingUnavailableError):
        embeddings._get_model()
    # Within the backoff window: fails fast WITHOUT re-constructing.
    with pytest.raises(embeddings.EmbeddingUnavailableError):
        embeddings._get_model()
    assert len(calls) == 1


def test_get_model_single_construction_under_concurrency(monkeypatch):
    from jobcannon.host import embeddings

    constructed = []

    def _slow_construct():
        constructed.append(1)
        return _StubModel()

    monkeypatch.setattr(embeddings, "_construct_model", _slow_construct)
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_model_unavailable_until", 0.0)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(embeddings._get_model())) for _ in range(8)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(constructed) == 1
    assert len({id(r) for r in results}) == 1
