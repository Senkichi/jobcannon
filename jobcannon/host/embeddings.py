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

fastembed is imported lazily inside _get_model so importing this module (done
at scan_tasks load) never pulls onnxruntime unless embedding actually runs.
"""

from __future__ import annotations

from typing import Any

from pgvector.psycopg import register_vector

from jobcannon.db.pool import commit_unless_nested

_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_MODEL_VERSION = "bge-small-en-v1.5"
EMBEDDING_DIM = 384

__all__ = [
    "EMBEDDING_MODEL_VERSION",
    "EMBEDDING_DIM",
    "embed_pending_postings",
]

_model: Any = None


def _get_model() -> Any:
    """Lazily construct and memoize the TextEmbedding model.

    fastembed downloads the ONNX model (~130 MB) to its cache on first use,
    then reuses it. Memoized module-level so repeated scan tasks in one worker
    process don't reload it. Imported here (not at module top) so onnxruntime
    stays out of the import graph until embedding actually runs.
    """
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=_MODEL_NAME)
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
    ADDS a jd_full-presence gate (btrim <> '') — an empty JD is not embeddable,
    and excluding such rows from the candidate set (rather than leaving them
    perpetually NULL-and-reselected) keeps them from occupying batch slots
    every scan.
    """
    raw = conn.raw if hasattr(conn, "raw") else conn
    pending = raw.execute(
        "SELECT id, jd_full FROM postings "
        "WHERE jd_full IS NOT NULL AND btrim(jd_full) <> '' "
        "AND embedding_model_version IS DISTINCT FROM %s LIMIT %s",
        (EMBEDDING_MODEL_VERSION, batch_size),
    ).fetchall()
    if not pending:
        return 0

    register_vector(raw)
    model = _get_model()
    # .embed() yields float32 ndarrays aligned 1:1 with input order; fastembed
    # truncates each text to the model's 512-token window internally.
    vectors = list(model.embed([row["jd_full"] for row in pending]))

    n = 0
    for row, vec in zip(pending, vectors):
        with raw.transaction():
            raw.execute(
                "UPDATE postings SET embedding = %s, embedding_model_version = %s, "
                "embedded_at = now() WHERE id = %s",
                (vec, EMBEDDING_MODEL_VERSION, row["id"]),
            )
        commit_unless_nested(raw)
        n += 1
    return n
