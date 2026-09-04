"""Migration 20 -- postings aggregator apply url column (L-0075).

Backs jobcannon/db/_jobs.py::annotate_posting_apply_url, ported (flat
re-adaptation) from private's job_finder/db/_postings.py::annotate_posting_apply_url
@ 175d0e1024eee45a279522868798fb7b4777a952 (job-cannon).

Private's writer attached ``aggregator_apply_url`` to one descriptor inside
the ``jobs.postings`` JSON array (keyed by ``(ats_platform, source_id)``).
This host's ``postings`` table is flat -- one row per posting, no per-source
descriptor sub-entity (m0001) -- so the same fact is a plain scalar column
on that one row instead. Distinct from ``direct_url`` (m0017, this host's
own precedence-ranked company-site writer, owned by
``jobcannon/db/_direct_link.py``): ``aggregator_apply_url`` is the
aggregator-sourced apply link, a separate provenance the design note (Q-C)
deliberately does not overload onto ``direct_url``/``direct_url_confidence``.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=20,
    description="postings aggregator apply url column (L-0075)",
    sql=[
        "ALTER TABLE postings ADD COLUMN IF NOT EXISTS aggregator_apply_url text",
    ],
)
