"""Migration 15 -- postings scoring tuple (ledger L-0064).

Ports the 5-column "scoring tuple" ``jobcannon.engine.job_scorer`` and
``jobcannon.engine.classification.derive_classification`` already read/derive
but have no ``postings`` columns to persist into: ``classification``,
``sub_scores_json``, ``fit_analysis``, ``scoring_provider``, ``scoring_model``.
Private source: ``job_finder/db/_assessment_writer.py`` (``persist_job_assessment``
/ ``invalidate_job_score``) @ b1f69f3e10a452cc498527f830959b852108f5e9.

Deliberately narrower than the private schema -- this migration adds ONLY the
literal 5-tuple the ledger names, not the private table's full column set:

- No ``classification_rank`` / ``sub_score_sum``: private-side materialization
  for a sort path with no host consumer (host has no equivalent ranked list
  view yet). Add later, additively, if/when one exists.
- No ``classification_rule_version`` / ``jd_adjudicated_version``: the second
  is already m0009's column, owned and written by ``_jd_full.py::set_jd_full``
  (verified via grep -- the only writer of ``jd_content_verdict`` /
  ``jd_content_signal`` / ``jd_adjudicated_version`` in this tree). This
  migration does not touch it or add a rule-version column of its own.
- No ``location_policy_*`` columns (6 in private): no ``LocationPolicy`` class
  exists anywhere in ``jobcannon`` (confirmed via grep). The writer this
  migration backs takes the already-established
  ``location_policy_verdict_json: str | None`` seam instead (see
  ``jobcannon.engine.classification.effective_sub_scores``, whose own
  docstring says it was ported ahead of this writer to take exactly that
  shape) -- a pre-serialized JSON string, not a first-class column set.
- No ``legitimacy_note`` / ``enrichment_tier`` columns: these are
  ``derive_classification`` INPUTS in the private schema, not part of the
  ledger's own "scoring tuple" output definition, and no host writer/reader
  of either concept exists yet. The ported writer selects for their presence
  at call time (mirroring ``_scan_log.py``'s ``_scan_log_columns`` /
  present-column-intersection pattern) and passes ``None`` when absent, so
  ``derive_classification``'s legitimacy-reject and enrichment-exhausted
  branches stay reachable the moment a future migration adds those columns,
  with no writer-side edit required.

``sub_scores_json`` / ``fit_analysis`` are ``jsonb`` (dominant convention in
this schema per m0001 -- ``locations_raw``, ``sightings``,
``salary_observations``, ``unresolved_reasons``, etc. -- not the one legacy
``comp_data_json text`` exception). ``classification`` / ``scoring_provider``
/ ``scoring_model`` are plain ``text``, matching ``jd_content_verdict``'s
precedent (m0009) for a small closed-ish string value.

I-05 backstop (this row's adjudicated seam explicitly requires a Postgres
CHECK/trigger equivalent of the private m078 SQLite trigger): a
table-level, separately-named CHECK constraint (matching
m0008/m0003's ADD CONSTRAINT precedent, not an inline column CHECK) enforces
"classification is set whenever scoring_model is set" -- i.e. a scored row
always carries a classification, never a scoring-model stamp with no verdict
behind it. All-NULL pre-existing rows satisfy it trivially (scoring_model IS
NULL). The ported ``invalidate_job_score`` nulls both columns in the SAME
UPDATE statement as every other tuple member, so the CHECK never fires
against its own unwriter (private's docstring flags this exact ordering
hazard; enforced here structurally by construction, not by statement
ordering, since both columns null in one statement can never transiently
violate the constraint).
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=15,
    description="postings scoring tuple: classification/sub_scores_json/fit_analysis/scoring_provider/scoring_model",
    sql=[
        "ALTER TABLE postings ADD COLUMN classification text",
        "ALTER TABLE postings ADD COLUMN sub_scores_json jsonb",
        "ALTER TABLE postings ADD COLUMN fit_analysis jsonb",
        "ALTER TABLE postings ADD COLUMN scoring_provider text",
        "ALTER TABLE postings ADD COLUMN scoring_model text",
        "ALTER TABLE postings ADD CONSTRAINT postings_scoring_model_requires_classification "
        "CHECK (scoring_model IS NULL OR classification IS NOT NULL)",
    ],
)
