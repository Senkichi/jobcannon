"""Flask app factory for the hosted skeleton (Wave 1: health + auth + webhooks)."""

from __future__ import annotations

import os

from flask import Flask, abort, g, request

PUBLIC_PATHS = frozenset({"/healthz"})


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(config or {})

    if not app.config.get("TESTING"):
        from jobcannon.host import init_engine_seams, load_host_config

        init_engine_seams(load_host_config())

    if "WEBHOOK_SECRET" in app.config:
        secret = app.config["WEBHOOK_SECRET"]
    else:
        secret = os.environ.get("CLERK_WEBHOOK_SIGNING_SECRET", "")
    app.config["WEBHOOK_SECRET"] = secret
    if not app.config.get("TESTING") and not secret:
        # Fail fast at startup (mirrors load_host_config's DATABASE_URL
        # fail-fast): an unset OR blank secret (whether injected via config
        # or left to the env-var default) must never surface as per-request
        # 500s / invalid-signature noise instead of a clear boot failure.
        raise RuntimeError("CLERK_WEBHOOK_SIGNING_SECRET is required (Svix webhook signing secret)")

    verify = app.config.get("VERIFY_REQUEST")
    if verify is None and not app.config.get("TESTING"):
        from jobcannon.web.auth import build_clerk_verifier

        verify = build_clerk_verifier()
        app.config["VERIFY_REQUEST"] = verify

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.before_request
    def clerk_auth():
        # Routing-based exemption: Flask resolves routing before
        # before_request runs, so request.blueprint is already populated for
        # any matched route (a 404 never reaches before_request at all). A
        # string-prefix check on request.path is comparatively fragile —
        # e.g. it would also exempt an unrelated /webhooks/-prefixed route
        # registered outside this blueprint.
        if request.path in PUBLIC_PATHS or request.blueprint == "webhooks":
            g.clerk_user = None
            return None
        identity = app.config["VERIFY_REQUEST"](request)
        # Set g.clerk_user BEFORE the possible abort(401): any error handler
        # that reads g.clerk_user must never see it unset on the 401 path.
        g.clerk_user = identity
        if identity is None:
            abort(401)
        return None

    from jobcannon.web.webhooks import webhooks_bp

    app.register_blueprint(webhooks_bp)
    return app
