"""Migration 12 — profiles.target_companies + profiles.workplace_type
(#169/#170: the picker's company selection and workplace-type preference
have no durable home for a signed-up user).

Before this migration, only `target_titles` (m0001) survives the anon-to-
authed handoff (`jobcannon/web/handoff.py`) into a durable `profiles` row —
`companies`/`workplace_type` exist only in the session-scoped
`pending_picker` dict, which /preview reads for the pre-signup feed but
which is gone the moment a stranger signs up. #170 ("the authed feed's
'matches your selections' copy promises filtering that never applies")
traces back to this gap: there was no column for the authed feed to filter
by even after the read side is fixed.

`target_companies` mirrors `target_titles`'s shape exactly: nullable jsonb,
COALESCE-preserve-when-omitted in `upsert_profile`
(jobcannon/db/_profiles.py) — see that module's docstring for the one
caller-side change (#169) that makes an empty selection actually clear a
prior one instead of reviving it.

`workplace_type` is a bare nullable `text` column, deliberately WITHOUT a
CHECK constraint enumerating the valid tokens — mirroring
`postings.workplace_type` (m0001), which has none either, despite both
columns holding the same closed set of uppercase tokens
(jobcannon/engine/location_canonical.py's WorkplaceType Literal). Validation
lives once, in code, at the write boundary
(jobcannon/web/onboarding.py's WORKPLACE_TYPES / _WORKPLACE_FILTERS) — the
same trust boundary postings.workplace_type already relies on for every
scanner-written row. `upsert_profile` writes this column with a plain
overwrite (not COALESCE) — see that module's docstring for why: it is the
one profiles column whose single legitimate "no preference" state (NULL)
would otherwise be indistinguishable from "field omitted this call."
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=12,
    description="profiles.target_companies (jsonb) + profiles.workplace_type (text), both nullable",
    sql=[
        "ALTER TABLE profiles ADD COLUMN target_companies jsonb",
        "ALTER TABLE profiles ADD COLUMN workplace_type text",
    ],
)
