"""Migration 5 — pgvector embedding column + HNSW index (fulfills the m0001
"pgvector deferred to Wave 2" note).

CREATE EXTENSION runs FIRST so the vector(384) column type resolves within this
same transaction (Postgres DDL sees prior statements' effects mid-txn). The
whole migration applies inside one transaction (migrate._apply_migration); a
plain CREATE INDEX (not CONCURRENTLY) is transactional and instant on the empty
postings table at migration time, so the HNSW graph builds incrementally as
postings land later.

embedding_model_version already exists (m0001) — it is the versioned-re-sweep
key jobcannon.host.embeddings.embed_pending_postings gates on; this migration
only adds the vector payload (`embedding`) and its write-timestamp
(`embedded_at`). Dimension 384 is BAAI/bge-small-en-v1.5's output width
(jobcannon.host.embeddings.EMBEDDING_DIM); vector_cosine_ops because the future
retrieve-then-rank stage measures cosine distance (<=>).
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=5,
    description="pgvector: postings.embedding vector(384) + embedded_at + HNSW cosine index",
    sql=[
        "CREATE EXTENSION IF NOT EXISTS vector",
        "ALTER TABLE postings ADD COLUMN embedding vector(384)",
        "ALTER TABLE postings ADD COLUMN embedded_at timestamptz",
        "CREATE INDEX idx_postings_embedding_hnsw ON postings "
        "USING hnsw (embedding vector_cosine_ops)",
    ],
)
