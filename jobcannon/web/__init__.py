"""Flask app factory for the hosted skeleton (Wave 1: health + auth + webhooks;
Wave 2 PR 8 adds per-request consent resolution for the log_event chokepoint;
adds the anonymous session carrier (jobcannon.web.anon_session),
Flask session signing (SECRET_KEY), and the HOST_CONFIG accessor; Phase 1C
adds the anon-to-authed handoff (jobcannon.web.handoff) and the consent
surface it can redirect to (jobcannon.web.consent); adds an HTML body for
401 responses via the errorhandler below, replacing Werkzeug's default
plain-text body; adds the authed save/dismiss/apply mutation routes
(jobcannon.web.actions); adds the authed, read-only self-service data-export
route (jobcannon.web.export) and the self-service account-deletion trigger
(jobcannon.web.account), sharing one Clerk SDK client between the JWT
verifier and that route's user-delete management call; adds the public,
scaffold-only /privacy placeholder (jobcannon.web.privacy, issue #94) —
ships the mechanism and a clearly-marked placeholder page, not the
policy text)."""

from __future__ import annotations

import logging
import os

from flask import Flask, abort, current_app, g, render_template, request

from jobcannon.web.anon_session import capture_attribution, ensure_session_ids
from jobcannon.web.handoff import run_handoff_if_pending

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/demo", "/start", "/preview", "/privacy"})


def _resolve_consent(identity) -> bool:
    """One DB read per authenticated request, resolving the log_event
    chokepoint's consent gate (jobcannon/host/events.py) up front so route
    handlers never have to think about it.

    Reads jobcannon.web.consent.CONSENT_VERSION fresh on every call (a local
    import, not a module-level one) so a grant recorded at a stale version
    is re-evaluated against the CURRENT version on every request — a version
    bump takes effect immediately, with no user action and no cache to
    invalidate.

    Fail-closed on any error (missing/unopened connection pool — e.g. a
    TESTING config that never calls init_engine_seams, same as
    tests/host/test_auth.py's lightweight identity-only tests — or a genuine
    DB outage): defaulting to "no consent" is the privacy-safe direction and
    must never turn into a 500 on an otherwise-successful request.
    """
    from jobcannon.db import _events
    from jobcannon.db.pool import connection_factory
    from jobcannon.web.consent import CONSENT_VERSION

    try:
        with connection_factory() as conn:
            return _events.read_consent_state(
                conn.raw, identity.user_id, current_version=CONSENT_VERSION
            )
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
        # Deployed processes get INFO-level logging on stderr, mirroring
        # jobcannon/worker/__main__.py. Without this only WARNING+ escapes
        # via Python's lastResort handler — which hid the pool's boot-time
        # "pinned DB hostaddr" INFO line (and every other INFO breadcrumb)
        # from platform logs during the 2026-08-26 incident. Placed BEFORE
        # init_engine_seams so pool-open logging is already visible.
        # basicConfig is a no-op when the root logger has handlers already.
        logging.basicConfig(level=os.environ.get("JC_LOG_LEVEL", "INFO"))
        from jobcannon.host import init_engine_seams, load_host_config

        host_config = app.config.get("HOST_CONFIG") or load_host_config()
        init_engine_seams(host_config)  # unchanged: the ONE wiring site, non-TESTING only
    else:
        from jobcannon.host.config import HostConfig

        host_config = app.config.get("HOST_CONFIG") or HostConfig(
            database_url="",  # tests open the pool with their own throwaway DSN
            secret_key="testing-secret-key",
            clerk_sign_up_url="https://clerk.test/sign-up",
            signup_wave="0",
        )
    app.config["HOST_CONFIG"] = host_config  # ALWAYS set, both branches

    if "WEBHOOK_SECRET" in app.config:
        secret = app.config["WEBHOOK_SECRET"]
    else:
        secret = host_config.clerk_webhook_signing_secret
    app.config["WEBHOOK_SECRET"] = secret
    if not app.config.get("TESTING") and not secret:
        # Fail fast at startup (mirrors load_host_config's DATABASE_URL
        # fail-fast): an unset OR blank secret (whether injected via config
        # or left to the env-var default) must never surface as per-request
        # 500s / invalid-signature noise instead of a clear boot failure.
        raise RuntimeError("CLERK_WEBHOOK_SIGNING_SECRET is required (Svix webhook signing secret)")

    # Flask session signing key (jobcannon.web.anon_session's cookie carrier
    # needs this). Same injectable-config / fail-fast shape as WEBHOOK_SECRET
    # above, kept AFTER it so an unset webhook secret still raises with that
    # message first (test_create_app_raises_on_missing_webhook_secret pins
    # this ordering).
    secret_key = app.config.get("SECRET_KEY") or getattr(host_config, "secret_key", "")
    if not app.config.get("TESTING") and not secret_key:
        raise RuntimeError("JC_SECRET_KEY is required (Flask session signing key)")
    app.config["SECRET_KEY"] = secret_key
    # Secure by default, relaxed for both test and local-dev runs. Keyed off
    # testing/debug rather than TESTING alone: a plain `flask run` /
    # `python -m jobcannon` over http://localhost with TESTING unset would
    # otherwise have the browser silently drop the session cookie (secure
    # cookies are dropped over plain HTTP) — a defect with no error anywhere,
    # that no test here would catch (the Werkzeug test client sends secure
    # cookies over plain HTTP regardless).
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = not (app.testing or app.debug)

    verify = app.config.get("VERIFY_REQUEST")
    clerk_client = app.config.get("CLERK_CLIENT")
    if verify is None and not app.config.get("TESTING"):
        from jobcannon.web.auth import build_clerk_client, build_clerk_verifier

        # One client, shared: built once here and handed to the verifier
        # below, then stashed on app.config so jobcannon.web.account's
        # user-delete management call reuses the same credentialed instance
        # instead of constructing a second one.
        clerk_client = clerk_client or build_clerk_client(host_config)
        verify = build_clerk_verifier(host_config, client=clerk_client)
        app.config["VERIFY_REQUEST"] = verify
    app.config["CLERK_CLIENT"] = clerk_client

    @app.get("/healthz")
    def healthz():
        """Instance health for the platform's health checks (render.yaml
        healthCheckPath). DB-aware by design — 2026-08-26 incident: the web
        instance's DB path died post-boot while the process kept serving,
        and a static healthz kept the wedged instance in rotation
        indefinitely. A bounded pooled probe turns that state into a 503 so
        the platform replaces the instance, and the failure log carries the
        exception + pool stats the incident diagnosis had to go without.

        SELECT 1 needs no schema, so first-boot ordering still holds: the
        web service goes healthy as soon as the DATABASE accepts
        connections, independent of the worker's migration authority. With
        no pool opened (tests, DB-free local runs) this stays the static
        OK it always was.

        The probe itself is db.pool.probe_pool — the SAME bounded probe the
        pool watchdog runs, so route health, platform health, and the
        watchdog's recycle decision all share one definition of "the DB
        answers SELECT 1 within 2.5 s wall-clock". Why the bound is a
        daemon-thread join and not the pool's timeout= parameter is
        documented on probe_pool; the trade-off (a hung probe thread
        strands its pooled connection, bounded in aggregate) is accepted
        because a 503-ing instance is being replaced by the platform
        anyway, and the watchdog recycles a wedged pool underneath us.
        """
        from jobcannon.db import pool as db_pool

        if not db_pool.is_open():
            return {"status": "ok", "db": "not-configured"}
        detail = db_pool.probe_pool()
        if detail is None:
            return {"status": "ok", "db": "ok"}
        try:
            stats = db_pool.get_pool().get_stats()
        except Exception:
            stats = {}
        logger.warning("healthz DB probe failed: %s (pool stats: %s)", detail, stats)
        return {"status": "unhealthy", "db": "unreachable"}, 503

    @app.errorhandler(401)
    def unauthorized(_error):
        """HTML body for every 401 in this app, not only an authed-route
        sign-in prompt: `clerk_auth` below runs before Flask resolves
        routing, so this handler also renders for `/static/<path>` (Flask
        registers that rule unconditionally, and it is not in
        PUBLIC_PATHS) and for any unmatched path, both of which would
        otherwise 404 — they hit this 401 handler first instead. That is
        deliberate, not a routing bug to "fix" into a distinct 404 page.
        The status code stays 401.

        Reads clerk_sign_up_url through the one HOST_CONFIG accessor
        (app.config["HOST_CONFIG"], set on every create_app code path
        above, including TESTING) rather than a second os.environ read.
        getattr tolerates a test double that only carries the attributes a
        given test cares about; the template branches on an empty value
        rather than rendering href="".
        """
        clerk_sign_up_url = getattr(current_app.config["HOST_CONFIG"], "clerk_sign_up_url", "")
        return render_template("error_401.html", clerk_sign_up_url=clerk_sign_up_url), 401

    @app.before_request
    def clerk_auth():
        # before_request runs for EVERY request regardless of routing
        # outcome — Flask raises the routing exception later, in dispatch —
        # so an unmatched path still hits this gate and 401s before it ever
        # gets a chance to 404 (deliberate fail-closed). URL-rule matching
        # HAS already run by this point for matched routes, though, which is
        # what the request.blueprint == "webhooks" exemption relies on. The
        # public-path exemption rests solely on the normalized-path
        # membership check below: request.path is compared with a trailing
        # slash stripped (falling back to "/") because /demo is registered
        # strict_slashes=False, so both /demo and /demo/ must match.
        if (request.path.rstrip("/") or "/") in PUBLIC_PATHS or request.blueprint == "webhooks":
            g.clerk_user = None
            g.consent_granted = False
            ensure_session_ids()
            capture_attribution()
            return None
        identity = app.config["VERIFY_REQUEST"](request)
        # Set g.clerk_user BEFORE the possible abort(401): any error handler
        # that reads g.clerk_user must never see it unset on the 401 path.
        g.clerk_user = identity
        if identity is None:
            abort(401)
        g.consent_granted = _resolve_consent(identity)
        ensure_session_ids()
        capture_attribution()
        # Must run after ensure_session_ids(): the handoff's user_signed_up
        # emission reads g.feed_session_id, which that call populates.
        handoff_response = run_handoff_if_pending()
        if handoff_response is not None:
            return handoff_response
        return None

    from jobcannon.web.webhooks import webhooks_bp

    app.register_blueprint(webhooks_bp)

    from jobcannon.web.pages import pages_bp

    app.register_blueprint(pages_bp)

    from jobcannon.web.onboarding import onboarding_bp

    app.register_blueprint(onboarding_bp)

    from jobcannon.web.consent import consent_bp

    app.register_blueprint(consent_bp)

    from jobcannon.web.actions import actions_bp

    app.register_blueprint(actions_bp)

    from jobcannon.web.account import account_bp
    from jobcannon.web.export import export_bp

    app.register_blueprint(account_bp)
    app.register_blueprint(export_bp)

    from jobcannon.web.privacy import privacy_bp

    app.register_blueprint(privacy_bp)
    return app
