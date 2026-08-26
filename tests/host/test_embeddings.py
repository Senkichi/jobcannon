"""Embedding tail: embed_pending_postings + its run_scan_task wiring, live on
Postgres. The fastembed model is stubbed with DETERMINISTIC PER-TEXT vectors of
width EMBEDDING_DIM (not a hardcoded literal) so the test drives the REAL
pgvector path — register_vector binding, the vector(384) INSERT, the versioned-
re-sweep predicate, the jd_full gate, commit_unless_nested — AND can detect a
row/vector mis-pairing or an EMBEDDING_DIM/migration-vector(N) drift. The real
fastembed model (id + output dimension) is validated separately by
tests/host/test_embeddings_model.py."""

from datetime import date

import numpy as np
import pytest

from jobcannon.host.embeddings import EMBEDDING_DIM
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

_JD_1 = "We are hiring a senior backend engineer to build and scale our distributed systems."
_JD_2 = "Data analyst role: SQL and dashboards."


def _stub_fill(text: str) -> float:
    """Per-text-deterministic fill so distinct texts embed to distinct vectors
    (reveals a reversed/mispaired zip; identical constant values could not)."""
    return (len(text) % 97) / 100.0


class _StubModel:
    def embed(self, texts, batch_size=256):
        for t in texts:
            yield np.full(EMBEDDING_DIM, _stub_fill(t), dtype=np.float32)


def test_migration_vector_width_matches_embedding_dim():
    """CI-safe (no Postgres, no model download): m0005's vector(N) width must
    equal EMBEDDING_DIM, so a future dimension bump can't silently drift the
    migration DDL and the constant apart."""
    from jobcannon.db.migrations.m0005_postings_embedding import MIGRATION

    ddl = " ".join(MIGRATION.sql)
    assert f"vector({EMBEDDING_DIM})" in ddl


def test_embed_tail_swallows_failure_and_returns_none(monkeypatch):
    """Best-effort tail: an embedding-infra failure is swallowed+logged,
    returning (None, <message>), so it never fails a successful scan."""
    from jobcannon.host import scan_tasks

    def boom(conn, config, **kwargs):
        raise RuntimeError("onnxruntime unavailable")

    monkeypatch.setattr(scan_tasks, "embed_pending_postings", boom)
    count, error = scan_tasks._embed_pending_best_effort(object(), {})
    assert count is None
    assert "onnxruntime unavailable" in error


@requires_postgres
def test_embed_tail_via_run_scan_task_pairs_vectors_and_gates_correctly(monkeypatch):
    from pgvector.psycopg import register_vector

    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services
    from jobcannon.host import embeddings
    from jobcannon.host.config import HostConfig
    from jobcannon.host.embeddings import EMBEDDING_MODEL_VERSION
    from jobcannon.host.scan_tasks import run_scan_task
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    # Distinct texts MUST embed to distinct stub vectors, else the alignment
    # assertion below is vacuous.
    assert _stub_fill(_JD_1) != _stub_fill(_JD_2)

    monkeypatch.setattr(embeddings, "_get_model", lambda: _StubModel())

    dsn, db_name = create_throwaway_db("jobcannon_embed_smoke")
    try:
        run_migrations(dsn)
        init_engine_seams(HostConfig(database_url=dsn, runtime={}))
        try:
            svc = services.get_services()
            with svc.connection_factory() as conn:
                conn.execute(
                    "INSERT INTO companies "
                    "(name, name_raw, ats_platform, ats_slug, ats_probe_status, scan_enabled) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("TestCo", "TestCo", "jobvite", "testco", "hit", True),
                )
                conn.commit()
                company_id = conn.execute(
                    "SELECT id FROM companies WHERE name = ?", ("TestCo",)
                ).fetchone()["id"]

                # Two embeddable postings with DISTINCT jd_full -> distinct stub
                # fills, so a mis-paired zip is detectable.
                for dk, jd in (("embed-yes-1", _JD_1), ("embed-yes-2", _JD_2)):
                    conn.execute(
                        "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
                        "posted_date, posted_date_precision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (dk, company_id, "Engineer", "TestCo", jd, date(2026, 7, 15), "exact"),
                    )
                # NULL jd_full -> not embeddable.
                conn.execute(
                    "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
                    "posted_date, posted_date_precision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "embed-null",
                        company_id,
                        "Analyst",
                        "TestCo",
                        None,
                        date(2026, 7, 15),
                        "exact",
                    ),
                )
                # Non-space WHITESPACE-only jd_full -> not embeddable (proves the
                # gate excludes tabs/newlines, which btrim(x) <> '' would not).
                conn.execute(
                    "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
                    "posted_date, posted_date_precision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "embed-blank",
                        company_id,
                        "Clerk",
                        "TestCo",
                        "\n\t\n",
                        date(2026, 7, 15),
                        "exact",
                    ),
                )
                conn.commit()

            summary = run_scan_task(company_names=["TestCo"])

            assert summary["errors"] == []
            assert summary["postings_embedded"] == 2
            assert summary["embedding_error"] is None

            with svc.connection_factory() as conn:
                register_vector(conn.raw)
                rows = {}
                for dk in ("embed-yes-1", "embed-yes-2", "embed-null", "embed-blank"):
                    rows[dk] = conn.raw.execute(
                        "SELECT embedding_model_version, embedded_at, embedding "
                        "FROM postings WHERE dedup_key = %s",
                        (dk,),
                    ).fetchone()

            # Each embeddable row got ITS OWN text's vector (alignment), full width.
            # register_vector binds the `embedding` column to pgvector's Vector
            # wrapper (not a plain list/ndarray) — it exposes .dimensions() and
            # .to_list()/.to_numpy() rather than __len__/__getitem__.
            for dk, jd in (("embed-yes-1", _JD_1), ("embed-yes-2", _JD_2)):
                r = rows[dk]
                assert r["embedding_model_version"] == EMBEDDING_MODEL_VERSION
                assert r["embedded_at"] is not None
                assert r["embedding"] is not None
                assert r["embedding"].dimensions() == EMBEDDING_DIM
                assert r["embedding"].to_list()[0] == pytest.approx(_stub_fill(jd), abs=1e-5)

            # NULL and whitespace-only rows are left unembedded by the gate.
            for dk in ("embed-null", "embed-blank"):
                assert rows[dk]["embedding_model_version"] is None
                assert rows[dk]["embedding"] is None

            # Versioned re-sweep is idempotent: a second scan re-embeds nothing.
            summary2 = run_scan_task(company_names=["TestCo"])
            assert summary2["postings_embedded"] == 0
            assert summary2["embedding_error"] is None
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)
