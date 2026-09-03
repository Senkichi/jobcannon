"""PORTED from job_finder/db/_conversion_metrics.py @ 27663d9ae9cb553fded7c5832831a5d3935a1836
(private job-cannon). Ledger L-0066. (# PORT-SEAM: module docstring rewritten
for this port; see the PORT-SEAM paragraphs below for the specific deltas.)

Read-only conversion-signal analytics: per-user, per fit-band application
rate. Zero model calls, zero writes -- reads ``postings.classification``
(m0015, L-0064) and ``pipeline_status`` (m0001), per this ledger row's
adjudicated seam: "New per-tenant read module in jobcannon/db/ alongside
_stats.py/_feed.py; needs a per-user (not global) CLASSIFICATIONS x
PIPELINE_STATUSES cross-tab against pipeline_status, not the private
single-user jobs table."
# PORT-SEAM: read-only module -- _user_actions.py's single-writer invariant
# for watchlists/pipeline_status is not implicated the way it was for
# L-0073's dropped functions (which needed to WRITE those tables). _feed.py's
# own docstring establishes the identical precedent -- "Both tables are
# written exclusively by jobcannon/db/_user_actions.py; this module only
# reads them" -- and this module follows the same rule.

Private computed BOTH application_rate (applied / scored) AND callback_rate
(converted / applied), where "applied"/"converted" were the FURTHEST stage a
job ever reached in a ``pipeline_events`` history table (POSITIVE_STAGES =
applied/phone_screen/technical/onsite/offer/accepted, a 6-stage progression
asserted at import time as a subset of private's PIPELINE_STATUSES).
# PORT-SEAM: this host has no pipeline_events-equivalent history table and no
# phone_screen/technical/onsite/offer/accepted vocabulary -- _user_actions.py's
# own _PIPELINE_STATUSES is `frozenset({"dismissed", "applied"})`, a 2-value
# vocabulary with no stage beyond "applied". Only `application_rate` is
# portable; `converted`/`callback_rate` are dropped entirely here -- they are
# uncomputable on this host, not merely difficult, since there is no further
# stage to measure a callback against. Because "applied" is this host's
# terminal positive status, CURRENT `pipeline_status.status = 'applied'` is
# exactly equivalent to private's "max-stage-ever >= applied" for this
# restricted vocabulary -- no event history is lost by reading current status
# instead of a max-stage-ever query over a table this host does not have.

Mirrors ``_stats.py``'s read-only module shape (module docstring + one
function, string-key row access, `raw = conn.raw if hasattr(conn, "raw")
else conn`) rather than private's save/restore-row_factory sqlite3 pattern.
# PORT-SEAM: private saved/restored `conn.row_factory` around the query body
# (a sqlite3-dialect side-effect guard) -- dropped, no row_factory concept on
# this host's psycopg-backed connections.
"""

from __future__ import annotations

from typing import (
    Any,
)  # PORT-SEAM: replaces private's import sqlite3 (no sqlite3 dialect on this host)

from jobcannon.engine.constants import (
    CLASSIFICATIONS,
)  # PORT-SEAM: replaces private's job_finder.constants import (CLASSIFICATIONS/PIPELINE_STATUSES both live in jobcannon.engine.constants on this host)


def compute_conversion_by_band(
    conn: Any, user_id: str
) -> dict[str, dict]:  # PORT-SEAM: user_id param added, see Args below
    """Per fit-band application rate for one user, read-only.

    For each band in CLASSIFICATIONS, over postings (# PORT-SEAM: postings
    replaces private's jobs table) that are SCORED
    (scoring_model IS NOT NULL AND classification IS NOT NULL):

      scored            -- count of scored postings in the band
      applied           -- count with a pipeline_status row for THIS user
                           where status = 'applied'
      application_rate  -- applied / scored (None if scored == 0)
      (# PORT-SEAM: private's bullet list also documented `converted` and
      # `callback_rate`, both dropped from this port -- see module docstring.)

    # PORT-SEAM: private also returned `converted` / `callback_rate`,
    # computed from a pipeline_events.to_status max-stage-ever query --
    # dropped here, see module docstring (no pipeline_events-equivalent
    # table, no stage beyond 'applied' in this host's vocabulary).
    # PORT-SEAM: private took only `conn` (single-user schema, no user
    # scoping); this host's `pipeline_status` is per-user, so this port
    # takes `user_id` and scopes the applied-count query to it, matching
    # this ledger row's adjudicated seam ("per-user, not the private
    # single-user jobs table").

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_stats.py`` / ``_user_actions.py``'s
            dispatch. (# PORT-SEAM: private said "Open sqlite3 connection".)
        user_id: scope the applied-count half of the cross-tab to this
            user's own ``pipeline_status`` rows.

    Returns a dict keyed by band (every band in CLASSIFICATIONS present, even
    at zero) -> the per-band dict above. Pure read; commits nothing.
    """
    raw = (
        conn.raw if hasattr(conn, "raw") else conn
    )  # PORT-SEAM: replaces private's row_factory save/restore try/finally, see module docstring

    result: dict[str, dict] = {
        band: {"scored": 0, "applied": 0, "application_rate": None} for band in CLASSIFICATIONS
    }  # PORT-SEAM: seeds every band even at zero, matching private's seed loop; drops the `converted`/`callback_rate` keys (see module docstring)

    scored_rows = raw.execute(
        "SELECT classification, COUNT(*) AS cnt FROM postings "  # PORT-SEAM: postings replaces private's jobs table
        "WHERE scoring_model IS NOT NULL AND classification IS NOT NULL "
        "GROUP BY classification"
    ).fetchall()
    for row in scored_rows:
        band = row["classification"]
        if band in result:
            result[band]["scored"] = row["cnt"]

    # PORT-SEAM: replaces private's two-step max_stage_query (pipeline_events
    # CASE-ranked subquery) + job_classification dict-join done in Python --
    # this host has no pipeline_events table, so the per-band applied count
    # is a direct SQL join against pipeline_status, scoped to user_id.
    applied_rows = raw.execute(
        "SELECT p.classification, COUNT(*) AS cnt "
        "FROM pipeline_status ps "
        "JOIN postings p ON p.id = ps.posting_id "
        "WHERE ps.user_id = %s AND ps.status = 'applied' "
        "AND p.scoring_model IS NOT NULL AND p.classification IS NOT NULL "
        "GROUP BY p.classification",
        (user_id,),
    ).fetchall()
    for row in applied_rows:
        band = row["classification"]
        if band in result:
            result[band]["applied"] = row["cnt"]

    for band in result:
        scored = result[band]["scored"]
        applied = result[band]["applied"]
        result[band]["application_rate"] = (
            (applied / scored) if scored > 0 else None
        )  # PORT-SEAM: callback_rate computation dropped here, see module docstring

    return result  # PORT-SEAM: private's pipeline_events max-stage-ever loop + callback_rate computation dropped here, see module docstring
