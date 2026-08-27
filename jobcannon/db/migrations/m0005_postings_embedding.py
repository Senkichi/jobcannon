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

Lock justification: `idx_postings_embedding_hnsw` builds against `postings`,
a table m0001 (an EARLIER migration) created, so
tests/test_migration_deploy_safety.py's Rule 3 (issue #219) flags it by
default — but at the time this migration runs, `postings` holds zero rows:
this is a brand-new, not-yet-deployed pgvector feature, so nothing has ever
written an `embedding` value, and the table's row count at deploy time is
whatever earlier migrations/ingestion put there, which for this specific
column is always zero on first apply. A `CREATE INDEX` build against an
empty table is instant regardless of CONCURRENTLY vs. not, so the SHARE lock
Rule 3 warns about is held for a negligible duration here — CONCURRENTLY
(and the autocommit=True it would require) buys nothing this migration
needs. Retroactively annotated when Rule 3 landed (#219), same shape as
m0003's contract_step annotation for #199/#218.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

# See "Lock justification:" above -- required alongside this flag by
# tests/test_migration_deploy_safety.py (issue #219).
lock_step = True

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
