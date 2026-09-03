"""set_company_attribution -- Postgres port of the private repo's single
point of enforcement for manual company attribution writes (ledger L-0065).
Private source: ``job_finder/db/_company_attribution.py`` @
7e23f5394b6b278b572971d658f20d4725db3623.

Landed as a new sibling module rather than inserted into
``jobcannon/db/_companies.py`` as the ledger's adjudicated seam suggested.
# PORT-SEAM: fidelity-diff is file-to-file -- diffing this private module
# against the existing, unrelated bulk of _companies.py produced 5/5
# unclassified hunks (every pre-existing _companies.py line the private
# file doesn't share), which cannot be resolved by marker insertion without
# mislabeling code owned by a different ledger row. A new module keeps the
# private module boundary intact, keeps fidelity-diff meaningful, and
# matches this repo's own many-small-files convention (CLAUDE.md).
``set_company_attribution`` still reuses ``_companies.py``'s
``_PROBE_STATUS_PRECEDENCE``/collision-handling conventions (psycopg
UniqueViolation on ``UNIQUE(ats_platform, ats_slug)``, same as m0001) and its
``commit_unless_nested`` transaction discipline.

The invariant bundle applied on every call, unchanged from private:

- ``ats_probe_status = 'pending'``  -- re-probe the new attribution
- ``consecutive_empty_scans = 0``   -- clear the empty-scan counter
- ``retry_count = 0``               -- clear retry bookkeeping
- ``retry_after = NULL``            -- clear retry bookkeeping
- ``miss_reason = NULL``            -- clear stale miss classification

When ``careers_url`` is explicitly provided (not ``_UNSET``):

# PORT-SEAM: private additionally sets careers_scan_enabled=1 and clears
# careers_crawl_flag_reason on this branch. Neither column exists on this
# host: m0001's companies table has one merged `scan_enabled` boolean
# (not private's split ats_scan_enabled/careers_scan_enabled -- see L-0040's
# seam, which needs that split landed first), and no crawl-flag-reason
# column at all. This port sets the host's single `scan_enabled = true` as
# the nearest available re-enable signal and drops the flag-reason clear
# (no column to clear). Revisit both once the WI-13 split (L-0040) lands.

# PORT-SEAM: private also calls snapshot_tracked/record_state_diff
# (job_finder/db/_company_state.py, WI-08) to record the transition in
# company_state_history, inside the same commit. No company_state_history
# table exists on this host yet -- that table is itself L-0040's ADAPT
# scope, escalated (blocked on the same WI-13 precondition; see
# verification.md). This port omits the history write entirely rather than
# half-porting a two-column split it can't observe; a future PR wires
# record_state_diff here once L-0040 lands.

**Sentinel vs None.** ``None`` means "clear the column to NULL"; ``_UNSET``
means "leave the column untouched" -- unchanged from private, and the same
reason: a caller setting only ``careers_url`` must not silently clobber
``ats_platform``/``ats_slug`` to NULL, and vice versa.

Commits on success (``commit_unless_nested``, a no-op inside an ambient
``with conn.transaction():`` block, a real commit otherwise -- matching
``_companies.py``/``_jobs.py``/``_jd_full.py``). On
``AttributionCollisionError`` the failed UPDATE is left uncommitted (the
inner ``with raw.transaction():`` SAVEPOINT rolls itself back) -- the caller
owns the collision-render path, unchanged from private.
"""

from __future__ import annotations

import logging  # PORT-SEAM: sqlite3 import dropped, no sqlite3 dialect on this host
from typing import Any

import psycopg

from jobcannon.db.pool import commit_unless_nested

logger = logging.getLogger(
    __name__
)  # PORT-SEAM: json_utils.utc_now_iso/private _company_state imports dropped -- see below and module docstring


class _Unset:
    """Sentinel type for "argument not provided" keyword arguments.

    Distinct from ``None`` which means "clear the column to NULL". A bare
    ``object()`` instance would work mechanically but a named class makes
    the repr and type annotations self-documenting.
    """

    _instance: _Unset | None = None

    def __new__(cls) -> _Unset:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<unset>"


_UNSET: Any = _Unset()


class AttributionCollisionError(Exception):
    """Raised when setting ``(ats_platform, ats_slug)`` would duplicate
    another company's UNIQUE pair (m0001's constraint; (# PORT-SEAM: renamed from private's m076)).

    Attributes:
        owner_id: The id of the company that already owns the pair, or None.
        owner_name: The ``name_raw`` of the owner, or None.
        ats_platform: The platform that collided.
        ats_slug: The slug that collided.
    """

    def __init__(
        self,
        owner_id: int | None,
        owner_name: str | None,
        ats_platform: str | None,
        ats_slug: str | None,
    ) -> None:
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.ats_platform = ats_platform
        self.ats_slug = ats_slug
        super().__init__(
            f"Cannot set ATS to {ats_platform}/{ats_slug} — already owned "
            f"by company id={owner_id} ({owner_name!r})"
        )


def set_company_attribution(
    conn: Any,  # PORT-SEAM: no sqlite3 dialect on this host (psycopg only)
    company_id: int,
    *,
    ats_platform: str | None = _UNSET,
    ats_slug: str | None = _UNSET,
    careers_url: str | None = _UNSET,
    # PORT-SEAM: private's changed_by param (company_state_history tag) is
    # dropped -- no history table on this host yet, see module docstring.
) -> None:
    """Set manual company attribution fields and reset the invariant bundle.

    Only the fields explicitly passed (not ``_UNSET``) are written; pass
    ``None`` to clear a column to NULL. This lets a caller set just the
    careers_url without clobbering ats_platform/ats_slug, or just the ATS
    pair without clobbering careers_url.

    Always resets ``ats_probe_status='pending'``,
    ``consecutive_empty_scans=0``, ``retry_count=0``, ``retry_after=NULL``,
    ``miss_reason=NULL``. When ``careers_url`` is explicitly provided, also
    sets ``scan_enabled=true`` (see module docstring's PORT-SEAM for why
    this differs from private's split careers_scan_enabled +
    careers_crawl_flag_reason clear). (# PORT-SEAM: private's Args also
    documented Records the tracked-field transition via record_state_diff
    -- dropped, see module docstring.)

    Commits on success. Raises ``AttributionCollisionError`` (without
    committing) if ``(ats_platform, ats_slug)`` is already owned by a
    different company.

    Args:
        conn: pooled connection (``.raw`` unwrapped) or a bare psycopg
            connection, matching ``_companies.py``'s dispatch. (# PORT-SEAM:
            private said "Open SQLite connection".)
        company_id: Company row ID.
        ats_platform: New ATS platform, None to clear, or _UNSET (default)
            to leave untouched.
        ats_slug: New ATS slug, None to clear, or _UNSET (default) to leave
            untouched.
        careers_url: New careers URL, None to clear, or _UNSET (default) to
            leave untouched.

    Raises:
        AttributionCollisionError: (ats_platform, ats_slug) already owned by
            a different company row. (# PORT-SEAM: private's changed_by Args
            entry dropped here too, same reason.)
    """
    raw = (
        conn.raw if hasattr(conn, "raw") else conn
    )  # PORT-SEAM: replaces private's utc_now_iso() now + dynamic-SET-clause comment

    set_parts: list[str] = [
        "ats_probe_status = 'pending'",
        "consecutive_empty_scans = 0",
        "retry_count = 0",
        "retry_after = NULL",
        "miss_reason = NULL",
        "updated_at = now()",  # PORT-SEAM: Postgres now() replaces private's Python-side utc_now_iso() bind param
    ]
    params: list[Any] = []  # PORT-SEAM: no now bind param (updated_at = now() is server-side)

    if ats_platform is not _UNSET:
        set_parts.append(
            "ats_platform = %s"
        )  # PORT-SEAM: psycopg %s placeholder replaces sqlite3 ?
        params.append(ats_platform)
    if ats_slug is not _UNSET:
        set_parts.append("ats_slug = %s")  # PORT-SEAM: psycopg %s placeholder replaces sqlite3 ?
        params.append(ats_slug)
    if careers_url is not _UNSET:
        set_parts.append("careers_url = %s")  # PORT-SEAM: psycopg %s placeholder replaces sqlite3 ?
        params.append(careers_url)
        set_parts.append(
            "scan_enabled = true"
        )  # PORT-SEAM: replaces private's careers_scan_enabled=1 + careers_crawl_flag_reason=NULL, see module docstring

    params.append(company_id)
    sql = f"UPDATE companies SET {', '.join(set_parts)} WHERE id = %s"  # PORT-SEAM: %s placeholder; private's before=snapshot_tracked(...) dropped, see module docstring

    try:
        with raw.transaction():
            raw.execute(sql, params)
    except psycopg.errors.UniqueViolation:  # PORT-SEAM: replaces sqlite3.IntegrityError
        # m0001's UNIQUE(ats_platform, ats_slug) gate. The inner
        # `with raw.transaction():` rolled back to its SAVEPOINT on the
        # exception, so this read sees the row's pre-write values for any
        # field the caller left as _UNSET.
        row = raw.execute(
            "SELECT ats_platform, ats_slug FROM companies WHERE id = %s",
            (company_id,),
        ).fetchone()
        plat = (
            ats_platform if ats_platform is not _UNSET else (row["ats_platform"] if row else None)
        )
        slug = ats_slug if ats_slug is not _UNSET else (row["ats_slug"] if row else None)
        owner = raw.execute(  # PORT-SEAM: raw.execute(...).fetchone() replaces cursor-based conn.execute()/fetchone()
            "SELECT id, name_raw FROM companies "
            "WHERE ats_platform = %s AND ats_slug = %s AND id != %s",  # PORT-SEAM: psycopg %s placeholders replace sqlite3 ?
            (plat, slug, company_id),
        ).fetchone()
        raise AttributionCollisionError(
            owner_id=owner["id"] if owner else None,
            owner_name=owner["name_raw"] if owner else None,
            ats_platform=plat,
            ats_slug=slug,
        ) from None

    commit_unless_nested(
        raw
    )  # PORT-SEAM: replaces private's after=snapshot_tracked/record_state_diff/conn.commit() -- see module docstring
