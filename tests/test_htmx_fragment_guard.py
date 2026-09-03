# PORTED from tests/test_htmx_fragment_guard.py @ b1aa40239b89327d2d0abdf38dd5cfd4f53698e3 (private job-cannon). Ledger L-0513.
"""Tests for the @htmx_fragment guard — single-point HX-Request enforcement.

Fragment routes render a bare partial (no base.html), so a direct browser hit
must redirect to the parent page instead of surfacing an unstyled orphan. The
guard used to be a hand-copied ``if not request.headers.get("HX-Request")``
idiom present on some fragment routes and silently missing from ~15 others;
``@htmx_fragment`` makes it a single, marker-introspectable enforcement point.
"""

from __future__ import annotations

import pytest
from flask import Flask, url_for

from jobcannon.web._htmx import htmx_fragment

# ---------------------------------------------------------------------------
# Decorator unit tests (isolated minimal app — no project wiring)
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_app() -> Flask:
    app = Flask(__name__)

    @app.route("/home")
    def home():
        return "HOME"

    @app.route("/frag")
    @htmx_fragment("home")
    def frag():
        return "FRAGMENT"

    return app


def test_decorator_redirects_without_hx_request(mini_app):
    resp = mini_app.test_client().get("/frag")
    assert resp.status_code == 302
    assert "/home" in resp.headers["Location"]


def test_decorator_passes_through_with_hx_request(mini_app):
    resp = mini_app.test_client().get("/frag", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert resp.data == b"FRAGMENT"


def test_decorator_sets_introspection_marker(mini_app):
    view = mini_app.view_functions["frag"]
    assert getattr(view, "_is_htmx_fragment", False) is True
    assert view._htmx_redirect_to == "home"


# PORT-SEAM: dropped test_fragment_routes_are_discovered and
# test_every_fragment_route_redirects_without_hx_request (+ _arg_value,
# _guarded_rules helpers) -- both walk the real app's client/url_map fixture,
# a private single-user-app surface with no public equivalent here.
