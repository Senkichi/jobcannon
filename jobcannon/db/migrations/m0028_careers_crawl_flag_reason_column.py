"""Migration 28 -- companies.careers_crawl_flag_reason column (issue #370).

Backs the cohort-legitimacy gate's flag/exclude cycle
(``jobcannon/engine/careers_crawler/_cohort_legitimacy.py``): a positive
trip of the aggregator-suspected gate calls ``record_legitimacy_flag``
(landed in #359, ledger L-0464), which writes

    UPDATE companies SET careers_crawl_flag_reason = ? WHERE id = ?

Two batch-selection guards in ``crawl_careers_batch``
(``jobcannon/engine/careers_crawler/__init__.py`` -- both the re-discovery
and origination lanes) read the same column back:
``c.careers_crawl_flag_reason IS NULL``, excluding a flagged company from
either lane until a human clears the flag.

No migration in this tree (m0001-m0027) ever added this column -- #359
landed the writer/reader pair against a schema that never got the column
that backs them, so every call to ``record_legitimacy_flag`` has been
raising ``UndefinedColumn`` since #359 merged (issue #370). This migration
is the fix -- adding only the column, not touching either reader/writer.

Column shape follows m0016/m0021/m0023's "deliberately narrower than
private" precedent: plain nullable text, no CHECK, no default beyond NULL
-- enforcement belongs at the writer, not the schema. Private's
``job_finder/web/migrations/m205996202_careers_crawl_flag_reason_column.py``
is ``TEXT DEFAULT NULL``; SQLite's explicit ``DEFAULT NULL`` is redundant
on Postgres (NULL is already the implicit default for a nullable column
with none declared), so this migration's ``ADD COLUMN`` carries no
``DEFAULT`` clause.

No backfill: NULL means "never flagged," matching private's own migration
docstring ("existing rows are untouched, nothing is backfilled
retroactively") -- every pre-existing company row lands NULL, unflagged.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=28,
    description="companies.careers_crawl_flag_reason column (#370)",
    sql=[
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS careers_crawl_flag_reason text",
    ],
)
