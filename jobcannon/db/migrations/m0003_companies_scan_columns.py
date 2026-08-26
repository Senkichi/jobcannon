"""Migration 3 — companies scan/retry columns + widen ats_probe_status CHECK.

The Wave-1 m0001 companies table omitted eight columns that the ported engine's
reachable run_ats_scan paths read/write directly (name_raw, retry state, scan
bookkeeping, board cache) and constrained ats_probe_status to
('pending','hit','miss') — but _handle_scan_error writes 'error' on every
transient scan failure. This migration closes both gaps. name_raw is added
nullable here; a later PR makes upsert_company populate it (single-writer).
consecutive_empty_scans and last_scanned_at already shipped in m0001.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=3,
    description="companies scan/retry columns + ats_probe_status 'error' state",
    sql=[
        "ALTER TABLE companies ADD COLUMN name_raw text",
        "ALTER TABLE companies ADD COLUMN retry_count integer NOT NULL DEFAULT 0",
        "ALTER TABLE companies ADD COLUMN retry_after timestamptz",
        "ALTER TABLE companies ADD COLUMN miss_reason text",
        "ALTER TABLE companies ADD COLUMN careers_crawl_last_at timestamptz",
        "ALTER TABLE companies ADD COLUMN jobs_found_total integer NOT NULL DEFAULT 0",
        "ALTER TABLE companies ADD COLUMN last_scan_postings_json jsonb",
        "ALTER TABLE companies ADD COLUMN last_scan_cached_at timestamptz",
        "ALTER TABLE companies DROP CONSTRAINT companies_ats_probe_status_check",
        "ALTER TABLE companies ADD CONSTRAINT companies_ats_probe_status_check "
        "CHECK (ats_probe_status IN ('pending','hit','miss','error'))",
    ],
)
