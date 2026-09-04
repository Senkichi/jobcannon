"""Migration 23 -- careers crawler scan columns (L-0465).

Backs the ``jobcannon/engine/careers_crawler/_persistence.py`` writer this
migration's sibling PR lands (ledger L-0465, private source:
``job_finder/web/careers_crawler/_persistence.py`` @
23644885615a78d509d73c8b1c640b7d49b4a089). Adds the three columns that
writer needs and m0001/m0003/m0021 did not already carry:

- ``company_scan_log.source`` text, nullable, no default. Absorbs private
  migration ``m208996968_add_source_to_company_scan_log.py`` (ledger L-0274,
  HOLD until this port lands). Lets the already-landed 5-strike penalty-box
  predicate (``jobcannon/engine/careers_crawler/_bench_predicate.py``,
  ledger L-0463) scope its count on ``source = 'careers_crawler'`` instead
  of counting every ``ats_scanner`` row too -- that predicate has read this
  column since it landed; this migration is what makes the column exist.
- ``company_scan_log.failure_reason`` text, nullable, no default. Absorbs
  private migration ``m209009471_add_failure_reason_to_company_scan_log.py``
  (ledger L-0276, HOLD until this port lands). Lets the same predicate's
  reason-aware strike semantics (W4) distinguish a broken scan from a clean
  "no title matched" zero-hit.
- ``companies.ats_link_discovery_last_at`` timestamptz, nullable, no
  default. Absorbs private migration
  ``m209775515_add_ats_link_discovery_last_at_to_companies.py`` (ledger
  L-0293, HOLD until this port lands). Backs the opportunistic ATS-link
  discovery cooldown stamp the crawler writes each attempt.

  Private's column is ``TEXT DEFAULT NULL`` (SQLite has no ``timestamptz``);
  this follows m0003's ``careers_crawl_last_at timestamptz`` precedent for
  the hosted schema's dominant timestamp-column convention instead of
  copying the SQLite type verbatim.

Does NOT touch ``companies.ats_scan_enabled`` / ``companies.
careers_scan_enabled`` -- those already landed via
``m0021_wi13_scan_lane_columns.py`` (ledger L-0286, HOLD, absorbed there;
see that migration's docstring, which explicitly forbids re-adding them
here).

Does NOT add ``companies.careers_crawl_tier`` -- private's ``_persistence.py``
writes it, but no migration in this tree (m0001-m0022) ever added the
column; that is a pre-existing baseline-port gap unrelated to the three
HOLD rows this migration resolves, tracked at
https://github.com/Senkichi/jobcannon/issues/347 and documented as a
PORT-SEAM in ``_persistence.py`` rather than silently widened into this
migration's declared 3-column scope.

Column shape follows m0016/m0021's "deliberately narrower than private"
precedent: plain nullable columns, no CHECK, no default beyond NULL --
enforcement belongs at the writer, not the schema, and a NOT NULL/CHECK
column would fail on any pre-existing row.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=23,
    description="company_scan_log.source/failure_reason + companies.ats_link_discovery_last_at (L-0465)",
    sql=[
        "ALTER TABLE company_scan_log ADD COLUMN IF NOT EXISTS source text",
        "ALTER TABLE company_scan_log ADD COLUMN IF NOT EXISTS failure_reason text",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS ats_link_discovery_last_at timestamptz",
    ],
)
