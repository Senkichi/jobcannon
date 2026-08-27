"""Migration 8 — profiles.comp_floor_usd (issue #28 item 2: comp_fit has no
backing profiles column).

The v3.0 scoring rubric scores a `comp_fit` axis against a candidate
compensation floor, but `profiles` (m0001) never gained a column to hold
one — the host candidate-context resolver could only ever render 6 of the
rubric's implied 7 inputs. Owner-approved design (issue #28, "Option A"):
a bare nullable annual-USD integer, matching the proven private-product
shape (`config.profile.min_salary`) with two deliberate hosted-product
translations: (1) scoring-context-only, no 85%-auto-exclude filter behavior
carried over; (2) a per-tenant profile column instead of a single global
config value. No currency/period columns (YAGNI — additive later via the
`postings.salary_*` CHECK-enum precedent m0001 already set, if ever
needed).

No DEFAULT and nullable: an unset floor must NOT silently anchor comp_fit
against 0 (which would tank every job's comp_fit score for a tenant who
simply hasn't told us their floor yet). NULL renders as "Not specified" in
host/candidate_context.build_candidate_context, deferring comp_fit rather
than anchoring it against a fabricated number — see the issue's "why not
Option C" discussion for why an unanchored axis is preferable to a false
anchor.

The CHECK is a separate named ADD CONSTRAINT statement (not an inline
column CHECK) to match this repo's existing precedent
(m0003_companies_scan_columns.py's ats_probe_status widen) for a
constraint a later migration might need to DROP/replace by name.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=8,
    description="profiles.comp_floor_usd (nullable annual-USD compensation floor for comp_fit scoring)",
    sql=[
        "ALTER TABLE profiles ADD COLUMN comp_floor_usd integer",
        "ALTER TABLE profiles ADD CONSTRAINT profiles_comp_floor_usd_nonneg "
        "CHECK (comp_floor_usd IS NULL OR comp_floor_usd >= 0)",
    ],
)
