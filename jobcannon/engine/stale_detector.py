# PORTED from job_finder/web/stale_detector.py @ 4348fc77093fa44e7be4e29a97ded6bed7d9ced3 (private job-cannon). Ledger L-0039.
"""Stale job detection and auto-archive logic.

Runs nightly (via APScheduler CronTrigger, as Phase A of the unified
staleness orchestrator) to:
1. Mark passive-stage jobs as stale when no liveness evidence for 14+ days
   (5 days for jobs the Phase C cascade has checked but could never
   positively confirm — see "Two-tier threshold" below).
2. Clear stale flag for jobs seen again (and for any job outside the
   passive stages — staleness is only meaningful pre-application).
3. Auto-archive discovered/reviewing jobs not seen for 30+ days.

"Seen" means any liveness evidence: a feed re-sighting (upsert touch), ATS
board presence (Phase B reconciler), or an HTTP live verdict (Phase C
cascade / scoring preflight via persist_job_expiry_state) — all of which
refresh last_seen.

Two-tier stale threshold (expiry_status-aware): some sources' links can
never be resolved by the Phase C cascade at all — e.g. Jooble's `/away/`
click-redirect URLs sit behind a Cloudflare bot-challenge that returns 403
to every request, and Jooble postings carry no ats_platform (Signal 1
N/A) and frequently no resolvable company homepage (Signal 2 N/A either).
Such jobs permanently land on expiry_status='inconclusive' — never
'live', never 'expired' — and every consumer (scoring gate, Phase C
archiver) only branches on 'expired', so 'inconclusive' silently renders
identical to a confirmed-live job. The standard 14-day grace period
assumes "no re-sighting yet" from a job we know how to verify; it is far
too generous for a job we have never been able to verify at all. Jobs
stuck at 'inconclusive' get the shorter _UNVERIFIED_STALE_THRESHOLD_DAYS
instead, closing that exposure window. A job with expiry_status IS NULL
(not yet checked, e.g. pre-scoring) still gets the standard threshold —
only an actual failed verification attempt shortens the grace period.

CRITICAL: Jobs in active pipeline stages (applied, phone_screen, technical,
onsite, offer, accepted) are NEVER auto-archived — they require explicit
action. They are never marked stale either: an applied job naturally stops
being re-sighted, and the default jobs view hides stale rows, so marking
them stale silently hid active applications (21 such rows at the
2026-06-11 audit).

Gated-only re-sightings do not reset the unverified decay clock (#1077):
some aggregators (Jooble et al., the same opaque/gated set as
is_opaque_redirect_source) republish stale inventory as fresh results, so a
job whose sources are ALL gated re-sightings gets its last_seen refreshed
forever without ever being independently corroborated — last_seen looking
fresh is not evidence of life for such a job. Two passes close this loop:
(1) a second mark pass keys the unverified cutoff on expiry_checked_at
instead of last_seen for gated-only rows, since last_seen never ages for
them; (2) the clear pass excludes gated-only rows from its last_seen-based
arm (unless expiry_status='live', a real independent corroboration), so it
cannot immediately undo what (1) just marked. See the inline comments on
the gated-only mark and clear blocks below for the full ordering
composition — it matters that (1) runs before (2) inside the same
transaction.
"""

# PORT-SEAM: copy/os/pathlib/yaml (private's on-disk config refresh) are not
# needed — see the disk-refresh PORT-SEAM note below.
import logging

# PORT-SEAM: os (private's on-disk config refresh) is not needed here.
from datetime import UTC, datetime, timedelta

# PORT-SEAM: pathlib.Path + yaml (private's on-disk config refresh) are not
# needed — see the disk-refresh PORT-SEAM note below.
from jobcannon.engine.json_utils import utc_now_iso

# PORT-SEAM: DB access routes through ScanServices.connection_factory, replacing
# job_finder.web.db_helpers.standalone_connection (host owns connection lifecycle).
from jobcannon.engine.services import get_services
from jobcannon.engine.source_registry import (
    UNVERIFIABLE_EVIDENCE_CEILING,
    UNVERIFIABLE_EVIDENCE_CONFIRMED,
    is_opaque_redirect_source,
    is_unverifiable_candidate,
)

logger = logging.getLogger(__name__)

# Default thresholds — days since last_seen before triggering each action.
# Overridable via config: staleness.stale_threshold_days / archive_threshold_days /
# unverified_stale_threshold_days.
_STALE_THRESHOLD_DAYS = 14  # Mark job as stale after this many days without re-sighting
_ARCHIVE_THRESHOLD_DAYS = 30  # Auto-archive passive-stage jobs after this many days
# Shorter threshold for jobs the cascade has checked but could never confirm
# live (expiry_status='inconclusive') — see module docstring "Two-tier
# stale threshold".
_UNVERIFIED_STALE_THRESHOLD_DAYS = 5


# PORT-SEAM: the private original's on-disk config.yaml refresh for
# opaque_redirect_sources (#1513/#1607 — job_finder.web.user_data_dirs.config_path()
# + _load_disk_opaque_sources/_refresh_opaque_sources_from_disk) is deliberately NOT
# ported here: it is config-file-shaped and needs the host's own config surface, not
# an engine-owned one. run_stale_detection below takes the passed-in `config` as-is;
# a host wanting the disk-freshness fix supplies an already-refreshed config.


# Stages where time-based staleness is meaningful. Active pipeline stages
# (applied onward) and user-resolved stages (dismissed, archived) are excluded.
_PASSIVE_STATUSES = ("discovered", "reviewing")


def run_stale_detection(db_path: str, config: dict | None = None) -> dict:
    """Run stale detection and auto-archive on the job database.

    Creates its own SQLite connection (thread-safe for background jobs).

    Rules:
    - Stale: passive-stage job with last_seen older than the stale threshold
      → set is_stale = 1. Jobs stuck at expiry_status='inconclusive' (the
      cascade checked but could never confirm liveness) use the shorter
      unverified threshold instead — see module docstring "Two-tier stale
      threshold".
    - Clear: job seen recently again, OR job no longer in a passive stage
      → set is_stale = 0. EXCEPTION (#1077): a gated-only job's last_seen
      refresh does not count as "seen recently" for this arm — see module
      docstring "Gated-only re-sightings" — unless expiry_status='live'.
    - Auto-archive: last_seen older than the archive threshold AND
      pipeline_status IN ('discovered', 'reviewing') → 'archived'
      (does NOT archive applied/phone_screen/technical/onsite/offer/accepted)

    Cutoffs are computed in Python with the canonical naive-UTC ISO-8601
    'T'-separator format. SQLite's datetime('now') emits a space separator,
    which string-compares against stored 'T' timestamps at date granularity
    only — the previous SQL-side cutoff was silently ~24h sloppy.

    Args:
        db_path: Path to the SQLite database file.
        config: Application config dict; reads staleness.stale_threshold_days,
            staleness.archive_threshold_days (defaults 14 / 30),
            staleness.unverified_stale_threshold_days (default 5), and
            staleness.unverifiable_grace_days / unverifiable_ceiling_days
            (defaults 14 / 60, Section 4 visibility policy).

    Returns:
        dict with keys:
            stale_marked (int): Jobs newly marked as stale.
            stale_cleared (int): Jobs cleared from stale (re-seen or non-passive).
            archived (int): Jobs auto-archived.
            unverifiable_archived (int): Jobs auto-archived under Section 4's
                visibility policy (opaque-only sources, never corroborated,
                grace-period-gated branch match or hard-ceiling backstop).
    """
    # PORT-SEAM: private source refreshes opaque_redirect_sources from disk
    # here (#1513); not ported — see the module-level PORT-SEAM note above.
    staleness_cfg = (config or {}).get("staleness", {})
    stale_days = staleness_cfg.get("stale_threshold_days", _STALE_THRESHOLD_DAYS)
    archive_days = staleness_cfg.get("archive_threshold_days", _ARCHIVE_THRESHOLD_DAYS)
    unverified_stale_days = staleness_cfg.get(
        "unverified_stale_threshold_days", _UNVERIFIED_STALE_THRESHOLD_DAYS
    )

    now_naive_utc = datetime.now(UTC).replace(tzinfo=None)
    stale_cutoff = (now_naive_utc - timedelta(days=stale_days)).isoformat()
    unverified_stale_cutoff = (now_naive_utc - timedelta(days=unverified_stale_days)).isoformat()
    archive_cutoff = (now_naive_utc - timedelta(days=archive_days)).isoformat()

    passive_placeholders = ",".join("?" * len(_PASSIVE_STATUSES))
    svc = get_services()  # PORT-SEAM: connection ownership moves to the host's ScanServices

    with svc.connection_factory() as conn:
        try:
            # Mark passive-stage jobs as stale: no liveness evidence since the
            # applicable cutoff. Jobs the cascade left at 'inconclusive' (never
            # positively confirmed live) use the shorter unverified_stale_cutoff;
            # everything else (NULL = not yet checked, 'live' = confirmed)
            # keeps the standard stale_cutoff.
            #
            # ISSUE #1077 FIX: For jobs whose sources are ALL gated/opaque (is_opaque_redirect_source),
            # re-sightings from those same sources must not reset the unverified decay clock.
            # We key the unverified cutoff on expiry_checked_at (when the job became unverifiable)
            # instead of last_seen for gated-only jobs. This prevents the infinite loop where
            # Jooble republishes stale inventory as fresh, refreshing last_seen and evading decay.
            #
            # Implementation: We use a hybrid approach - first mark based on last_seen (standard),
            # then fetch gated-only candidates and mark based on expiry_checked_at. This ensures
            # exact predicate parity with is_opaque_redirect_source without complex SQL.
            cursor = conn.execute(
                "UPDATE jobs SET is_stale = 1 "
                "WHERE is_stale = 0 "
                f"AND pipeline_status IN ({passive_placeholders}) "
                "AND last_seen < CASE WHEN expiry_status = 'inconclusive' THEN ? ELSE ? END",
                (*_PASSIVE_STATUSES, unverified_stale_cutoff, stale_cutoff),
            )
            stale_marked = cursor.rowcount

            # Second pass for gated-only jobs: use expiry_checked_at instead of last_seen
            # for the unverified cutoff. This catches the case where last_seen is fresh
            # (due to re-sightings from the same gated source) but the job has been
            # unverifiable for longer than the threshold.
            gated_candidates = conn.execute(
                f"""SELECT dedup_key, sources, source_urls, expiry_checked_at
                   FROM jobs
                   WHERE is_stale = 0
                     AND pipeline_status IN ({passive_placeholders})
                     AND expiry_status = 'inconclusive'
                     AND expiry_checked_at IS NOT NULL
                     AND expiry_checked_at < ?""",
                (*_PASSIVE_STATUSES, unverified_stale_cutoff),
            ).fetchall()

            gated_to_mark = []
            for row in gated_candidates:
                if is_opaque_redirect_source(row, config or {}):
                    gated_to_mark.append(row["dedup_key"])

            if gated_to_mark:
                placeholders = ",".join("?" * len(gated_to_mark))
                cursor = conn.execute(
                    f"UPDATE jobs SET is_stale = 1 WHERE dedup_key IN ({placeholders})",
                    gated_to_mark,
                )
                gated_marked = cursor.rowcount
                stale_marked += gated_marked
                logger.info(
                    "Stale detection: marked %d gated-only jobs as stale (expiry_checked_at < %s days)",
                    gated_marked,
                    unverified_stale_days,
                )

            # Clear stale flag for jobs seen recently again, and for jobs that
            # left the passive stages (staleness is meaningless post-application).
            # Mirrors the same per-row CASE as the mark-stale UPDATE above —
            # using the standard (longer) cutoff unconditionally here would
            # immediately re-clear anything just flagged via the shorter
            # unverified cutoff, since its last_seen is by definition more
            # recent than the standard cutoff too.
            #
            # ISSUE #1077 FIX — composition of the three passes above/here,
            # in order:
            #   1. mark pass (last_seen, standard/inconclusive two-tier cutoff)
            #   2. gated-only mark pass (expiry_checked_at instead of
            #      last_seen — catches what (1) can never catch, because a
            #      gated-only job's last_seen is refreshed by every re-sighting
            #      from the very source that can never verify it, so it never
            #      ages past a last_seen-based cutoff)
            #   3. clear pass (this block)
            # Without the exclusion below, (3) runs immediately after (2) IN
            # THE SAME TRANSACTION and undoes it on the spot: (2) just set
            # is_stale=1 for a gated-only row precisely because last_seen is
            # fresh (that's the whole bug), so the naive
            # `last_seen >= cutoff` arm here would immediately clear it right
            # back to 0 — verified empirically against the issue's named
            # regression fixture (stale_marked=1, stale_cleared=1, final
            # is_stale=0 on current main).
            #
            # Fix: a gated-only job's fresh last_seen is not evidence of life
            # — every re-sighting it can ever get comes from the same
            # never-verifiable source. So the last_seen-based clear arm
            # excludes gated-only rows (is_opaque_redirect_source — the exact
            # same predicate (2) and the migration use; one place, not a
            # diverging copy) UNLESS expiry_status='live', which is a real,
            # independent corroboration and clears normally. Gated-only rows
            # can still be cleared via the OTHER arm (left the passive
            # stage — e.g. the user applied to it).
            clear_last_seen_candidates = conn.execute(
                f"""SELECT dedup_key, sources, source_urls, expiry_status
                   FROM jobs
                   WHERE is_stale = 1
                     AND pipeline_status IN ({passive_placeholders})
                     AND last_seen >= CASE WHEN expiry_status = 'inconclusive' THEN ? ELSE ? END""",
                (*_PASSIVE_STATUSES, unverified_stale_cutoff, stale_cutoff),
            ).fetchall()

            gated_only_excluded = [
                row["dedup_key"]
                for row in clear_last_seen_candidates
                if row["expiry_status"] != "live" and is_opaque_redirect_source(row, config or {})
            ]

            exclude_clause = ""
            exclude_params: tuple = ()
            if gated_only_excluded:
                exclude_placeholders = ",".join("?" * len(gated_only_excluded))
                exclude_clause = f"AND dedup_key NOT IN ({exclude_placeholders})"
                exclude_params = tuple(gated_only_excluded)

            cursor = conn.execute(
                "UPDATE jobs SET is_stale = 0 "
                "WHERE is_stale = 1 AND ("
                "(last_seen >= CASE WHEN expiry_status = 'inconclusive' THEN ? ELSE ? END "
                f"{exclude_clause}) "
                f"OR pipeline_status NOT IN ({passive_placeholders}))",
                (unverified_stale_cutoff, stale_cutoff, *exclude_params, *_PASSIVE_STATUSES),
            )
            stale_cleared = cursor.rowcount

            conn.commit()

            # Auto-archive discovered/reviewing jobs not seen for the archive window.
            # CRITICAL: only archive passive stages, never active pipeline stages.
            # BATCH-03: batch UPDATE + executemany INSERT instead of per-row status calls
            rows_to_archive = conn.execute(
                "SELECT dedup_key, pipeline_status FROM jobs "
                "WHERE last_seen < ? "
                f"AND pipeline_status IN ({passive_placeholders})",
                (archive_cutoff, *_PASSIVE_STATUSES),
            ).fetchall()

            archived = 0
            if rows_to_archive:
                keys = [r["dedup_key"] for r in rows_to_archive]
                placeholders = ",".join("?" * len(keys))

                # Bulk UPDATE jobs to archived
                conn.execute(
                    f"UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key IN ({placeholders})",
                    keys,
                )

                # Bulk INSERT pipeline_events (audit trail)
                now = utc_now_iso()
                evidence = f"not_seen_{archive_days}_days"
                conn.executemany(
                    "INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp, source, evidence) "
                    "VALUES (?, ?, 'archived', ?, 'stale_detector', ?)",
                    [
                        (r["dedup_key"], r["pipeline_status"], now, evidence)
                        for r in rows_to_archive
                    ],
                )
                conn.commit()
                archived = len(keys)

            # --- Section 4 (job-listing-verification, Plan 3): archive
            # unverifiable-aggregator-listing candidates. A job whose
            # sources are entirely within the opaque-redirect registry and
            # has never been corroborated (direct_url IS NULL) is archived
            # once its specific dead-end branch is satisfied (grace-period
            # gated), or unconditionally once the longer hard-ceiling age is
            # reached, whichever comes first. Runs after the standard
            # archiver's commit so it can never double-count rows that pass
            # just touched, and stays inside this function's existing try/
            # except so the same rollback-on-error guarantee covers it.
            #
            # Branches (all additionally require is_unverifiable_candidate,
            # the Section 4/5 shared base predicate — opaque-only sources,
            # direct_url IS NULL — and first_seen older than
            # unverifiable_grace_days):
            #   1. company_id IS NULL — nothing further reachable.
            #   2. company_id resolved, companies.scan_enabled = 0 —
            #      confirmed unscannable, nothing further reachable.
            #   3. ats_probe_status = 'miss' AND careers_checked_at IS NOT
            #      NULL — no ATS board ever found; the one reachable signal
            #      (careers-page) was tried and didn't confirm.
            #   4. ats_probe_status = 'hit' AND direct_url_attempts >=
            #      max_attempts AND careers_checked_at IS NOT NULL — a real
            #      board was found and searched repeatedly, AND the
            #      careers-page channel was independently tried too.
            # Backstop: independent of every branch, first_seen older than
            # unverifiable_ceiling_days archives unconditionally — closes
            # every reachability gap not enumerated above (probe stuck
            # 'pending', homepage never discovered, etc.).
            unverifiable_grace_days = staleness_cfg.get("unverifiable_grace_days", 14)
            unverifiable_ceiling_days = staleness_cfg.get("unverifiable_ceiling_days", 60)
            # Mirrors primary_source_resolver._resolver_settings's exact
            # config path and default — the resolver's own retry-budget
            # value, read directly (not imported; that helper is private and
            # this module has no other dependency on that file).
            max_attempts = (
                (config or {}).get("direct_link", {}).get("resolver", {}).get("max_attempts", 3)
            )
            grace_cutoff = (now_naive_utc - timedelta(days=unverifiable_grace_days)).isoformat()
            ceiling_cutoff = (
                now_naive_utc - timedelta(days=unverifiable_ceiling_days)
            ).isoformat()  # PORT-SEAM: ruff line-length 100 vs 99 wraps this differently; pure reformat

            unverifiable_rows = conn.execute(
                "SELECT j.dedup_key, j.pipeline_status, j.sources, j.source_urls, "
                "j.direct_url, j.first_seen, j.company_id, j.careers_checked_at, "
                "j.direct_url_attempts, c.scan_enabled, c.ats_probe_status "  # PORT-SEAM: ats_scan_enabled/careers_scan_enabled split reverted (invented column, no migration backs it)
                "FROM jobs j "
                "LEFT JOIN companies c ON c.id = j.company_id "
                f"WHERE j.first_seen < ? AND j.pipeline_status IN ({passive_placeholders})",
                (grace_cutoff, *_PASSIVE_STATUSES),
            ).fetchall()

            unverifiable_to_archive: list[
                tuple[str, str, str]
            ] = []  # (dedup_key, from_status, evidence)
            for row in unverifiable_rows:
                if not is_unverifiable_candidate(row, config or {}):
                    continue

                if row["first_seen"] < ceiling_cutoff:
                    unverifiable_to_archive.append(
                        (row["dedup_key"], row["pipeline_status"], UNVERIFIABLE_EVIDENCE_CEILING)
                    )
                    continue

                company_id = row["company_id"]
                scan_enabled = row[
                    "scan_enabled"
                ]  # PORT-SEAM: ats_scan_enabled/careers_scan_enabled split reverted
                probe_status = row["ats_probe_status"]
                careers_checked = row["careers_checked_at"]
                attempts = row["direct_url_attempts"] or 0

                branch_matched = (
                    company_id is None  # Branch 1
                    or (
                        company_id is not None and scan_enabled == 0
                    )  # Branch 2 (# PORT-SEAM: ats_scan_enabled/careers_scan_enabled split reverted)
                    or (probe_status == "miss" and careers_checked is not None)  # Branch 3
                    or (
                        probe_status == "hit"
                        and attempts >= max_attempts
                        and careers_checked is not None
                    )  # Branch 4
                )
                if branch_matched:
                    unverifiable_to_archive.append(
                        (row["dedup_key"], row["pipeline_status"], UNVERIFIABLE_EVIDENCE_CONFIRMED)
                    )

            unverifiable_archived = 0
            if unverifiable_to_archive:
                keys = [r[0] for r in unverifiable_to_archive]
                placeholders = ",".join("?" * len(keys))
                conn.execute(
                    f"UPDATE jobs SET pipeline_status = 'archived' WHERE dedup_key IN ({placeholders})",
                    keys,
                )
                now = utc_now_iso()
                conn.executemany(
                    "INSERT INTO pipeline_events (job_id, from_status, to_status, timestamp, source, evidence) "
                    "VALUES (?, ?, 'archived', ?, 'stale_detector', ?)",
                    [
                        (dk, from_status, now, evidence)
                        for dk, from_status, evidence in unverifiable_to_archive
                    ],
                )
                conn.commit()
                unverifiable_archived = len(keys)

            result = {
                "stale_marked": stale_marked,
                "stale_cleared": stale_cleared,
                "archived": archived,
                "unverifiable_archived": unverifiable_archived,
            }
            logger.info("Stale detection complete: %s", result)
            return result

        except Exception:
            conn.rollback()
            logger.exception("Stale detection failed")
            raise
