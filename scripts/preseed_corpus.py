"""Corpus pre-seed: upsert the wedge companies, then enqueue their
scans THROUGH THE QUEUE — the pre-seed is the scheduler's first real-volume
proof, so it must not bypass procrastinate with direct run_scan_task calls.

Usage (operator, from the deployed worker environment or a shell with
DATABASE_URL set):
    python scripts/preseed_corpus.py data/seed_companies.csv           # upsert + enqueue
    python scripts/preseed_corpus.py data/seed_companies.csv --verify  # GET-check board URLs, no writes
    python scripts/preseed_corpus.py data/seed_companies.csv --limit 8 # staging-scan subset (spec §8)

Note on --verify timing: each row now gets up to 3 attempts at a 6s read
timeout with backoff between them (#106), so a run against a CSV with many
genuinely-dead boards can take noticeably longer than a quick spot check —
budget minutes, not seconds, on the full corpus.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import time

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("preseed_corpus")

# --verify's whole point is to distinguish "board URL is actually dead" from
# "network had a slow moment." A tight timeout collapses that distinction
# (#106: non-overlapping failure sets across two runs against the same CSV
# from the same network — pure timeout flakiness, not real unreachability).
# 6s read timeout + a couple of retries absorbs ordinary Greenhouse latency
# variance without materially slowing down a genuinely-dead-board verdict.
_VERIFY_TIMEOUT_S = 6
_VERIFY_MAX_ATTEMPTS = 3
_VERIFY_BACKOFF_BASE_S = 1.0


def _get_with_retries(url: str, *, timeout: float, attempts: int, backoff_base: float):
    """GET with a small retry-with-backoff loop, dependency-free (stdlib
    ``time.sleep`` — no urllib3 Retry/HTTPAdapter wiring, no tenacity).
    Retries only on network-level failures (timeout, connection reset) —
    those are the transient case; an actual HTTP error status is returned
    as-is on the first attempt, not retried, since it's not flaky."""
    import requests

    last_exc: requests.RequestException | None = None
    for attempt in range(attempts):
        try:
            return requests.get(url, timeout=timeout, allow_redirects=True)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(backoff_base * (attempt + 1))
    raise last_exc  # type: ignore[misc]  # loop always sets last_exc before exhausting attempts


_BOARD_URL_BUILDERS = {
    "greenhouse": lambda slug: f"https://boards.greenhouse.io/{slug}",
    "lever": lambda slug: f"https://jobs.lever.co/{slug}",
    "ashby": lambda slug: f"https://jobs.ashbyhq.com/{slug}",
}

# Ashby is a client-side-routed SPA: EVERY slug, real or fake, returns HTTP
# 200 with the same generic app shell (verified empirically 2026-07-18 — a
# deliberately-invalid slug also 200s). The <title> tag IS server-rendered
# per-org though: a nonexistent org gets the literal shell default "Jobs";
# a real org gets "<Company> Jobs". A bare status-code check would silently
# accept every Ashby row regardless of slug validity, so Ashby rows are
# discriminated on <title> instead of status.
_ASHBY_GENERIC_TITLE = "Jobs"


def _read_rows(csv_path: str) -> list[dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _board_url(row: dict[str, str]) -> str | None:
    builder = _BOARD_URL_BUILDERS.get(row["ats_platform"])
    return builder(row["ats_slug"]) if builder else None


def _verify(rows: list[dict[str, str]]) -> int:
    import requests

    unreachable = []
    for row in rows:
        url = _board_url(row)
        if url is None:
            unreachable.append((row["name"], row["ats_platform"], "no URL builder"))
            continue
        try:
            # GET, not HEAD: Ashby's title-based discrimination (below) needs
            # a real body, and some boards 404 on HEAD but 200 on GET anyway.
            resp = _get_with_retries(
                url,
                timeout=_VERIFY_TIMEOUT_S,
                attempts=_VERIFY_MAX_ATTEMPTS,
                backoff_base=_VERIFY_BACKOFF_BASE_S,
            )
            ok = resp.status_code < 400
            if ok and row["ats_platform"] == "ashby":
                m = re.search(r"<title>(.*?)</title>", resp.text)
                title = m.group(1).strip() if m else ""
                ok = title != "" and title != _ASHBY_GENERIC_TITLE
        except requests.RequestException as exc:
            ok = False
            log.warning("%s (%s): request failed: %s", row["name"], url, exc)
        if ok:
            log.info("OK   %-30s %s", row["name"], url)
        else:
            unreachable.append((row["name"], url, "unreachable"))
            log.warning("FAIL %-30s %s", row["name"], url)
    if unreachable:
        log.error("%d/%d board URLs unreachable", len(unreachable), len(rows))
        return 1
    log.info("all %d board URLs reachable", len(rows))
    return 0


def _upsert_companies(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Upsert each row's company via the pooled connection factory (requires
    the engine seams to already be wired). Returns the rows that landed
    (upsert_company raises CompanyNameRejectedError on malformed names and
    CompanyUpsertError on row-level DB failures — both logged and skipped
    here — so one bad manifest row can't abort the seed; the caller enqueues
    scans only for the returned rows, so a skipped row never gets an orphan
    scan for a company that was never inserted)."""
    from jobcannon.db import _companies
    from jobcannon.engine import services

    svc = services.get_services()
    landed: list[dict[str, str]] = []
    with svc.connection_factory() as conn:
        for row in rows:
            try:
                _companies.upsert_company(
                    conn,
                    row["name"],
                    ats_platform=row["ats_platform"],
                    ats_slug=row["ats_slug"],
                    ats_probe_status="hit",
                    homepage_url=row.get("homepage_url") or None,
                )
            except (
                _companies.CompanyNameRejectedError,
                _companies.CompanyUpsertError,
            ) as exc:
                log.warning("SKIP %-30s %s", row["name"], exc)
                continue
            landed.append(row)
        conn.commit()
    return landed


def _enqueue_scans(rows: list[dict[str, str]]) -> tuple[int, int]:
    """Defer one `scan` job per row through the queue (jobcannon.host.tasks.
    app), deduped by a per-company queueing lock — mirrors the periodic
    tick's own dedup shape (jobcannon.host.tasks.enqueue_due_scans). Returns
    (enqueued, already_enqueued). Pure procrastinate-connector operation: no
    DB connection of its own, so it is unit-testable against an InMemory
    Connector swapped in via tasks.app.replace_connector, with no network."""
    from procrastinate import exceptions as procrastinate_exceptions

    from jobcannon.host import tasks

    enqueued = already = 0
    with tasks.app.open():
        for row in rows:
            try:
                tasks.scan.configure(queueing_lock=f"scan:{row['name']}").defer(
                    company_name=row["name"]
                )
                enqueued += 1
            except procrastinate_exceptions.AlreadyEnqueued:
                already += 1
    return enqueued, already


def _seed(rows: list[dict[str, str]]) -> int:
    from jobcannon.host.config import load_host_config
    from jobcannon.host.wiring import init_engine_seams, teardown_engine_seams

    host_config = load_host_config()
    init_engine_seams(host_config)
    try:
        landed = _upsert_companies(rows)
        enqueued, already = _enqueue_scans(landed)
    finally:
        teardown_engine_seams()

    log.info(
        "pre-seed complete: %d companies upserted, %d scans enqueued, %d already-enqueued",
        len(landed),
        enqueued,
        already,
    )
    if rows and not landed:
        log.error("pre-seed FAILED: every row was skipped — systemic DB failure, not bad rows")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path")
    parser.add_argument("--verify", action="store_true", help="GET-check board URLs; no writes")
    parser.add_argument("--limit", type=int, default=None, help="seed only the first N rows")
    args = parser.parse_args(argv)

    rows = _read_rows(args.csv_path)
    if args.limit is not None:
        rows = rows[: args.limit]

    if args.verify:
        return _verify(rows)
    return _seed(rows)


if __name__ == "__main__":
    raise SystemExit(main())
