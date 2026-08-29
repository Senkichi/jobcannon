"""Visual-review capture for the Living Journal adoption (plan Task 14).

Boots the real app against a throwaway Postgres database (same recipe as
tests/host), serves it on a loopback port, and screenshots the public routes
in BOTH themes via Playwright color-scheme emulation.

Run: uv run --no-sync --with playwright python scripts/design_review_shots.py --out <dir>
(prereq: uv run --no-sync --with playwright python -m playwright install chromium)
"""

from __future__ import annotations

import argparse
import logging
import os
import threading
import uuid
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo
from werkzeug.serving import make_server

log = logging.getLogger("design_review_shots")

ROUTES = {
    "demo": "/demo",
    "preview": "/preview",
    "picker": "/start",
    "privacy": "/privacy",
    "consent": "/consent",
    "not-found": "/this-page-does-not-exist",
}
SCHEMES = ("light", "dark")
PORT = 5017


def _boot_app(admin_dsn: str) -> tuple[object, str]:
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.web import create_app

    db_name = f"jobcannon_design_shots_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{db_name}"')
    dsn = make_conninfo(admin_dsn, dbname=db_name)
    run_migrations(dsn)
    pool_mod.open_pool(dsn)
    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda r: None,
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
        }
    )
    return app, db_name


def _teardown(admin_dsn: str, db_name: str) -> None:
    from jobcannon.db import pool as pool_mod

    pool_mod.close_pool()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="directory for PNGs")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    admin_dsn = os.environ["POSTGRES_ADMIN_DSN"]
    app, db_name = _boot_app(admin_dsn)
    server = make_server("127.0.0.1", PORT, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for scheme in SCHEMES:
                context = browser.new_context(
                    color_scheme=scheme, viewport={"width": 1280, "height": 900}
                )
                page = context.new_page()
                for name, route in ROUTES.items():
                    page.goto(f"http://127.0.0.1:{PORT}{route}", wait_until="networkidle")
                    # let draw-on animations resolve before capturing
                    page.wait_for_timeout(1600)
                    target = out / f"{name}-{scheme}.png"
                    page.screenshot(path=str(target), full_page=True)
                    log.info("captured %s", target)
                context.close()
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        _teardown(admin_dsn, db_name)
    log.info("done — %d screenshots in %s", len(ROUTES) * len(SCHEMES), out)


if __name__ == "__main__":
    main()
