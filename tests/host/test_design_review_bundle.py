"""scripts/design_review_shots.py — the derivation logic behind the fresh-eyes evidence bundle.

Exercised against a tiny throwaway Flask app, not the real one: what is under test is that the
unit list, the skip reasons and the template coverage are DERIVED (url_map, template_rendered,
Jinja's include graph) — so a new route or template is picked up with no edit to the script.
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify, redirect, render_template, request
from jinja2 import DictLoader

from scripts.design_review_shots import (
    SCENARIO_HEADER,
    Scenario,
    build_plan,
    enumerate_units,
    referenced_templates,
    unit_slug,
)

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
    plan = build_plan(app, {"thing_id": 7}, "http://127.0.0.1:5017", SCENARIOS)
    assert plan["app"] == "jobcannon"
    assert plan["base_url"] == "http://127.0.0.1:5017"
    assert [v["primary"] for v in plan["variants"]] == [True, False]
    assert plan["frozen_time"].endswith("Z")
    assert plan["units"] and plan["surfaces_declared"]
