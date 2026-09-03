"""F4 — re-probe the ats_probe_status='miss' cohort against all 27 platforms.

Standalone ops script (no Flask needed). Per-row commit makes Ctrl+C safe.

Parallel design:
- Within a company: all 27 platform probes fire concurrently (inner pool).
  First True wins (cancel-others is best-effort because in-flight HTTP cannot
  actually be cancelled — but extras finishing late don't hurt).
- Across companies: configurable --workers outer pool. Default 3 to keep
  per-vendor concurrent load polite for small ATS providers.
- Per-vendor 429 detection via thread-local status capture (monkey-patch of
  requests.get) + exponential backoff (30s → 60s → 120s → ... up to 600s).
  Reset on next 200.

Usage:
    uv run --active python scripts/f4_reprobe_misses.py \\
        [--db jobs.db] [--limit N] [--workers 3] [--dry-run] [--serial]
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Ensure repo root on path for direct script invocation.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Monkey-patch requests.get BEFORE importing ats_prober so that prober calls
# go through our status-capturing wrapper. Thread-safe via threading.local().
import requests  # noqa: E402

_thread_status: threading.local = threading.local()
_original_get = requests.get


def _capturing_get(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Wrap requests.get to stash response status_code in thread-local."""
    resp = _original_get(*args, **kwargs)
    _thread_status.last_status = resp.status_code
    return resp


requests.get = _capturing_get  # type: ignore[assignment]

from jobcannon.engine.ats_detection import (  # noqa: E402
    derive_slug_candidates,
    probe_hit_consistent_or_dead_url,
)
from jobcannon.engine.ats_prober import (  # noqa: E402
    _probe_ashby,
    _probe_bamboohr,
    _probe_breezy,
    _probe_greenhouse,
    _probe_jazzhr,
    _probe_lever,
    _probe_personio,
    _probe_pinpoint,
    _probe_recruitee,
    _probe_teamtailor,
)
from jobcannon.engine.brand_blocklist import is_blocked_brand  # noqa: E402

_PROBES: list[tuple[str, Callable[[str], bool]]] = [
    ("lever", _probe_lever),
    ("greenhouse", _probe_greenhouse),
    ("ashby", _probe_ashby),
    ("recruitee", _probe_recruitee),
    ("breezy", _probe_breezy),
    ("jazzhr", _probe_jazzhr),
    ("pinpoint", _probe_pinpoint),
    ("teamtailor", _probe_teamtailor),
    ("personio", _probe_personio),
    ("bamboohr", _probe_bamboohr),
]

_DELAY_SECONDS = 0.5
_PROGRESS_EVERY = 50

# Exponential backoff schedule for 429s, per platform.
_BACKOFF_INITIAL_SECONDS = 30.0
_BACKOFF_MAX_SECONDS = 600.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("f4_reprobe")


class _BackoffState:
    """Per-platform 429 backoff. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until: dict[str, float] = {}
        self._attempts: dict[str, int] = {}

    def is_in_backoff(self, platform: str) -> bool:
        with self._lock:
            return time.monotonic() < self._until.get(platform, 0.0)

    def record_429(self, platform: str) -> None:
        with self._lock:
            attempts = self._attempts.get(platform, 0) + 1
            self._attempts[platform] = attempts
            delay = min(
                _BACKOFF_INITIAL_SECONDS * (2 ** (attempts - 1)),
                _BACKOFF_MAX_SECONDS,
            )
            self._until[platform] = time.monotonic() + delay
            log.warning(
                "platform %s: 429 detected — backoff %.0fs (attempt %d)",
                platform,
                delay,
                attempts,
            )

    def record_success(self, platform: str) -> None:
        with self._lock:
            if platform in self._attempts:
                log.info("platform %s: recovered after backoff", platform)
                self._attempts.pop(platform, None)
                self._until.pop(platform, None)


_backoff = _BackoffState()


def _probe_with_diagnostics(platform: str, probe_fn: Callable[[str], bool], slug: str) -> bool:
    """Run a single probe, updating per-vendor backoff state from the captured
    HTTP status. Returns the probe's bool result.
    """
    if _backoff.is_in_backoff(platform):
        return False
    _thread_status.last_status = None  # reset per call
    try:
        result = probe_fn(slug)
    except Exception as exc:
        log.debug("probe %s/%s raised %s", platform, slug, exc)
        return False
    status = getattr(_thread_status, "last_status", None)
    if status == 429:
        _backoff.record_429(platform)
    elif status == 200:
        # Even a 200-with-no-postings counts as evidence the vendor is alive.
        _backoff.record_success(platform)
    return result


def _probe_slug_parallel(
    slug: str, careers_url: str | None, company_name: str
) -> tuple[str, str] | None:
    """Probe every platform for one slug concurrently. First True wins.

    F6 consistency gate (with liveness): a hit whose platform disagrees with
    the platform inferred from `careers_url` is rejected ONLY when that
    careers_url is still live (not 404/410). If careers_url is stale (probably
    an ATS migration), the live probe hit wins. We keep waiting for the next
    future. If no consistent hit lands, returns None.
    """
    with ThreadPoolExecutor(max_workers=len(_PROBES), thread_name_prefix="probe") as pool:
        futures = {
            pool.submit(_probe_with_diagnostics, platform, probe, slug): platform
            for platform, probe in _PROBES
        }
        hit: tuple[str, str] | None = None
        for fut in as_completed(futures):
            platform = futures[fut]
            try:
                if not fut.result():
                    continue
            except Exception as exc:
                log.debug("probe future %s/%s raised %s", platform, slug, exc)
                continue
            if not probe_hit_consistent_or_dead_url(platform, careers_url):
                log.info(
                    "REJECT %s -> %s/%s (careers_url %s infers different platform AND is live)",
                    company_name,
                    platform,
                    slug,
                    careers_url,
                )
                continue
            hit = (platform, slug)
            break
        # Best-effort cancel of remaining futures (already-in-flight HTTP runs
        # to completion regardless; this just suppresses queued ones).
        for fut in futures:
            fut.cancel()
        return hit


def _probe_slug_serial(
    slug: str, careers_url: str | None, company_name: str
) -> tuple[str, str] | None:
    """Serial fallback path. Same ordering as ats_scanner/_probe.py.

    F6 consistency gate (with liveness): hits inconsistent with `careers_url`
    are skipped only when the careers_url is still live (not 404/410). This
    preserves brand-collision protection while allowing the probe to win on
    ATS migrations where the old careers_url is now dead.
    """
    for platform, probe in _PROBES:
        if not _probe_with_diagnostics(platform, probe, slug):
            continue
        if not probe_hit_consistent_or_dead_url(platform, careers_url):
            log.info(
                "REJECT %s -> %s/%s (careers_url %s infers different platform AND is live)",
                company_name,
                platform,
                slug,
                careers_url,
            )
            continue
        return (platform, slug)
    return None


def _probe_company(
    company_name: str, careers_url: str | None, parallel: bool
) -> tuple[str, str] | None:
    """Try each slug candidate until one hits. First hit wins."""
    for slug in derive_slug_candidates(company_name):
        hit = (
            _probe_slug_parallel(slug, careers_url, company_name)
            if parallel
            else _probe_slug_serial(slug, careers_url, company_name)
        )
        if hit:
            return hit
    return None


def _distribution(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT ats_probe_status, COUNT(*) FROM companies GROUP BY ats_probe_status"
    ).fetchall()
    return {(status or "<null>"): n for status, n in rows}


# ---- Per-worker processing ----------------------------------------------------


def _process_one(
    db_path: str,
    company_id: int,
    company_name: str,
    careers_url: str | None,
    parallel_inner: bool,
    dry_run: bool,
) -> tuple[int, str, str | None, str | None]:
    """Probe a single company and write its result. Returns (id, name,
    hit_platform_or_None, hit_slug_or_None).

    F8 — short-circuit when company name matches the brand blocklist. The
    speculative ladder is poisoned for famous brands (Shopify, Walmart, etc.)
    because slug-collisions with small-company ATS tenants produce ~29%
    FPs (see brand_blocklist.py rationale). Blocked rows are written back
    to 'miss' with miss_reason='blocked_brand' so the scheduler will not
    re-probe them on every restart.

    Opens its own sqlite3 connection so callers can safely run this in a
    ThreadPoolExecutor. WAL mode on the DB lets concurrent writers coexist.
    """
    if is_blocked_brand(company_name):
        log.info(
            "BLOCKED %s (id=%d) — brand blocklist, no probes attempted",
            company_name,
            company_id,
        )
        if not dry_run:
            conn = sqlite3.connect(db_path, timeout=30.0)
            try:
                now = datetime.now().isoformat()
                conn.execute(
                    """UPDATE companies
                       SET ats_probe_status='miss',
                           miss_reason='blocked_brand',
                           ats_probe_attempted_at=?, updated_at=?
                       WHERE id=?""",
                    (now, now, company_id),
                )
                conn.commit()
            finally:
                conn.close()
        return (company_id, company_name, None, None)

    hit = _probe_company(company_name, careers_url, parallel=parallel_inner)
    hit_platform = hit[0] if hit else None
    hit_slug = hit[1] if hit else None

    if dry_run:
        return (company_id, company_name, hit_platform, hit_slug)

    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        now = datetime.now().isoformat()
        if hit_platform:
            try:
                conn.execute(
                    """UPDATE companies
                       SET ats_platform=?, ats_slug=?, ats_probe_status='hit',
                           ats_probe_attempted_at=?, updated_at=?
                       WHERE id=?""",
                    (hit_platform, hit_slug, now, now, company_id),
                )
            except sqlite3.IntegrityError as exc:
                # m076's UNIQUE(ats_platform, ats_slug) gate. Another
                # company already owns this pair. Log and continue —
                # the F4 script is a best-effort reprobe and should not
                # abort on a single collision.
                log.warning(
                    "f4_reprobe_misses: collision %s/%s for id=%s (%s) — skipping. exc=%s",
                    hit_platform,
                    hit_slug,
                    company_id,
                    company_name,
                    exc,
                )
        else:
            conn.execute(
                """UPDATE companies
                   SET ats_probe_status='miss',
                       ats_probe_attempted_at=?, updated_at=?
                   WHERE id=?""",
                (now, now, company_id),
            )
        conn.commit()
    finally:
        conn.close()
    return (company_id, company_name, hit_platform, hit_slug)


# ---- Driver -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="jobs.db", help="Path to jobs.db")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most this many rows (0 = all). For smoke-testing.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=3,
        help="Outer parallelism: how many companies to probe concurrently "
        "(default 3 — keeps per-vendor concurrent load polite). Use 1 to "
        "disable outer parallelism. Inner parallelism (10 platforms per "
        "slug) is always on unless --serial.",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help="Disable inner parallelism. Forces serial probe of platforms "
        "within each company. Mostly for debugging or absolute politeness.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Probe without writing to DB. Logs hits but does not mutate "
        "ats_probe_status or ats_platform.",
    )
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    log.info("Distribution BEFORE: %s", _distribution(conn))
    log.info(
        "Mode: outer_workers=%d inner_parallel=%s dry_run=%s",
        args.workers,
        not args.serial,
        args.dry_run,
    )

    misses = conn.execute(
        "SELECT id, name_raw, careers_url FROM companies WHERE ats_probe_status='miss' ORDER BY id"
    ).fetchall()
    total = len(misses)
    if args.limit and args.limit < total:
        misses = misses[: args.limit]
        log.info("Limiting to first %d of %d misses", args.limit, total)
        total = len(misses)
    log.info("Cohort size: %d companies", total)

    if total == 0:
        log.info("Nothing to do.")
        return 0

    if not args.dry_run:
        ids = [row["id"] for row in misses]
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE companies SET ats_probe_status='pending' WHERE id IN ({placeholders})",
            ids,
        )
        conn.commit()
        log.info("Flipped %d rows miss -> pending", total)
        log.info("Distribution AFTER FLIP: %s", _distribution(conn))
    else:
        log.info("Dry run: NOT flipping miss -> pending")

    hits_by_platform: Counter[str] = Counter()
    counter_lock = threading.Lock()
    miss_count = 0
    processed = 0
    started = time.monotonic()

    work_items = [(row["id"], row["name_raw"], row["careers_url"]) for row in misses]
    db_path_str = str(db_path)

    def _on_completed(result: tuple[int, str, str | None, str | None]) -> None:
        nonlocal miss_count, processed
        _, company_name, hit_platform, hit_slug = result
        with counter_lock:
            if hit_platform and hit_slug:
                hits_by_platform[hit_platform] += 1
                log.info(
                    "HIT  %s -> %s/%s (running hits=%d)",
                    company_name,
                    hit_platform,
                    hit_slug,
                    sum(hits_by_platform.values()),
                )
            else:
                miss_count += 1
            processed += 1
            if processed % _PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - started
                rate = processed / elapsed if elapsed else 0
                eta_s = (total - processed) / rate if rate else 0
                log.info(
                    "progress %d/%d (%.1f%%) hits=%d misses=%d rate=%.2f/s eta=%.0fs",
                    processed,
                    total,
                    100.0 * processed / total,
                    sum(hits_by_platform.values()),
                    miss_count,
                    rate,
                    eta_s,
                )

    try:
        if args.workers <= 1:
            for company_id, company_name, careers_url in work_items:
                result = _process_one(
                    db_path_str,
                    company_id,
                    company_name,
                    careers_url,
                    parallel_inner=not args.serial,
                    dry_run=args.dry_run,
                )
                _on_completed(result)
                time.sleep(_DELAY_SECONDS)
        else:
            with ThreadPoolExecutor(
                max_workers=args.workers, thread_name_prefix="company"
            ) as outer:
                futures = {
                    outer.submit(
                        _process_one,
                        db_path_str,
                        company_id,
                        company_name,
                        careers_url,
                        not args.serial,
                        args.dry_run,
                    ): (company_id, company_name)
                    for company_id, company_name, careers_url in work_items
                }
                for fut in as_completed(futures):
                    try:
                        result = fut.result()
                    except Exception as exc:
                        company_id, company_name = futures[fut]
                        log.warning(
                            "company %s (%s) crashed: %s",
                            company_id,
                            company_name,
                            exc,
                        )
                        continue
                    _on_completed(result)
    except KeyboardInterrupt:
        log.warning(
            "Interrupted at %d/%d — completed work has been committed",
            processed,
            total,
        )

    elapsed = time.monotonic() - started
    log.info("Done in %.1fs (%.2f companies/sec)", elapsed, processed / elapsed if elapsed else 0)
    log.info("Total processed: %d", processed)
    log.info("Total hits: %d", sum(hits_by_platform.values()))
    log.info("Per-platform hits: %s", dict(hits_by_platform))
    log.info("Distribution AFTER: %s", _distribution(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
