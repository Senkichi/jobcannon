"""Self-hosted per-posting JD embeddings, batch-computed at ingest (1B spec:
"self-hosted embeddings batch-computed per posting at ingest and cached").

Mirrors jobcannon.host.structural_axes.score_pending_structural_axes: a
versioned re-sweep (embedding_model_version IS DISTINCT FROM the current
version) so a model bump re-embeds the whole corpus with no separate backfill,
single-writer (this module is the ONLY writer of postings.embedding /
embedding_model_version / embedded_at), and the same commit_unless_nested /
raw-unwrap contract.

Differs from the structural tail in two deliberate ways:
1. The pending SELECT gates on jd_full presence (you cannot embed nothing) —
   structural axes degrade to a no-JD verdict and so carry no gate.
2. Failure is best-effort at the run_scan_task call site (scan_tasks._embed_
   pending_best_effort), NOT propagated like the structural tail: embedding
   pulls a heavy native runtime (fastembed/onnxruntime) plus a first-run model
   download, no consumer reads embeddings yet (the ranker is a later wave), and
   the versioned re-sweep makes a skipped round self-healing — so an embedding-
   infra hiccup must not fail an otherwise-successful scan. It is logged loudly
   and surfaced in the task summary, never silent.

fastembed is imported lazily inside _construct_model so importing this module
(done at scan_tasks load) never pulls onnxruntime unless embedding actually
runs.

Concurrency hardening (PR-6 deferral debt, closed now that concurrency>1
workers exist): `_get_model` is guarded by a double-checked lock so N
concurrent workers construct the model exactly once, not N times, and a
negative-cache backs off re-attempting construction for JC_EMBED_RETRY_
BACKOFF_S seconds after a failure so a broken/absent onnxruntime doesn't
re-pay an expensive (and doomed) construction on every scan. `EmbeddingUn
availableError` is raised on both the original failure and every fast-failed
retry within the backoff window; `scan_tasks._embed_pending_best_effort`
already catches `Exception`, so this flows into the existing
`embedding_error` summary field with zero call-site changes — the "expensive
retry every scan" concern that call site previously documented is now
bounded by the backoff window.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from pgvector.psycopg import register_vector

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_MODEL_VERSION = "bge-small-en-v1.5"
EMBEDDING_DIM = 384

_RETRY_BACKOFF_S = int(os.environ.get("JC_EMBED_RETRY_BACKOFF_S", "3600"))

__all__ = [
    "EMBEDDING_MODEL_VERSION",
    "EMBEDDING_DIM",
    "EmbeddingUnavailableError",
    "embed_pending_postings",
]

_model: Any = None
_model_lock = threading.Lock()
_model_unavailable_until: float = 0.0  # time.monotonic() deadline; in-process only


class EmbeddingUnavailableError(RuntimeError):
    """Model construction failed recently; failing fast until the backoff
    deadline so every scan doesn't re-pay a doomed model download."""


def _construct_model() -> Any:
    """Build the TextEmbedding model. fastembed downloads the ONNX model
    (~130 MB) to its cache on first use. Imported here (not at module top) so
    onnxruntime stays out of the import graph until embedding actually runs."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=_MODEL_NAME)


def _get_model() -> Any:
    """Lazily construct and memoize the TextEmbedding model, guarded by a
    double-checked lock (one construction under concurrency, not N) and a
    negative-cache backoff (a failed construction fails fast for
    JC_EMBED_RETRY_BACKOFF_S seconds instead of re-attempting every call)."""
    global _model, _model_unavailable_until
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if time.monotonic() < _model_unavailable_until:
            raise EmbeddingUnavailableError(
                f"embedding model construction failed within the last {_RETRY_BACKOFF_S}s; backing off"
            )
        try:
            _model = _construct_model()
        except Exception as exc:
            _model_unavailable_until = time.monotonic() + _RETRY_BACKOFF_S
            raise EmbeddingUnavailableError(f"embedding model unavailable: {exc}") from exc
    return _model


def embed_pending_postings(conn: Any, config: Any, *, batch_size: int = 500) -> int:
    """Embed up to batch_size postings not yet embedded under
    EMBEDDING_MODEL_VERSION; return the number embedded.

    `conn` may be a bare psycopg connection or an EngineCompatConnection —
    unwrapped to `.raw` exactly as score_pending_structural_axes does, so this
    host-native %s SQL never routes through the qmark-translation shim.

    `config` is accepted for call-site parity (unused; no tunables yet).

    Pending predicate mirrors the structural versioned re-sweep (IS DISTINCT
    FROM, so NULL/never-embedded and stale-version rows are both picked up) but
    ADDS a jd_full-presence gate (jd_full ~ '\\S' — at least one non-whitespace
    char, so NULL, empty, and tab/newline-only text are all excluded), and
    excluding such rows from the candidate set (rather than leaving them
    perpetually NULL-and-reselected) keeps them from occupying batch slots
    every scan.

    Concurrency semantics change (PR-6 debt): the whole claim+embed+write cycle
    is now ONE batch transaction with `FOR UPDATE SKIP LOCKED` row claiming, so
    concurrent sweeps (N>1 workers) PARTITION the pending backlog instead of
    racing to double-embed or blocking on each other's rows. Rows stay LOCKED
    for the duration of the batch (seconds of ONNX compute); commit is
    batch-atomic — a crash mid-batch loses at most that batch's work, and the
    versioned re-sweep self-heals it next run. This deliberately trades away
    the previous per-row-commit partial-progress property for correct
    partitioning under concurrency.

    psycopg3 nesting note: on a bare pooled connection `raw.transaction()` is a
    real BEGIN/COMMIT (the connection is idle at entry — the structural sweep
    ahead of this one in run_scan_task commits its own work first); under the
    rollback-isolated `db_conn` test fixture it degrades to a SAVEPOINT, which
    still scopes `FOR UPDATE` row locks correctly. `register_vector` inside the
    transaction is fine (connection-level adapter registration, idempotent).
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    with raw.transaction():
        pending = raw.execute(
            "SELECT id, jd_full FROM postings "
            "WHERE jd_full ~ '\\S' "
            "AND embedding_model_version IS DISTINCT FROM %s "
            "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (EMBEDDING_MODEL_VERSION, batch_size),
        ).fetchall()
        if not pending:
            return 0
        register_vector(raw)
        model = _get_model()
        # .embed() yields float32 ndarrays aligned 1:1 with input order; fastembed
        # truncates each text to the model's 512-token window internally.
        vectors = list(model.embed([row["jd_full"] for row in pending]))
        for row, vec in zip(pending, vectors):
            raw.execute(
                "UPDATE postings SET embedding = %s, embedding_model_version = %s, "
                "embedded_at = now() WHERE id = %s",
                (vec, EMBEDDING_MODEL_VERSION, row["id"]),
            )
    return len(pending)
