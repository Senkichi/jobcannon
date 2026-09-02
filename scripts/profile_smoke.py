"""Live-browser smoke of /profile (Spec 2, issue #262 / PR #275).

Boots the real app against a throwaway Postgres database (same recipe as
scripts/design_review_shots.py), authed via the VERIFY_REQUEST seam with
REAL CSRF enabled, and drives the save flow in headless Chromium: consent
gate -> blank form -> fill -> save -> PRG ?saved=1 stamp -> reload
persistence -> direct DB row check -> authed /start redirect. Screenshots
land in --out. Exits non-zero on the first failed check.

This deliberately overlaps tests/host/test_profile_route.py from the other
side: those tests monkeypatch the DAL (no Postgres, no browser), so this
script is the only place the full stack — real server, real htmx/CSRF in a
real browser, real replace_profile writes — is exercised end to end.

Run: uv run --no-sync --with playwright python scripts/profile_smoke.py --out <dir>
(prereq: uv run --no-sync --with playwright python -m playwright install chromium)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from werkzeug.serving import make_server

log = logging.getLogger("profile_smoke")
PORT = 5019
USER_ID = "user_live_smoke_1"


def check(name: str, cond: bool, detail: str | None = "") -> None:
    log.info("%s  %s  %s", "PASS" if cond else "FAIL", name, detail or "")
    if not cond:
        raise AssertionError(name)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="directory for PNGs")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    from jobcannon.db import pool as pool_mod
    from jobcannon.db._profiles import get_profile
    from jobcannon.db._users import ensure_user
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app
    from jobcannon.web.auth import ClerkIdentity

    admin_dsn = os.environ["POSTGRES_ADMIN_DSN"]
    db_name = f"jobcannon_profile_smoke_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    dsn = make_conninfo(admin_dsn, dbname=db_name)
    exit_code = 1
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        with pool_mod.connection_factory() as conn:
            ensure_user(conn, USER_ID)
        app = create_app(
            config={
                "TESTING": True,
                "WTF_CSRF_ENABLED": True,  # real token through the real browser
                "VERIFY_REQUEST": lambda r: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID}),
                "WEBHOOK_SECRET": "whsec_dGVzdA==",
            }
        )
        server = make_server("127.0.0.1", PORT, app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            from playwright.sync_api import sync_playwright

            base = f"http://127.0.0.1:{PORT}"
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_context(viewport={"width": 1280, "height": 1200}).new_page()

                # 0. Consent gate: a fresh authed user is redirected to
                # /consent before any gated page. Answer it in-browser
                # (htmx in-place panel swap), like a real first visit.
                page.goto(f"{base}/profile", wait_until="networkidle")
                if "/consent" in page.url:
                    check("consent gate intercepted first visit", True, page.url)
                    page.screenshot(path=str(out / "0-consent.png"), full_page=True)
                    page.click('button[name="choice"][value="grant"]')
                    page.wait_for_timeout(800)  # htmx swap settles

                # 1. Blank authed view
                page.goto(f"{base}/profile", wait_until="networkidle")
                check("/profile reachable after consent", page.url.endswith("/profile"), page.url)
                check(
                    "nav Profile link rendered",
                    page.locator("[data-profile-nav-link]").count() == 1,
                )
                check("form present", page.locator('form[action="/profile"]').count() == 1)
                check(
                    "csrf token non-empty",
                    bool(page.locator('input[name="csrf_token"]').get_attribute("value")),
                )
                check(
                    "no saved stamp on first load",
                    page.locator("[data-profile-saved]").count() == 0,
                )
                page.screenshot(path=str(out / "1-blank.png"), full_page=True)

                # 2. Fill and save. Form vocabulary is lowercase ("remote");
                # the DB row stores the filter vocabulary ("REMOTE") — the
                # mapping is replace_profile's caller's job and is asserted
                # from both sides below.
                page.fill('textarea[name="target_titles"]', "Staff Engineer\nPrincipal Engineer")
                page.fill('textarea[name="target_companies"]', "Acme")
                page.fill('textarea[name="target_locations"]', "Seattle, WA")
                first_skill = page.locator('input[name="skills"]').first
                skill_value = first_skill.get_attribute("value")
                first_skill.check()
                page.select_option('select[name="seniority_level"]', index=1)
                seniority = page.locator('select[name="seniority_level"]').input_value()
                page.fill('input[name="years_of_experience"]', "12.5")
                page.fill('input[name="comp_floor_usd"]', "180000")
                page.select_option('select[name="workplace_type"]', "remote")
                page.fill('textarea[name="experience_summary"]', "Twelve years.\nMostly backend.")
                page.screenshot(path=str(out / "2-filled.png"), full_page=True)
                page.click('button[type="submit"]')
                page.wait_for_load_state("networkidle")
                check("PRG landed on ?saved=1", page.url.endswith("/profile?saved=1"), page.url)
                check("saved stamp visible", page.locator("[data-profile-saved]").count() == 1)
                page.screenshot(path=str(out / "3-saved.png"), full_page=True)

                # 3. Reload: values persisted through the real DB
                page.goto(f"{base}/profile", wait_until="networkidle")
                check(
                    "titles persisted",
                    page.locator('textarea[name="target_titles"]').input_value()
                    == "Staff Engineer\nPrincipal Engineer",
                )
                check(
                    "skill checkbox persisted",
                    page.locator(f'input[name="skills"][value="{skill_value}"]').is_checked(),
                    skill_value,
                )
                check(
                    "workplace persisted",
                    page.locator('select[name="workplace_type"]').input_value() == "remote",
                )
                check(
                    "years persisted",
                    page.locator('input[name="years_of_experience"]').input_value() == "12.5",
                )
                page.screenshot(path=str(out / "4-reloaded.png"), full_page=True)

                # 4. DB row matches what the browser saved
                with pool_mod.connection_factory() as conn:
                    row = get_profile(conn, USER_ID)
                check("db row exists", row is not None)
                assert row is not None  # narrow for the checks below
                check("db titles", row["target_titles"] == ["Staff Engineer", "Principal Engineer"])
                check("db skills", row["skills"] == [skill_value])
                check("db seniority", row["seniority_level"] == seniority, seniority)
                check("db comp floor", row["comp_floor_usd"] == 180000)
                check("db workplace", row["workplace_type"] == "REMOTE")

                # 5. Authed /start redirects to /profile (Spec 2, issue #262)
                page.goto(f"{base}/start", wait_until="networkidle")
                check(
                    "authed /start redirected",
                    page.url.rstrip("/").endswith("/profile"),
                    page.url,
                )

                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
        exit_code = 0
    finally:
        try:
            pool_mod.close_pool()
        except Exception:
            log.warning("pool close failed during teardown", exc_info=True)
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
