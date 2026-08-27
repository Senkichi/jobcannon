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
verifier and that route's user-delete management call; adds the public
/privacy and /terms routes (jobcannon.web.legal, issue #94) serving the
ratified privacy policy and terms of service, rendered once at import
time from committed markdown under jobcannon/web/legal/ and gated by
jobcannon.web.legal_guard against leftover drafting matter; adds a
context processor exposing two derived, non-secret footer values —
source_url/source_sha_short, pinning the AGPL Corresponding Source link
to the deployed RENDER_GIT_COMMIT — to every template render, the same
way Flask itself auto-injects g/request/session, rather than the whole
HOST_CONFIG object (which also carries Clerk/webhook secrets no template
should be able to reach) or a per-route kwarg repeated on every one of
base.html's several consuming routes, issue #94 follow-up; adds the
clerk-js frontend loader wiring (fail-fast CLERK_PUBLISHABLE_KEY
validation + a context processor exposing
clerk_publishable_key/clerk_frontend_api_host to every template, issue
#149) that completes Clerk's cross-domain sign-in handshake, which the
Python backend SDK alone cannot do); gates that clerk-js loader off of
every PUBLIC_PATHS page via the shared _is_public_request_path()
normalization (issue #158: a visitor reading /privacy before ever
deciding to sign up should not have their IP/UA phoned to Clerk); adds a
context processor exposing clerk_sign_up_url/clerk_sign_in_url to every
template, powering base.html's header sign-in/sign-up nav on public pages
and the 401 page (issue #145 — the acquisition funnel previously had no
discoverable entry point anywhere on the public surface)."""

from __future__ import annotations

import logging
import os

from flask import Flask, abort, g, render_template, request
from flask_wtf import CSRFProtect
from flask_wtf.csrf import CSRFError

from jobcannon.web.anon_session import capture_attribution, ensure_session_ids
from jobcannon.web.handoff import run_handoff_if_pending
from jobcannon.web.security_headers import register_security_headers

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz", "/demo", "/start", "/preview", "/privacy", "/terms"})

# Terms of Service §8's AGPL Corresponding Source offer names this repo.
_REPO_URL = "https://github.com/Senkichi/jobcannon"


def _is_public_request_path() -> bool:
    """The one normalization PUBLIC_PATHS membership is checked against,
    shared by clerk_auth's before_request gate and inject_clerk_frontend's
    clerk-js loader gate (issue #158) so the two can never drift apart --
    strict_slashes=False routes like /demo register both /demo and /demo/,
    so a trailing slash is stripped (falling back to "/") before the
    membership check, exactly as clerk_auth used to do inline."""
    return (request.path.rstrip("/") or "/") in PUBLIC_PATHS


def _source_link_context(host_config) -> dict[str, str]:
    """base.html's footer "Source" link, derived from HOST_CONFIG rather than
    read ad hoc in the template: `getattr(..., "render_git_commit", "")`
    (not a bare attribute access) so a HOST_CONFIG test double that predates
    this field — e.g. tests/host/test_empty_states.py's bare
    `types.SimpleNamespace(clerk_sign_up_url="")`, which every request
    renders through this same context processor — degrades to the unset
    branch instead of raising AttributeError, mirroring the 401 handler's
    identical tolerance for `clerk_sign_up_url` a few lines up. Unset (local
    dev, most test doubles): the repo root URL, text-identical to the
    literal this footer link used to hard-code, so no existing assertion of
    the plain URL breaks. Set (every real Render deploy): a /tree/<sha> link
    pinned to the exact commit the running instance was built from, with the
    7-char short SHA carried in `source_sha_short` for the template to show
    as a title/tooltip rather than lengthening the visible "Source" text."""
    sha = getattr(host_config, "render_git_commit", "") or ""
    if not sha:
        return {"source_url": _REPO_URL, "source_sha_short": ""}
    return {"source_url": f"{_REPO_URL}/tree/{sha}", "source_sha_short": sha[:7]}


def _auth_link_context(host_config) -> dict[str, str]:
    """base.html's header sign-in/sign-up nav (issue #145), derived from
    HOST_CONFIG the same way _source_link_context derives the footer's
    Source link: getattr, not a bare attribute access, so a HOST_CONFIG test
    double that predates one or both fields -- e.g.
    tests/host/test_pages.py's bare types.SimpleNamespace(clerk_sign_up_url=
    ""), which every request renders through this same context processor --
    degrades to "" for whichever field it lacks instead of raising
    AttributeError. Each URL renders independently in the template (an
    unset one renders nothing, never a bare href="")."""
    return {
        "clerk_sign_up_url": getattr(host_config, "clerk_sign_up_url", ""),
        "clerk_sign_in_url": getattr(host_config, "clerk_sign_in_url", ""),
    }


def _warn_if_auth_links_unset(host_config) -> None:
    """Both fields _auth_link_context reads are non-secret and intentionally
    NOT fail-fast (unlike CLERK_PUBLISHABLE_KEY/WEBHOOK_SECRET below): a bare
    HOST_CONFIG test double or a deliberate soft-launch gate must still boot.
    But silent-degrade-to-nothing is exactly how issue #145 happened in the
    first place (a blank CLERK_SIGN_UP_URL rendered zero sign-up affordances
    anywhere on the public surface with no signal anyone could see short of
    live QA) -- so a boot-time WARNING makes a future unset/typo'd var
    self-announcing in the deploy logs instead of silently regressing #145,
    without hard-failing the process the way a missing secret does."""
    if not getattr(host_config, "clerk_sign_up_url", ""):
        logger.warning(
            "CLERK_SIGN_UP_URL is unset -- the header nav, /preview CTA, and 401 page "
            "render no sign-up link anywhere (issue #145 regresses silently)"
        )
    if not getattr(host_config, "clerk_sign_in_url", ""):
        logger.warning(
            "CLERK_SIGN_IN_URL is unset -- the header nav renders no sign-in link "
            "anywhere (issue #145 regresses silently)"
        )


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
            clerk_sign_in_url="https://clerk.test/sign-in",
            signup_wave="0",
        )
    app.config["HOST_CONFIG"] = host_config  # ALWAYS set, both branches
    if not app.config.get("TESTING"):
        _warn_if_auth_links_unset(host_config)

    @app.context_processor
    def _inject_footer_source_link():
        # Runs on every template render (Flask's own g/request/session
        # injection mechanism) — deliberately exposes only the two DERIVED
        # values _source_link_context computes, never the full HOST_CONFIG
        # object, which also carries Clerk/webhook secrets no template
        # should be able to touch.
        return _source_link_context(app.config["HOST_CONFIG"])

    @app.context_processor
    def _inject_auth_links():
        # Runs on every template render, same as the footer source link
        # above -- base.html's header nav is gated in the template on
        # `not g.clerk_user` (public pages and the 401 page render it,
        # authed pages don't), not on request.path, so this needs no
        # PUBLIC_PATHS-based logic of its own.
        return _auth_link_context(app.config["HOST_CONFIG"])

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
    if app.config.get("TESTING") and not secret_key:
        # A TESTING HOST_CONFIG double that carries no secret_key of its own
        # (e.g. tests/host/test_empty_states.py's bare
        # `types.SimpleNamespace(clerk_sign_up_url="")`, predating issue #146)
        # must still get a real Flask session: base.html's `csrf_meta_tag()`/
        # `csrf_token()` (issue #146) touch the session on EVERY render now,
        # including error_401.html, so a blank secret_key breaks page
        # rendering itself with a RuntimeError, not just an actual CSRF
        # check — before CSRF, a double this bare only had to survive routes
        # that never touched Flask's session at all. Same literal the
        # HOST_CONFIG-absent TESTING branch above already uses, so a test
        # reading the value back sees one consistent constant either way.
        secret_key = "testing-secret-key"
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

    # CSRF protection (issue #146) — Flask-WTF's session-embedded
    # double-submit token, not a hand-rolled one: SECRET_KEY above is a
    # stable, deployment-configured value (JC_SECRET_KEY), identical across
    # every gunicorn --preload worker (never minted per-process), so a token
    # issued by one worker validates against a request served by another —
    # the one condition that would have forced the hand-rolled HMAC
    # alternative instead. `WTF_CSRF_ENABLED` defaults to the SAME value
    # production gets (True) even under TESTING, matching this app's real
    # enforcement rather than silently exempting every test from it; the
    # tests/host/*'s existing POST call sites that predate CSRF need it off
    # to keep passing unmodified, so TESTING flips the default to False
    # UNLESS the caller's own `config` dict already set
    # WTF_CSRF_ENABLED explicitly (setdefault, checked against app.config as
    # already updated from `config` at the top of this function) —
    # tests/host/test_csrf.py is the one module that opts back in to
    # exercise the real enforcement path end to end.
    app.config.setdefault("WTF_CSRF_ENABLED", not app.config.get("TESTING"))
    csrf = CSRFProtect(app)

    @app.errorhandler(CSRFError)
    def csrf_error(error):
        # A clear, dedicated page for a same-origin browser navigation that
        # fails CSRF (e.g. a stale tab's form re-submitted after a session
        # rotated) — never the generic Werkzeug plain-text 400 body. HTMX's
        # own mutation controls (save/dismiss/apply) get the small fragment
        # instead: their target is a DOM node inside the page, not the whole
        # document, so swapping in a full HTML page there would corrupt the
        # layout the same way any other fragment route returning a full page
        # would (jobcannon/CLAUDE.md's HTMX conventions).
        if request.headers.get("HX-Request"):
            return render_template("_csrf_error_fragment.html", reason=error.description), 400
        return render_template("error_csrf.html", reason=error.description), 400

    # Clerk frontend (clerk-js) wiring — issue #149. A blank or malformed
    # publishable key must never silently reproduce #149 (clerk-js never
    # loads -> the hosted Account Portal sign-in never hands this host a
    # __session cookie -> every signed-in human 401s forever), so this
    # fails fast at boot, same shape/rationale as WEBHOOK_SECRET and
    # SECRET_KEY above. TESTING tolerates a blank key (most tests never
    # care about the frontend loader) but still derives the FAPI host from
    # a configured one, so tests exercising the loader don't need a second
    # seam.
    from jobcannon.web.clerk_frontend import frontend_api_host

    # getattr, not direct access: TESTING config doubles (e.g.
    # tests/host/test_empty_states.py's types.SimpleNamespace) may carry
    # only the HOST_CONFIG attributes a given test cares about, same
    # rationale as the 401 handler's clerk_sign_up_url read below.
    clerk_publishable_key = getattr(host_config, "clerk_publishable_key", "")
    clerk_frontend_api_host = ""
    if not app.config.get("TESTING"):
        if not clerk_publishable_key:
            raise RuntimeError(
                "CLERK_PUBLISHABLE_KEY is required (Clerk frontend publishable key; unset "
                "means clerk-js never loads and a hosted sign-in never hands this host a "
                "session — issue #149)"
            )
        try:
            clerk_frontend_api_host = frontend_api_host(clerk_publishable_key)
        except ValueError as exc:
            raise RuntimeError(f"CLERK_PUBLISHABLE_KEY is malformed: {exc}") from exc
    elif clerk_publishable_key:
        try:
            clerk_frontend_api_host = frontend_api_host(clerk_publishable_key)
        except ValueError:
            # Blank BOTH so the template's `{% if clerk_publishable_key %}`
            # gate skips the loader entirely instead of emitting a script
            # tag with an empty host ("https:///npm/...").
            clerk_publishable_key = ""
            clerk_frontend_api_host = ""

    # Stashed on app.config (not just the closure above) so
    # jobcannon.web.security_headers can build the CSP's script-src/
    # connect-src host allowances without re-deriving the FAPI host from the
    # publishable key a second time at a second call site — one derivation,
    # read by two consumers.
    app.config["CLERK_FRONTEND_API_HOST"] = clerk_frontend_api_host

    @app.context_processor
    def inject_clerk_frontend():
        # Issue #158: clerk-js has a job only on the 401 handshake-repair
        # page (error_401.html, issue #151) and pages rendered for a
        # signed-in visitor -- never on a PUBLIC_PATHS page (/demo, /start,
        # /preview, /privacy, /terms), where loading it has no purpose
        # other than Clerk (and Cloudflare in front of it) receiving the
        # visitor's IP/UA and setting cookies before they've done anything.
        # A public path is never the reason a request 401s -- clerk_auth
        # exempts every PUBLIC_PATHS request from verification before this
        # ever runs -- so any request that reaches a non-public path
        # already needed clerk-js, whether it ends up 401ing or rendering
        # an authed page. Reuses the exact same normalization clerk_auth's
        # gate uses (_is_public_request_path) so the two can never diverge.
        if _is_public_request_path():
            return {"clerk_publishable_key": "", "clerk_frontend_api_host": ""}
        return {
            "clerk_publishable_key": clerk_publishable_key,
            "clerk_frontend_api_host": clerk_frontend_api_host,
        }

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
    @csrf.exempt
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

        error_401.html's own signup link and base.html's header nav both
        read clerk_sign_up_url (and, for the header nav, clerk_sign_in_url)
        from the global _inject_auth_links context processor above, not a
        route-specific kwarg here -- that processor is the one HOST_CONFIG
        accessor for both fields, reached the same way on every render.
        """
        return render_template("error_401.html"), 401

    @app.before_request
    def clerk_auth():
        # before_request runs for EVERY request regardless of routing
        # outcome — Flask raises the routing exception later, in dispatch —
        # so an unmatched path still hits this gate and 401s before it ever
        # gets a chance to 404 (deliberate fail-closed). URL-rule matching
        # HAS already run by this point for matched routes, though, which is
        # what the request.blueprint == "webhooks" exemption relies on. The
        # public-path exemption rests solely on _is_public_request_path()'s
        # normalized-path membership check: request.path is compared with a
        # trailing slash stripped (falling back to "/") because /demo is
        # registered strict_slashes=False, so both /demo and /demo/ must
        # match.
        if _is_public_request_path() or request.blueprint == "webhooks":
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
    # Svix already authenticates this route by HMAC signature over the raw
    # body (jobcannon/web/webhooks.py's module docstring) — Clerk's webhook
    # sender carries no browser session/cookie and can mint no CSRF token,
    # so the double-submit check would only ever reject the legitimate
    # sender, never a forged one.
    csrf.exempt(webhooks_bp)

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

    from jobcannon.web.legal import legal_bp

    app.register_blueprint(legal_bp)

    # Registered last, once every blueprint above is mounted and
    # CLERK_FRONTEND_API_HOST/HOST_CONFIG are both final — issue #147.
    register_security_headers(app)
    return app
