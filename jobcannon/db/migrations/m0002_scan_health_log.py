"""Migration 2 — scan_health_log for the extraction_health recorder (spec §3.4)."""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=2,
    description="scan_health_log table backing the extraction_health recorder",
    sql=[
        """
        CREATE TABLE scan_health_log (
            id          bigserial PRIMARY KEY,
            recorded_at timestamptz NOT NULL DEFAULT now(),
            payload     jsonb NOT NULL
        )
        """,
        "CREATE INDEX idx_scan_health_log_recorded_at ON scan_health_log(recorded_at)",
    ],
)
