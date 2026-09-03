"""Migration 17 -- postings direct_url writer columns (L-0068).

Backs jobcannon/db/_direct_link.py, ported from private's
job_finder/db/_direct_link.py @ c81bb00205e1a87d14c37f855a90e2a8027cabac
(job-cannon). m0001 already has a bare ``postings.direct_url text`` column,
but confirmed via ``jobcannon/web/apply_url.py``'s own module docstring
("postings.direct_url is permanently NULL... no UPDATE anywhere refills
it"), no writer for it exists on this host at all -- neither private's
no-downgrade precedence write (strict/loose) nor its resolver-attempt
bookkeeping. This migration adds the three columns private's writer pair
needs; the sibling PR's ``_direct_link.py`` is the write side.

- ``direct_url_confidence`` -- CHECK-constrained to ('strict','loose'),
  matching private's ``_VALID_CONFIDENCE`` tuple (single-sourced by the
  writer's own validation, not duplicated as a bare CHECK list -- see
  ``_direct_link.py``'s module docstring for why the CHECK still exists).
- ``direct_url_checked_at`` -- timestamptz, same shape as private's
  ``jobs.direct_url_checked_at`` (m092 there).
- ``direct_url_attempts`` -- integer NOT NULL DEFAULT 0, same shape as
  private's ``jobs.direct_url_attempts``.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=17,
    description="postings direct_url writer columns (direct_url_confidence/checked_at/attempts)",
    sql=[
        "ALTER TABLE postings ADD COLUMN IF NOT EXISTS direct_url_confidence text "
        "CHECK (direct_url_confidence IN ('strict', 'loose'))",
        "ALTER TABLE postings ADD COLUMN IF NOT EXISTS direct_url_checked_at timestamptz",
        "ALTER TABLE postings ADD COLUMN IF NOT EXISTS direct_url_attempts integer NOT NULL DEFAULT 0",
    ],
)
