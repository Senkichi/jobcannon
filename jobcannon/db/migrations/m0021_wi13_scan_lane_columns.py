"""Migration 21 — WI-13 scan-lane columns (ats_scan_enabled, careers_scan_enabled).

Prerequisite for ledger row L-0040 (jobcannon/db/_company_state.py): the
company-state audit log's 6 tracked fields need all 6 companies columns to
exist before ``_TRACKED_FIELDS`` can snapshot them, and this host's schema
before this migration has only the single legacy ``scan_enabled`` boolean
(m0003). Mirrors the split the private repo made in
``job_finder/web/migrations/m209604315_per_source_scan_enabled.py`` (WI-13,
D16, #1840): one bit gating the ATS scanner (``ats_scan_enabled``), one
gating the careers-page crawler (``careers_scan_enabled``), so an ATS
demotion no longer silently disables careers discovery for the same
company.

This migration is the ONLY place these two columns are added. The wave-3
careers-crawler group's own migration (ledger L-0286, HOLD until that ADAPT
port lands — tracked: https://github.com/Senkichi/jobcannon/issues/310)
consumes these columns; it must not re-add them.

Backfill: both new columns copy the existing ``scan_enabled`` value
verbatim. ``companies.scan_enabled`` has been ``NOT NULL`` since m0001, so
this port needs no COALESCE the way private's NULL-tolerant
``COALESCE(scan_enabled, 1)`` did. This host had no
``careers_crawl_flag_reason`` column at the time this migration ran (the
careers crawler was unported — ledger L-0286; the column was added later
by m0028, issue #370), so there was no second signal to fold into
``careers_scan_enabled`` the way private's backfill combined
``scan_enabled`` with that reason column; both new columns therefore start
IDENTICAL to the legacy bit, preserving every company's current
scan-disabled state on both lanes rather than silently re-enabling
anything. The legacy ``scan_enabled`` column is retained, unchanged,
matching private's revert-safety rationale (a future writer keeps
dual-writing it until every reader is migrated off it).

Expand-safe by shape: both ``ADD COLUMN`` statements carry a constant
``DEFAULT``, and the ``UPDATE`` narrows no existing column and adds no
constraint — no ``contract_step`` needed (tests/test_migration_deploy_safety.py
treats ``ADD COLUMN ... NOT NULL DEFAULT <const>`` as a known-safe shape).

Inverted-order safety (issue #199): the backfill ``UPDATE`` targets
``companies``, a table an earlier migration (m0001) created, so
tests/test_migration_deploy_safety.py's Rule 2 flags it as a "who backfills
the stragglers written by the still-live previous release" hazard —
pre-deploy migrations now always run BEFORE the new release's code
(docs/deploy-runbook.md §3). This UPDATE is idempotent under that inverted
ordering (a straight column-to-column copy, safely re-runnable), but the
copy itself only ever happens ONCE, at migration time — no writer this host
has today (``jobcannon/db/_company_attribution.py``, ``jobcannon/engine/ats_prober.py``,
``jobcannon/engine/ats_scanner/_run.py``) co-writes ``ats_scan_enabled``/
``careers_scan_enabled`` alongside ``scan_enabled`` yet (see each file's own
"split reverted (invented column, no migration backs it)" PORT-SEAM notes —
stale prose as of this migration, corrected by whichever future row wires
that writer, not amended here to stay out of this row's scope). So the
"straggler" window is not just the narrow deploy-overlap gap; it persists
indefinitely until a follow-up ports the private WI-13 per-writer co-write
instrumentation (job_finder/db/_company_state.py's own docstring: "every
instrumented writer that sets scan_enabled = 0 sets ats_scan_enabled = 0 in
the same statement").

Stragglers: benign for this migration's actual blast radius. No live reader
consumes the split columns yet — every scan-selection query still reads
legacy ``scan_enabled`` (the PORT-SEAM notes named above) — so a company
whose ``scan_enabled`` changes after this migration commits (old writer,
pre-cutover, or any later writer before its own co-write instrumentation
lands) simply keeps its migration-time-backfilled ``ats_scan_enabled``/
``careers_scan_enabled`` value: no scan behavior changes, because nothing
reads those columns for scan decisions yet. The one present-day consumer,
``jobcannon/db/_company_state.py``'s audit log (ledger L-0040), records
whatever the split columns' current value is at write time — a stale split
column produces an audit-log gap (a real scan-lane toggle that the flat
``ats_scan_enabled``/``careers_scan_enabled`` history under-reports), not a
scan-behavior defect. Tracked as follow-up (per-writer co-write
instrumentation), not fixed inline here.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

# See "Inverted-order safety:" above -- required alongside this flag by
# tests/test_migration_deploy_safety.py (issue #199).
inverted_order_safe = True

MIGRATION = Migration(
    version=21,
    description="WI-13 scan-lane columns (ats_scan_enabled, careers_scan_enabled)",
    sql=[
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS ats_scan_enabled boolean NOT NULL DEFAULT true",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS careers_scan_enabled boolean NOT NULL DEFAULT true",
        "UPDATE companies SET ats_scan_enabled = scan_enabled, careers_scan_enabled = scan_enabled",
    ],
)
