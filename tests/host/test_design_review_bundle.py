"""scripts/design_review_shots.py — the derivation logic behind the fresh-eyes evidence bundle.

Exercised against a tiny throwaway Flask app, not the real one: what is under test is that the
unit list, the skip reasons and the template coverage are DERIVED (url_map, template_rendered,
Jinja's include graph) — so a new route or template is picked up with no edit to the script.
The runtime seams the bundle depends on are covered too: install_scenarios' header-switched
identity + handoff marker, seed()'s writes against a real migrated throwaway database
(requires_postgres — schema drift fails CI here, not inside the script), and main()'s
fail-closed exit-2 paths.
"""

from __future__ import annotations

import shutil
import sys

import pytest
from flask import Flask, jsonify, redirect, render_template, request
from jinja2 import DictLoader

from jobcannon.web.auth import ClerkIdentity
from jobcannon.web.handoff import _HANDOFF_DONE_KEY
from scripts.design_review_shots import (
    MEMBER_USER_ID,
    SCENARIO_HEADER,
    Scenario,
    build_plan,
    enumerate_units,
    install_scenarios,
    main,
    referenced_templates,
    seed,
    unit_slug,
)
from tests.host.conftest import create_throwaway_db, drop_throwaway_db, requires_postgres

TEMPLATES = {
    "base.html": "<!doctype html><html><body>{% block body %}{% endblock %}</body></html>",
    "_row.html": "<li>row</li>",
    "home.html": '{% extends "base.html" %}{% block body %}<ul>{% include "_row.html" %}</ul>'
    "{% if member %}<p>member</p>{% endif %}{% endblock %}",
    "about.html": '{% extends "base.html" %}{% block body %}<p>about</p>{% endblock %}',
    "thing.html": '{% extends "base.html" %}{% block body %}<p>thing {{ thing_id }}</p>{% endblock %}',
    "never.html": '{% extends "base.html" %}{% block body %}<p>unreachable</p>{% endblock %}',
    "404.html": '{% extends "base.html" %}{% block body %}<p>lost</p>{% endblock %}',
}
SCENARIOS = (
    Scenario("anon", "anonymous", {}),
    Scenario("member", "signed-in member", {SCENARIO_HEADER: "member"}),
)


@pytest.fixture
def app():
    app = Flask(__name__)
    app.jinja_env.loader = DictLoader(TEMPLATES)

    def _member() -> bool:
        return request.headers.get(SCENARIO_HEADER) == "member"

    @app.get("/")
    def home():
        return render_template("home.html", member=_member())

    @app.get("/about")
    def about():
        return render_template("about.html")

    @app.get("/account")
    def account():
        return render_template("about.html") if _member() else redirect("/")

    @app.get("/things/<int:thing_id>")
    def thing(thing_id):
        return render_template("thing.html", thing_id=thing_id)

    @app.get("/orphans/<int:orphan_id>")
    def orphan(orphan_id):
        return render_template("thing.html", thing_id=orphan_id)

    @app.get("/frag")
    def frag():
        return render_template("_row.html")

    @app.get("/api")
    def api():
        return jsonify(ok=True)

    @app.post("/submit")
    def submit():
        return "", 204

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("404.html"), 404

    return app


def _ids(result):
    return sorted(u["unit_id"] for u in result["units"])


def _skips(result):
    return {s["surface"]["id"]: s["reason"] for s in result["surfaces_skipped"]}


def test_unit_slug_is_a_safe_path_segment():
    assert unit_slug("/", "anon") == "root--anon"
    assert (
        unit_slug("/postings/<int:posting_id>/detail", "member")
        == "postings-int-posting-id-detail--member"
    )


def test_referenced_templates_follows_extends_and_include():
    env = Flask(__name__).jinja_env
    env.loader = DictLoader(TEMPLATES)
    assert referenced_templates(env, "home.html") == {"home.html", "base.html", "_row.html"}


def test_units_are_derived_from_the_url_map(app):
    result = enumerate_units(app, {"thing_id": 7}, SCENARIOS)
    assert _ids(result) == [
        "about--anon",  # member renders identical HTML -> deduplicated
        "account--member",  # anon is redirected; member is the only renderable state
        "not-found--anon",
        "root--anon",
        "root--member",  # differs from anon -> both kept
        "things-int-thing-id--anon",
    ]


def test_skips_carry_a_reason(app):
    skips = _skips(enumerate_units(app, {"thing_id": 7}, SCENARIOS))
    assert skips["/orphans/<int:orphan_id>"] == "unresolved_param:orphan_id"
    assert "fragment" in skips["/frag"]
    assert "non_html" in skips["/api"]
    assert "/submit" not in skips  # not a GET rule: not a surface at all
    assert "/account" not in skips  # rendered in one scenario: covered, not skipped


def test_template_coverage_includes_partials_and_exposes_the_unrendered(app):
    result = enumerate_units(app, {"thing_id": 7}, SCENARIOS)
    declared = {(s["kind"], s["id"]) for s in result["surfaces_declared"]}
    covered = {(c["kind"], c["id"]) for u in result["units"] for c in u["covers"]}
    assert ("template", "_row.html") in covered  # reached only through {% include %}
    assert ("template", "base.html") in covered  # reached only through {% extends %}
    assert ("error", "404") in covered
    assert ("template", "never.html") in declared - covered  # the coverage gap fresh-eyes reports
    assert covered <= declared


def test_units_carry_scenario_headers_and_a_source_hint(app):
    units = {u["unit_id"]: u for u in enumerate_units(app, {"thing_id": 7}, SCENARIOS)["units"]}
    assert units["root--member"]["headers"] == {SCENARIO_HEADER: "member"}
    assert units["things-int-thing-id--anon"]["path"] == "/things/7"
    assert units["things-int-thing-id--anon"]["source_hint"] == {
        "route": "/things/<int:thing_id>",
        "endpoint": "thing",
        "template": "thing.html",
    }
    assert all(u["content_provenance"] == "seeded" for u in units.values())


def test_build_plan_is_a_complete_capture_plan(app):
    seeded = {"thing_id": 7}
    plan = build_plan(app, seeded, "http://127.0.0.1:5017", SCENARIOS)
    assert set(plan) == {
        "app",
        "base_url",
        "renderer",
        "frozen_time",
        "settle_ms",
        "variants",
        "units",
        "surfaces_declared",
        "surfaces_skipped",
    }
    assert plan["app"] == "jobcannon"
    assert plan["base_url"] == "http://127.0.0.1:5017"
    # Literal contract values, not the module constants: a drift in RENDERER,
    # FROZEN_TIME, SETTLE_MS or VARIANTS is a bundle-format change the
    # fresh-eyes consumer must see fail here.
    assert plan["renderer"] == {"name": "jobcannon/scripts/design_review_shots.py", "version": "2"}
    assert plan["frozen_time"] == "2026-01-15T17:00:00Z"
    assert plan["settle_ms"] == 300
    assert plan["variants"] == [
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
    ]
    derived = enumerate_units(app, seeded, SCENARIOS)
    assert plan["units"] == derived["units"]
    assert plan["surfaces_declared"] == derived["surfaces_declared"]
    assert plan["surfaces_skipped"] == derived["surfaces_skipped"]


def test_install_scenarios_switches_identity_and_marks_handoff():
    """The header seam: `X-Design-Scenario: member` swaps in a ClerkIdentity for
    MEMBER_USER_ID and sets the handoff_done session marker; any other request
    stays anonymous and unmarked."""
    app = Flask(__name__)
    app.secret_key = "design-review-test"  # sessions need signing for the marker
    install_scenarios(app)

    @app.get("/")
    def home():
        return "ok"

    verify = app.config["VERIFY_REQUEST"]
    with app.test_request_context("/", headers={SCENARIO_HEADER: "member"}):
        identity = verify(request)
    assert isinstance(identity, ClerkIdentity)
    assert identity.user_id == MEMBER_USER_ID
    assert identity.claims == {"sub": MEMBER_USER_ID}

    with app.test_request_context("/"):
        assert verify(request) is None
    with app.test_request_context("/", headers={SCENARIO_HEADER: "not-a-scenario"}):
        assert verify(request) is None

    member_client = app.test_client()
    member_client.get("/", headers={SCENARIO_HEADER: "member"})
    with member_client.session_transaction() as sess:
        assert sess[_HANDOFF_DONE_KEY] is True

    anon_client = app.test_client()
    anon_client.get("/")
    with anon_client.session_transaction() as sess:
        assert _HANDOFF_DONE_KEY not in sess


@requires_postgres
def test_seed_returns_url_params_and_writes_member_rows():
    """seed() against a real migrated throwaway database: it must return the
    posting_id/dedup_key registry AND land the member user, profile and posting
    rows — schema drift fails CI here instead of inside the runtime script."""
    from jobcannon.db import pool as pool_mod
    from jobcannon.db.migrate import run_migrations
    from jobcannon.db.pool import connection_factory

    dsn, db_name = create_throwaway_db("jobcannon_design_seed")
    try:
        run_migrations(dsn)
        pool_mod.open_pool(dsn)
        seeded = seed()
        with connection_factory() as conn:
            member_user = conn.raw.execute(
                "SELECT id, plan_tier FROM users WHERE id = %s", (MEMBER_USER_ID,)
            ).fetchone()
            member_profile = conn.raw.execute(
                "SELECT user_id FROM profiles WHERE user_id = %s", (MEMBER_USER_ID,)
            ).fetchone()
            posting = conn.raw.execute(
                "SELECT id, dedup_key, company_id FROM postings WHERE id = %s",
                (seeded["posting_id"],),
            ).fetchone()
            company = (
                conn.raw.execute(
                    "SELECT id, name FROM companies WHERE id = %s", (posting["company_id"],)
                ).fetchone()
                if posting
                else None
            )
    finally:
        pool_mod.close_pool()
        drop_throwaway_db(db_name)

    assert set(seeded) == {"posting_id", "dedup_key"}
    assert seeded["dedup_key"] == "design-review-posting"
    assert seeded["posting_id"] is not None
    assert member_user is not None and member_user["plan_tier"] == "free"
    assert member_profile is not None and member_profile["user_id"] == MEMBER_USER_ID
    assert posting is not None and posting["dedup_key"] == "design-review-posting"
    assert company is not None and company["name"] == "Design Review Co"


def test_main_fails_closed_without_postgres_admin_dsn(monkeypatch, tmp_path):
    monkeypatch.delenv("POSTGRES_ADMIN_DSN", raising=False)
    monkeypatch.setattr(sys, "argv", ["design_review_shots", "--bundle", str(tmp_path)])
    assert main() == 2


def test_main_fails_closed_without_fresh_eyes_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("POSTGRES_ADMIN_DSN", "postgresql://unused.invalid/db")
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(sys, "argv", ["design_review_shots", "--bundle", str(tmp_path)])
    assert main() == 2
