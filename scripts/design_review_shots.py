"""Render every reachable GET page into a fresh-eyes evidence bundle.

    uv run --no-sync python -m scripts.design_review_shots --bundle <dir>

What to render is DERIVED, never listed: GET rules come from `app.url_map`, templates from the
Jinja loader, template coverage from Flask's `template_rendered` signal plus Jinja's own
extends/include graph. A route whose URL parameters the seed data cannot resolve, or that
redirects / errors / returns a fragment in every scenario, is reported as a skipped surface, so
the gap is visible instead of silent. Adding a route or a template needs no edit here.

Boots the app against a THROWAWAY Postgres database (created, migrated, seeded and dropped
here), serves it on loopback, then hands a capture plan to `fresh-eyes capture-web`, which owns
the browser. This script never imports fresh_eyes: the bundle format is the only contract.

The signed-in scenario is switched by a request header that only THIS process's app instance
honours (VERIFY_REQUEST test seam, loopback, throwaway database). Nothing here ships.

Requires POSTGRES_ADMIN_DSN, and the `fresh-eyes` CLI on PATH:
    uv tool install --editable <path-to>/fresh-eyes --with "playwright>=1.45" ; fresh-eyes web-setup
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from flask import template_rendered
from jinja2 import TemplateNotFound, meta
from psycopg.conninfo import make_conninfo
from werkzeug.serving import make_server

log = logging.getLogger("design_review_shots")

PORT = 5017
FROZEN_TIME = "2026-01-15T17:00:00Z"
SETTLE_MS = 300
SCENARIO_HEADER = "X-Design-Scenario"
MEMBER_USER_ID = "design_review_member"
NOT_FOUND_PATH = "/__fresh_eyes_not_a_route__"
RENDERER = {"name": "jobcannon/scripts/design_review_shots.py", "version": "2"}
VARIANTS = (
    {
        "name": "light-desktop",
        "primary": True,
        "color_scheme": "light",
        "viewport_width": 1280,
        "viewport_height": 900,
    },
    {
        "name": "dark-desktop",
        "primary": False,
        "color_scheme": "dark",
        "viewport_width": 1280,
        "viewport_height": 900,
    },
)


@dataclass(frozen=True)
class Scenario:
    name: str
    label: str
    headers: dict[str, str] = field(default_factory=dict)


SCENARIOS = (
    Scenario("anon", "anonymous"),
    Scenario("member", "signed-in member", {SCENARIO_HEADER: "member"}),
)


@dataclass(frozen=True)
class Probe:
    status: int
    location: str
    content_type: str
    body: bytes
    templates: tuple[str, ...]


def _boot_app(admin_dsn: str):
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


def unit_slug(rule: str, scenario: str) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", rule.lower()).strip("-") or "root"
    return f"{stem}--{scenario}"


def referenced_templates(env: Any, name: str) -> set[str]:
    """`name` plus everything it extends / includes / imports, transitively."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            source = env.loader.get_source(env, current)[0]
        except TemplateNotFound:
            continue
        stack.extend(ref for ref in meta.find_referenced_templates(env.parse(source)) if ref)
    return seen


def _probe(app: Any, path: str, scenario: Scenario) -> Probe:
    rendered: list[str] = []

    def _on_render(_sender: Any, template: Any, **_extra: Any) -> None:
        if template.name:
            rendered.append(template.name)

    with template_rendered.connected_to(_on_render, app):
        resp = app.test_client().get(
            path, headers=scenario.headers
        )  # fresh client: no cookie bleed
    return Probe(
        status=resp.status_code,
        location=resp.headers.get("Location", ""),
        content_type=resp.headers.get("Content-Type", ""),
        body=resp.get_data(),
        templates=tuple(rendered),
    )


def _skip_reason(probe: Probe, expect_status: int) -> str | None:
    if 300 <= probe.status < 400:
        return f"redirect -> {probe.location}"
    if probe.status != expect_status:
        return f"http {probe.status}"
    if not probe.content_type.startswith("text/html"):
        return "non_html"
    if b"<html" not in probe.body[:2048].lower():
        return "fragment"
    return None


def _unit(
    app: Any,
    surface: dict,
    unit_id: str,
    title: str,
    path: str,
    scenario: Scenario,
    probe: Probe,
    hint: dict[str, str],
) -> dict:
    templates = sorted(
        {t for name in probe.templates for t in referenced_templates(app.jinja_env, name)}
    )
    if probe.templates:
        hint = {**hint, "template": probe.templates[0]}
    return {
        "unit_id": unit_id,
        "title": title,
        "reached_by": f"GET {path} as {scenario.label}",
        "path": path,
        "headers": dict(scenario.headers),
        "content_provenance": "seeded",
        "covers": [surface, *({"kind": "template", "id": t} for t in templates)],
        "source_hint": hint,
    }


def enumerate_units(
    app: Any, seeded: Mapping[str, Any], scenarios: Sequence[Scenario] = SCENARIOS
) -> dict:
    units: list[dict] = []
    skipped: list[dict] = []
    declared: list[dict] = []

    rules = sorted(
        (
            r
            for r in app.url_map.iter_rules()
            if "GET" in (r.methods or ()) and r.endpoint.rsplit(".", 1)[-1] != "static"
        ),
        key=lambda r: r.rule,
    )
    for rule in rules:
        surface = {"kind": "route", "id": rule.rule}
        declared.append(surface)
        needed = sorted(rule.arguments - set(rule.defaults or {}))
        missing = [a for a in needed if a not in seeded]
        if missing:
            skipped.append({"surface": surface, "reason": "unresolved_param:" + ",".join(missing)})
            continue
        built = rule.build({a: seeded[a] for a in needed}, append_unknown=False)
        path = built[1] if built else rule.rule
        reasons: list[str] = []
        bodies: list[bytes] = []
        for scenario in scenarios:
            probe = _probe(app, path, scenario)
            reason = _skip_reason(probe, 200)
            if reason:
                reasons.append(f"{scenario.name}: {reason}")
                continue
            if probe.body in bodies:
                continue  # identical to a scenario already kept: one unit is enough
            bodies.append(probe.body)
            units.append(
                _unit(
                    app,
                    surface,
                    unit_slug(rule.rule, scenario.name),
                    f"{rule.rule} ({scenario.label})",
                    path,
                    scenario,
                    probe,
                    {"route": rule.rule, "endpoint": rule.endpoint},
                )
            )
        if not bodies:
            skipped.append({"surface": surface, "reason": "; ".join(reasons)})

    error_surface = {"kind": "error", "id": "404"}
    declared.append(error_surface)
    probe = _probe(app, NOT_FOUND_PATH, scenarios[0])
    reason = _skip_reason(probe, 404)
    if reason:
        skipped.append({"surface": error_surface, "reason": f"{scenarios[0].name}: {reason}"})
    else:
        units.append(
            _unit(
                app,
                error_surface,
                f"not-found--{scenarios[0].name}",
                f"404 page ({scenarios[0].label})",
                NOT_FOUND_PATH,
                scenarios[0],
                probe,
                {"route": "<404>"},
            )
        )

    declared.extend(
        {"kind": "template", "id": name}
        for name in sorted(app.jinja_env.list_templates(extensions=["html"]))
    )
    return {"units": units, "surfaces_declared": declared, "surfaces_skipped": skipped}


def build_plan(
    app: Any, seeded: Mapping[str, Any], base_url: str, scenarios: Sequence[Scenario] = SCENARIOS
) -> dict:
    return {
        "app": "jobcannon",
        "base_url": base_url,
        "renderer": RENDERER,
        "frozen_time": FROZEN_TIME,
        "settle_ms": SETTLE_MS,
        "variants": list(VARIANTS),
        **enumerate_units(app, seeded, scenarios),
    }


def install_scenarios(app: Any) -> None:
    """Header-switched identity for THIS process's app instance only (see module docstring)."""
    from flask import request, session

    from jobcannon.web.auth import ClerkIdentity
    from jobcannon.web.handoff import _HANDOFF_DONE_KEY

    def _is_member(req: Any) -> bool:
        return req.headers.get(SCENARIO_HEADER) == "member"

    def verify(req: Any) -> Any:
        if _is_member(req):
            return ClerkIdentity(user_id=MEMBER_USER_ID, claims={"sub": MEMBER_USER_ID})
        return None

    def mark_handoff_done() -> None:
        if _is_member(request):
            session[_HANDOFF_DONE_KEY] = True

    app.config["VERIFY_REQUEST"] = verify
    app.before_request_funcs.setdefault(None, []).insert(0, mark_handoff_done)


def _first(row: Any) -> Any:
    return row["id"] if isinstance(row, Mapping) else row[0]


def seed() -> dict[str, Any]:
    """Seed the throwaway database; return the URL-parameter registry (arg name -> value)."""
    from jobcannon.db._profiles import upsert_profile
    from jobcannon.db.pool import commit_unless_nested, connection_factory
    from scripts import seed_guest_demo as guest

    with connection_factory() as conn:
        guest.seed(conn)
        raw = conn.raw if hasattr(conn, "raw") else conn
        raw.execute(
            "INSERT INTO users (id, plan_tier) VALUES (%s, 'free') ON CONFLICT (id) DO NOTHING",
            (MEMBER_USER_ID,),
        )
        commit_unless_nested(raw)
        upsert_profile(
            conn,
            MEMBER_USER_ID,
            skills=guest._GUEST_SKILLS,
            experience_summary=guest._GUEST_EXPERIENCE_SUMMARY,
            target_titles=guest._GUEST_TARGET_TITLES,
            target_locations=guest._GUEST_TARGET_LOCATIONS,
            seniority_level=guest._GUEST_SENIORITY_LEVEL,
            years_of_experience=guest._GUEST_YEARS_OF_EXPERIENCE,
            workplace_type=None,
        )
        company_id = _first(
            raw.execute(
                "INSERT INTO companies (name) VALUES (%s) RETURNING id", ("Design Review Co",)
            ).fetchone()
        )
        dedup_key = "design-review-posting"
        posting_id = _first(
            raw.execute(
                "INSERT INTO postings (dedup_key, company_id, title, company, jd_full) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (
                    dedup_key,
                    company_id,
                    "Senior Product Data Scientist",
                    "Design Review Co",
                    "Own experimentation for the growth team: design tests, read them honestly, and "
                    "turn the result into a shipped recommendation.",
                ),
            ).fetchone()
        )
        commit_unless_nested(raw)
    return {"posting_id": posting_id, "dedup_key": dedup_key}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bundle", required=True, type=Path, help="bundle directory to write")
    args = parser.parse_args()

    admin_dsn = os.environ.get("POSTGRES_ADMIN_DSN")
    if not admin_dsn:
        log.error("POSTGRES_ADMIN_DSN is required")
        return 2
    fresh_eyes = shutil.which("fresh-eyes")
    if fresh_eyes is None:
        log.error("`fresh-eyes` is not on PATH (see this module's docstring)")
        return 2

    app, db_name = _boot_app(admin_dsn)
    try:
        install_scenarios(app)
        plan = build_plan(app, seed(), f"http://127.0.0.1:{PORT}")
        log.info("%d units, %d surfaces skipped", len(plan["units"]), len(plan["surfaces_skipped"]))
        if not plan["units"]:
            log.error("nothing renderable: refusing to write an empty bundle")
            return 2
        server = make_server("127.0.0.1", PORT, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="jc-design-plan-") as tmp:
                plan_path = Path(tmp) / "plan.json"
                plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
                done = subprocess.run(
                    [
                        fresh_eyes,
                        "capture-web",
                        "--plan",
                        str(plan_path),
                        "--out",
                        str(args.bundle),
                    ],
                    check=False,
                )
                return done.returncode
        finally:
            server.shutdown()
    finally:
        _teardown(admin_dsn, db_name)


if __name__ == "__main__":
    raise SystemExit(main())
