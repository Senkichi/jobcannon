"""Embedding tail: embed_pending_postings + its run_scan_task wiring, live on
Postgres. The fastembed model is stubbed (deterministic 384-dim float32) so the
test is hermetic and fast while still driving the REAL pgvector path —
register_vector binding, the vector(384) INSERT, the versioned-re-sweep
predicate, the jd_full gate, and commit_unless_nested — against a live DB with
the m0005 extension+column+index applied. The real fastembed model (id + output
dimension) is validated separately by tests/host/test_embeddings_model.py."""

from datetime import date

import numpy as np

from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres


class _StubModel:
    def embed(self, texts):
        for _ in texts:
            yield np.full(384, 0.1, dtype=np.float32)


@requires_postgres
def test_embed_tail_via_run_scan_task_embeds_only_jd_postings(monkeypatch):
    from jobcannon.db.migrate import run_migrations
    from jobcannon.engine import services
    from jobcannon.host import embeddings
    from jobcannon.host.config import HostConfig
    from jobcannon.host.embeddings import EMBEDDING_MODEL_VERSION
    from jobcannon.host.scan_tasks import run_scan_task
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

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
                # Pending + embeddable (has jd_full).
                conn.execute(
                    "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
                    "posted_date, posted_date_precision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        "embed-yes",
                        company_id,
                        "Engineer",
                        "TestCo",
                        "We are hiring an engineer to build and scale distributed systems.",
                        date(2026, 7, 15),
                        "exact",
                    ),
                )
                # Pending but NOT embeddable (jd_full NULL).
                conn.execute(
                    "INSERT INTO postings (dedup_key, company_id, title, company, jd_full, "
                    "posted_date, posted_date_precision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("embed-no", company_id, "Analyst", "TestCo", None, date(2026, 7, 15), "exact"),
                )
                conn.commit()

            summary = run_scan_task(company_names=["TestCo"])

            assert summary["errors"] == []
            assert summary["postings_embedded"] == 1

            with svc.connection_factory() as conn:
                yes = conn.execute(
                    "SELECT embedding_model_version, embedded_at, "
                    "vector_dims(embedding) AS dims FROM postings WHERE dedup_key = ?",
                    ("embed-yes",),
                ).fetchone()
                no = conn.execute(
                    "SELECT embedding_model_version, embedding FROM postings WHERE dedup_key = ?",
                    ("embed-no",),
                ).fetchone()

            assert yes["embedding_model_version"] == EMBEDDING_MODEL_VERSION
            assert yes["embedded_at"] is not None
            assert yes["dims"] == 384
            assert no["embedding_model_version"] is None
            assert no["embedding"] is None
        finally:
            teardown_engine_seams()
    finally:
        drop_throwaway_db(db_name)


def test_embed_tail_swallows_failure_and_returns_none(monkeypatch):
    """The best-effort tail: an embedding-infra failure is swallowed+logged,
    returning None, so it never fails an otherwise-successful scan."""
    from jobcannon.host import scan_tasks

    def boom(conn, config, **kwargs):
        raise RuntimeError("onnxruntime unavailable")

    monkeypatch.setattr(scan_tasks, "embed_pending_postings", boom)
    assert scan_tasks._embed_pending_best_effort(object(), {}) is None
