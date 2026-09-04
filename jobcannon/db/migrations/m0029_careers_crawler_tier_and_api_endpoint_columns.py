"""Migration 29 -- companies.careers_api_endpoint + careers_crawl_tier +
company_scan_log.jobs_matched (#385, #347, #386).

Backs three ``jobcannon/engine/careers_crawler`` read/write paths that
predate this migration but have never had schema behind them on the hosted
database:

- ``companies.careers_api_endpoint`` text, nullable, no default. Written by
  ``_api_cache.py``'s ``_cache_api_endpoint`` / ``_clear_api_cache``, read by
  ``_escalation.py`` (``company["careers_api_endpoint"]``) and by both of
  ``crawl_careers_batch``'s lane SELECTs (``jobcannon/engine/careers_crawler/
  __init__.py``). Absorbs private migration
  ``job_finder/web/migrations/m035_careers_api_endpoint.py`` (private
  ``ALTER TABLE companies ADD COLUMN careers_api_endpoint TEXT DEFAULT
  NULL``). #385.
- ``companies.careers_crawl_tier`` text, nullable, no default. Written by
  ``_persistence.py``'s companies UPDATE (re-wired by this migration's
  sibling PR -- see that file's now-resolved ``# PORT-SEAM: careers_crawl_tier
  dropped, see below (#347)`` note) and by ``_tier_cache.py``'s cached-tier
  replay, read by ``_escalation.py`` (``company["careers_crawl_tier"]``) and
  both lane SELECTs. Absorbs private migration
  ``job_finder/web/migrations/m036_careers_crawl_tier.py`` (private
  ``ALTER TABLE companies ADD COLUMN careers_crawl_tier TEXT DEFAULT
  NULL``). #347.
- ``company_scan_log.jobs_matched`` integer, nullable, no default. Read
  directly by ``_bench_predicate.py``'s 5-strike penalty-box SQL (both
  ``build_bench_predicate_sql`` and ``is_company_benched``) to decide
  whether a ``company_scan_log`` row is a "hit" (``jobs_matched > 0``).
  Written by ``_scan_log.py``'s ``record_scan_outcome`` -- already passing
  this kwarg today, but silently dropped by that writer's present-column
  introspection (see its module docstring) since the column has never
  existed on the hosted schema. ``_persistence.py`` passes
  ``jobs_matched=company_jobs_found``, so this migration is a behavior
  change, not just a schema catch-up: penalty-box hit-detection goes from
  "never recorded" (every crawler-origin row on the host has read as a
  strike candidate) to live the moment this lands. Absorbs private
  migration ``job_finder/web/migrations/m023_recalibrate_jobs_found_total.py``
  (private ``ALTER TABLE company_scan_log ADD COLUMN jobs_matched INTEGER
  DEFAULT NULL``). #386.

All three private columns are ``DEFAULT NULL`` (SQLite has no typed
nullable-with-no-default distinct from bare nullable); this follows
m0016/m0021/m0023's "deliberately narrower than private" precedent for the
hosted schema: plain nullable columns, no CHECK, no default beyond NULL --
enforcement belongs at the writer, not the schema.

m0023's docstring explicitly named ``careers_crawl_tier`` as intentionally
NOT added ("that is a pre-existing baseline-port gap ... tracked at
https://github.com/Senkichi/jobcannon/issues/347"); this migration is that
tracked follow-up, landed alongside #385 because both columns are read by
the exact same two lane SELECT lists and their sibling PR would otherwise
need two round-trips through ``crawl_careers_batch`` (#385's own "Suggested
fix"). ``jobs_matched`` joined this same migration once #386's work
(parameterizing the bench predicate so it can run un-stubbed against
Postgres, see ``jobcannon/db/compat.py``) surfaced that the predicate's own
SQL reads a column no migration had ever added -- adding it here rather than
as a separate migration keeps one ordering story for one PR's schema needs
instead of two.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=29,
    description=(
        "companies.careers_api_endpoint + careers_crawl_tier + "
        "company_scan_log.jobs_matched (#385, #347, #386)"
    ),
    sql=[
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS careers_api_endpoint text",
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS careers_crawl_tier text",
        "ALTER TABLE company_scan_log ADD COLUMN IF NOT EXISTS jobs_matched integer",
    ],
)
