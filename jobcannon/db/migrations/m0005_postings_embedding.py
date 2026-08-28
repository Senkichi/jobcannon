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
default. An earlier version of this note justified that on the premise that
`postings` holds zero rows at migration time — factually wrong: this
migration runs whenever the pending-migration queue reaches it, which for a
live app is not guaranteed to be before any postings exist, and nothing here
constrains `postings`' row count. The real basis is narrower: `embedding` is
a column THIS SAME migration adds, via a bare `ADD COLUMN` with no DEFAULT,
so Postgres leaves it NULL for every row that already exists at the instant
the column comes into being — nothing could have written a value into it
before that ALTER TABLE statement runs, because the column did not exist
yet. pgvector's HNSW build skips NULL vector values entirely, so the graph
the immediately-following `CREATE INDEX` produces has zero nodes no matter
how large `postings` is — the actual indexing work is nil. That does NOT
make the SHARE lock's hold time zero: a non-CONCURRENT `CREATE INDEX` still
heap-scans every row in `postings` to see that each one's `embedding` is
NULL, so the lock is held for that scan, which scales with `postings`'
total row count at deploy time — not with the (zero) count of vectors
actually indexed. The accepted trade is a heap-scan-only SHARE lock over a
column nothing yet reads or writes (no application code queries or sets
`embedding` before this migration ships it), which is materially cheaper
than a real HNSW build over populated vectors — not that it is instant.
CONCURRENTLY (and the autocommit=True it would require) was judged not
worth the added complexity for a scan against a column with no existing
readers to contend with. Retroactively annotated when Rule 3 landed (#219),
same shape as m0003's contract_step annotation for #199/#218.
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
