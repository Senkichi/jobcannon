"""Flask app factory for the hosted skeleton (Wave 1: health + auth + webhooks;
Wave 2 PR 8 adds per-request consent resolution for the log_event chokepoint)."""

from __future__ import annotations

import logging
import os

from flask import Flask, abort, g, request

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/demo"})


def _resolve_consent(identity) -> bool:
    """One DB read per authenticated request, resolving the log_event
    chokepoint's consent gate (jobcannon/host/events.py) up front so route
    handlers never have to think about it.

    Fail-closed on any error (missing/unopened connection pool — e.g. a
    TESTING config that never calls init_engine_seams, same as
    tests/host/test_auth.py's lightweight identity-only tests — or a genuine
    DB outage): defaulting to "no consent" is the privacy-safe direction and
    must never turn into a 500 on an otherwise-successful request.
    """
    from jobcannon.db import _events
    from jobcannon.db.pool import connection_factory

    try:
        with connection_factory() as conn:
            return _events.read_consent_state(conn.raw, identity.user_id)
    except Exception:
        logger.warning(
            "consent lookup failed for user %s (defaulting to no consent)",
            identity.user_id,
            exc_info=True,
        )
        return False


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
            g.consent_granted = False
            return None
        identity = app.config["VERIFY_REQUEST"](request)
        # Set g.clerk_user BEFORE the possible abort(401): any error handler
        # that reads g.clerk_user must never see it unset on the 401 path.
        g.clerk_user = identity
        if identity is None:
            abort(401)
        g.consent_granted = _resolve_consent(identity)
        return None

    from jobcannon.web.webhooks import webhooks_bp

    app.register_blueprint(webhooks_bp)

    from jobcannon.web.pages import pages_bp

    app.register_blueprint(pages_bp)
    return app
