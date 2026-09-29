"""Migration 30 -- profiles.work_arrangement + industries + exclusions +
positions + education (issue #420: grow ``profiles`` to cover the
config-shaped candidate-context fields so the two candidate-context
renderers can eventually be unified, #364).

Context. The config-shaped renderer
(``jobcannon/host/scoring_orchestrator.py::build_candidate_context`` plus
its ``_render_location_targeting`` helper) consumes profile fields the
``profiles`` table never had: ``profile.work_arrangement`` (the location
preference hierarchy the flat ``target_locations`` list cannot express),
``profile.industries``, ``profile.exclusions``, and the experience
profile's structured ``positions``/``education`` lists (``profiles`` only
had free-text ``experience_summary``). Issue #420's operator decision
(2026-09-25) picked Option B -- typed nullable columns per field, the
repo norm every existing profiles migration follows (m0008's scalar,
m0012's pair) -- over a single jsonb blob mirroring the config profile.

Columns (all nullable, no DEFAULT, no CHECK -- the m0008/m0012/m0029
norm; enforcement belongs at the write boundary, and NULL must mean
"not specified" so a never-populated row defers rather than fabricating
an anchor, the same reasoning as m0008's comp_floor_usd):

- ``work_arrangement`` text -- the candidate's PREFERRED arrangement
  token the scoring path branches on (``_render_location_targeting``'s
  remote-first vs. geography-first hierarchy; ``engine/location_fit``'s
  Rows R-a/R-b). It carries the config shape's lowercase tokens
  (``"remote"``/``"hybrid"``/``"onsite"``) and is deliberately a
  SEPARATE column from m0012's ``workplace_type``: that column holds the
  picker's UPPERCASE WorkplaceType feed-filter token, validated at the
  ``jobcannon/web/onboarding.py`` write boundary -- a different
  vocabulary for a different consumer (feed filtering, not scoring
  preference). Issue #420's open question 2 lands here as coexistence,
  not replacement: neither column can express the other's semantics,
  and a DROP COLUMN against the outgoing release would be
  contract-shaped anyway. No CHECK enumerating the token set -- the
  same no-enum-at-the-schema-layer precedent m0012 set for
  ``workplace_type``.
- ``industries`` jsonb -- list of industry strings, mirroring
  ``config["profile"]["industries"]``.
- ``exclusions`` jsonb -- an OBJECT mirroring
  ``config["profile"]["exclusions"]`` verbatim (``{"companies": [...],
  "title_keywords": [...]}``; ``engine/exclusion_filter.py`` reads both
  keys), not just the companies list the renderer renders today --
  keeping the config's dict shape is what lets a unified renderer
  substitute this column for ``cfg_profile["exclusions"]`` without
  re-shaping.
- ``positions`` jsonb -- list of ``{title, company, start_date,
  end_date}`` objects mirroring the experience profile's ``positions``;
  the renderer caps display at 6 at render time, so the column stores
  the full list unbounded.
- ``education`` jsonb -- list of ``{degree, institution, graduation}``
  objects mirroring the experience profile's ``education``; renderer
  caps at 3 the same way.

Deliberately NOT in this migration: any writer or backfill (issue
#420's open question 3 -- picker UI vs. config import -- stays open;
the columns land NULL on existing rows with nothing to backfill), and
no renderer change (retiring one of the two renderers is #364's
follow-up; ``host/candidate_context.py``'s row renderer does not read
these columns yet). The one read-side change that DOES land with this
schema: ``get_profile``'s SELECT (and ``clear_profile_targets``'s
RETURNING, which mirrors it) widen to include all five columns so they
are classified INTO the account export per the #105 contract
(tests/host/test_account_export.py's
``test_profiles_table_columns_are_all_classified_for_export``) --
self-reported career/preference data in the same minimization class as
``skills``/``target_titles``, which is also the classification the
eventual writer would otherwise have to remember to revisit.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=30,
    description=(
        "profiles.work_arrangement (text) + industries/exclusions/positions/education "
        "(jsonb), all nullable -- the config-shaped candidate-context fields (#420)"
    ),
    sql=[
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS work_arrangement text",
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS industries jsonb",
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS exclusions jsonb",
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS positions jsonb",
        "ALTER TABLE profiles ADD COLUMN IF NOT EXISTS education jsonb",
    ],
)
