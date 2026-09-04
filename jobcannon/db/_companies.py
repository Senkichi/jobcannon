"""upsert_company — Postgres port of the private repo's single company-write
chokepoint. Same signature, same monotonic probe-status rule
(hit=2 > pending=1 > miss=0; UPDATE applies at new_rank >= current_rank so an
equal-rank sighting still refreshes fields via COALESCE, but a downgrade
never lands), same collision semantics (UNIQUE(ats_platform, ats_slug):
another owner of the pair -> log, leave ATS fields untouched, return the id),
and same failure contract: name-policy rejects raise CompanyNameRejectedError
and any other failure is wrapped in CompanyUpsertError — never a silent None.
The malformed-name predicate is isalnum()-based like the private original
(Unicode-aware, and accepts digit-only names that isalpha() would reject).

Recorded divergence (Ledger L-0011, ats_company.py drift since Wave-1
baseline): four private-side commits landed after this module's baseline
(#1913 merged-loser resolution, #1881 company_state_history, #1867
normalizer v2, #1869 scan_enabled split). Two gaps were originally recorded
here; one is now closed:
(1) company_state_history — CLOSED by ledger L-0040. ``_update_existing``
below snapshots the tracked fields before its UPDATE and diffs them after
(``jobcannon.db._company_state.snapshot_tracked`` /
``record_state_diff``), same wrapping as private's ``ats_company.py``. This
needed the WI-13 ``ats_scan_enabled``/``careers_scan_enabled`` split
(``jobcannon/db/migrations/m0021_wi13_scan_lane_columns.py``) to land first
so the tracked set has all 6 public columns to read.
(2) merge-survivor resolution (#1913) — STILL OPEN. The private repo's
merged-loser handling picks a surviving row across duplicate companies;
this module's collision path (above) only handles the ats_platform/ats_slug
UNIQUE case, not a general merge. Unaddressed scope, not an intentional
non-port — a future row should close it, matching the intentional Phase-2
deferral recorded just below.

``jobcannon/db/_company_attribution.py`` (ledger L-0065, a sibling writer
this module does not call) still has its OWN separate, not-yet-closed
company_state_history gap — its module docstring names it explicitly as
follow-up now that this module's history wiring exists; wiring
``set_company_attribution`` is out of this row's scope (L-0040 covers only
this module's UPDATE path).

Row access note: all internal row reads use STRING keys only (never
positional). This function is called both through the pooled
connection_factory (HybridRow — supports both access styles) and directly
against a bare psycopg connection in tests (`dict_row` factory — a plain
dict, which does NOT support integer indexing). String-key access is the
only style both row shapes share.

Recorded divergence (Wave-1, name-collision scope ruling): the private
original's full name-normalization + denylist identity work is Phase-2
scope and is deliberately NOT ported here. This module only adds a
case-insensitive uniqueness guard — the `companies_name_ci_uq` index on
`lower(name)` (m0001) — so 'Acme Robotics' and 'ACME ROBOTICS' resolve to
the same row instead of creating a duplicate company. The first-seen
casing wins and is never renormalized.

Transaction-boundary note (recorded port deviation): the risky (possibly-
UniqueViolation) INSERT/UPDATE statements are each wrapped in their own
nested `with raw.transaction():` block purely for SAVEPOINT-based recovery —
if the statement fails, that block's __exit__ rolls back to the savepoint
(without aborting the whole surrounding transaction, real or ambient) so the
retry statement can run cleanly. That is NOT the same as a durable commit —
psycopg3's Transaction() degrades to a savepoint whenever the connection
already carries an open, non-Transaction-managed transaction (true here: the
initial bare `SELECT id, ats_probe_status ...` read a few lines above
already put the connection in that state), so exiting it does not make the
write visible to any OTHER connection. Durability is handled separately by
the explicit pool.commit_unless_nested(raw) call before each return — a
no-op when nested inside an ambient `with conn.transaction():` block (e.g.
tests/host/conftest.py's db_conn fixture, where psycopg3 forbids explicit
commit() outright), a real commit otherwise. The outer
`except Exception` handler (e.g. the companies hit-state CHECK firing when a
caller passes ats_probe_status="hit" without platform+slug) wraps the
failure in CompanyUpsertError and needs no
explicit rollback: whichever inner `with raw.transaction():` was open when
the exception was raised already rolled itself back automatically.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from jobcannon.db._company_state import record_state_diff, snapshot_tracked
from jobcannon.db.pool import commit_unless_nested

logger = logging.getLogger(__name__)


class CompanyNameRejectedError(ValueError):
    """Raised when a company name is rejected by the name-policy boundary.

    Carries the rejected raw name and the classification reason so callers
    cannot silently treat a failure as a missing id.
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"Company name rejected: {name!r} ({reason})")


class CompanyUpsertError(RuntimeError):
    """Raised when upsert_company fails for a non-name-policy reason."""

    def __init__(self, name: str, cause: Exception):
        self.name = name
        self.cause = cause
        super().__init__(f"upsert_company failed for {name!r}: {cause}")


_PROBE_STATUS_PRECEDENCE = {"hit": 2, "pending": 1, "miss": 0}
_MAX_NAME_LEN = 200

# Constraint names that indicate a case-insensitive NAME collision (as
# opposed to an ats_platform/ats_slug collision), so the INSERT
# UniqueViolation handler can route to the right recovery path.
# companies_name_key is the implicit constraint Postgres names for the
# plain UNIQUE(name) column; companies_name_ci_uq is the case-insensitive
# guard added alongside it (m0001).
_NAME_COLLISION_CONSTRAINTS = frozenset({"companies_name_key", "companies_name_ci_uq"})


def _update_existing(
    raw: Any,
    company_id: int,
    current_status: str,
    normalized: str,
    ats_platform: str | None,
    ats_slug: str | None,
    ats_probe_status: str,
    homepage_url: str | None,
) -> int:
    """Shared existing-row update path: monotonic probe-status rule +
    collision-safe ATS field write. Used both by the normal "row already
    existed" flow and by the INSERT-side name-collision recovery path.

    # PORT-SEAM: snapshot_tracked/record_state_diff wrapping added (ledger
    # L-0040) -- mirrors how private's job_finder/web/ats_company.py wraps
    # this same UPDATE. Reads before the branch below, diffs after every
    # path through it resolves, so a collision fallback (ATS fields left
    # untouched) still records whatever DID change (e.g. homepage_url is
    # not tracked, so a pure-collision retry correctly records zero rows).
    """
    state_before = snapshot_tracked(raw, company_id)
    current_rank = _PROBE_STATUS_PRECEDENCE.get(current_status, 0)
    new_rank = _PROBE_STATUS_PRECEDENCE.get(ats_probe_status, 0)
    if new_rank >= current_rank:
        try:
            with raw.transaction():
                raw.execute(
                    "UPDATE companies SET "
                    "  ats_platform = COALESCE(%s, ats_platform), "
                    "  ats_slug = COALESCE(%s, ats_slug), "
                    "  ats_probe_status = %s, "
                    "  homepage_url = COALESCE(%s, homepage_url), "
                    "  consecutive_empty_scans = CASE WHEN %s::text IS NOT NULL AND %s::text IS NOT NULL "
                    "      THEN 0 ELSE consecutive_empty_scans END, "
                    "  updated_at = now() "
                    "WHERE id = %s",
                    (
                        ats_platform,
                        ats_slug,
                        ats_probe_status,
                        homepage_url,
                        ats_platform,
                        ats_slug,
                        company_id,
                    ),
                )
        except psycopg.errors.UniqueViolation as exc:
            logger.warning(
                "upsert_company: ATS collision for %r on %s/%s — leaving ATS fields untouched. exc=%s",
                normalized,
                ats_platform,
                ats_slug,
                exc,
            )
            with raw.transaction():
                raw.execute(
                    "UPDATE companies SET homepage_url = COALESCE(%s, homepage_url), updated_at = now() "
                    "WHERE id = %s",
                    (homepage_url, company_id),
                )
    else:
        with raw.transaction():
            raw.execute(
                "UPDATE companies SET homepage_url = COALESCE(%s, homepage_url), updated_at = now() "
                "WHERE id = %s",
                (homepage_url, company_id),
            )
    # PORT-SEAM: see docstring above -- L-0040 wiring.
    record_state_diff(
        raw, company_id, state_before, snapshot_tracked(raw, company_id), "upsert_company"
    )
    commit_unless_nested(raw)
    return company_id


def upsert_company(
    conn: Any,
    name: str,
    ats_platform: str | None = None,
    ats_slug: str | None = None,
    ats_probe_status: str = "pending",
    homepage_url: str | None = None,
) -> int:
    raw = conn.raw if hasattr(conn, "raw") else conn
    normalized = (name or "").strip()
    if not normalized:
        raise CompanyNameRejectedError(name or "", "empty_after_cleanup")
    if not any(c.isalnum() for c in normalized):
        raise CompanyNameRejectedError(name, "no_alphanumeric_characters")
    if len(normalized) > _MAX_NAME_LEN:
        raise CompanyNameRejectedError(name, "overlong")
    try:
        existing = raw.execute(
            "SELECT id, ats_probe_status FROM companies WHERE lower(name) = lower(%s)",
            (normalized,),
        ).fetchone()
        if existing is None:
            try:
                with raw.transaction():
                    row = raw.execute(
                        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status, homepage_url) "
                        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                        (
                            normalized,
                            normalized,
                            ats_platform,
                            ats_slug,
                            ats_probe_status,
                            homepage_url,
                        ),
                    ).fetchone()
            except psycopg.errors.UniqueViolation as exc:
                constraint = getattr(exc.diag, "constraint_name", None)
                if constraint in _NAME_COLLISION_CONSTRAINTS:
                    # Another writer beat us to the same company under
                    # different casing (or the identical name) — resolve to
                    # that row and continue down the existing-row update
                    # path instead of the ATS-pair fallback below.
                    logger.info(
                        "upsert_company: case-insensitive name collision for %r — "
                        "resolving to the existing row",
                        normalized,
                    )
                    existing = raw.execute(
                        "SELECT id, ats_probe_status FROM companies WHERE lower(name) = lower(%s)",
                        (normalized,),
                    ).fetchone()
                    company_id, current_status = (
                        existing["id"],
                        existing["ats_probe_status"] or "pending",
                    )
                    return _update_existing(
                        raw,
                        company_id,
                        current_status,
                        normalized,
                        ats_platform,
                        ats_slug,
                        ats_probe_status,
                        homepage_url,
                    )
                # (ats_platform, ats_slug) collision on INSERT: retry without ATS fields.
                logger.warning(
                    "upsert_company: ATS collision for %r on %s/%s — inserting without ATS fields",
                    normalized,
                    ats_platform,
                    ats_slug,
                )
                with raw.transaction():
                    row = raw.execute(
                        "INSERT INTO companies (name, name_raw, homepage_url) "
                        "VALUES (%s, %s, %s) RETURNING id",
                        (normalized, normalized, homepage_url),
                    ).fetchone()
            commit_unless_nested(raw)
            return row["id"]

        company_id, current_status = existing["id"], existing["ats_probe_status"] or "pending"
        return _update_existing(
            raw,
            company_id,
            current_status,
            normalized,
            ats_platform,
            ats_slug,
            ats_probe_status,
            homepage_url,
        )
    except CompanyNameRejectedError:
        raise
    except Exception as e:
        logger.warning("upsert_company failed for '%s' (non-fatal): %s", name, e)
        raise CompanyUpsertError(name, e) from e
