"""Migration 9 — postings.jd_content_verdict / jd_content_signal /
jd_adjudicated_version: the columns that arm the D5 scoring gate (#152).

``jobcannon.engine.job_scorer.scoring_precheck`` already reads
``job["jd_content_verdict"]`` / ``job["jd_adjudicated_version"]`` (F7 port of
private d1c578b3, #1794) and defers a job whose persisted jd-content-contract
verdict is not CLEAN and not yet adjudicated. Until this migration, the
hosted ``postings`` table had none of the three columns, so every row read
back ``NULL`` for ``jd_content_verdict`` and the gate stayed permanently
inert (correct-but-inert per that function's own docstring — fails OPEN on
a NULL verdict, never a false-positive block).

``jobcannon/db/_jd_full.py::set_jd_full`` (this same PR) is the writer that
stamps ``jd_content_verdict`` / ``jd_content_signal`` at the single storage
chokepoint via ``jobcannon.engine.jd_content_contract.classify_jd_content``,
and nulls ``jd_adjudicated_version`` whenever the stored jd_full content
changes so a stale adjudication can never vouch for a body it never saw.

No DEFAULT and no NOT NULL on any of the three: every pre-existing row (and
every row nothing has stamped yet, e.g. one whose jd_full predates this
migration and is not re-touched) lands/stays NULL, which is exactly the
"no host has stamped this row" state ``scoring_precheck`` already treats as
fail-open. ``jd_content_verdict`` / ``jd_content_signal`` are ``text``
(``JdVerdict`` values are plain string enums, e.g. ``"clean"``); adjudicated
version is ``integer`` — compared against
``jobcannon.engine.jd_content_contract.JD_CONTENT_VERSION`` by
``scoring_precheck``.

Row-projection note: this hosted schema has no ``JOBS_ALL_COLUMNS``-style
explicit column-list projection to update (the sole read pattern used by
``_jd_full.py`` / ``_jobs.py`` for a full posting row is
``SELECT * FROM postings WHERE ...``, which picks up new columns for free).
A future host wiring ``score_and_persist_job`` (Wave 2 — no in-tree caller
exists yet, per ``job_scorer.py``'s own module docstring) gets these three
keys automatically from that same ``SELECT *`` pattern; no additional
projection change is needed once this migration lands. That same wiring PR
must also ship a writer that sets ``jd_adjudicated_version`` non-NULL (or an
equivalent resolution) per #183 — CI-enforced by
tests/test_scoring_precheck_wiring_guard.py.
"""

from __future__ import annotations

from jobcannon.db.migrations.types import Migration

MIGRATION = Migration(
    version=9,
    description="postings.jd_content_verdict/jd_content_signal/jd_adjudicated_version (D5 gate)",
    sql=[
        "ALTER TABLE postings ADD COLUMN jd_content_verdict text",
        "ALTER TABLE postings ADD COLUMN jd_content_signal text",
        "ALTER TABLE postings ADD COLUMN jd_adjudicated_version integer",
    ],
)
