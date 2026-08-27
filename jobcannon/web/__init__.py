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
discoverable entry point anywhere on the public surface); adds a
`request.routing_exception` re-raise at the top of clerk_auth (issue #173)
so an unmatched path or a wrong HTTP method on a real route reaches
Flask's normal 404/405 handling instead of being swallowed into the 401
gate, plus branded 404/405/400 errorhandlers sharing one error.html
template; adds public_get, a per-view GET-only auth opt-out decorator
(issue #171) that lets GET /consent render a signed-out explanation
without adding /consent to PUBLIC_PATHS (which would also exempt the
POST mutation and skip clerk-js loading entirely, including for a
signed-in visitor); extends the #158 clerk-js gate itself to also skip
loading it on a public_get view's signed-out render specifically (a
signed-out GET /consent is, for that request, the same class of page as
/privacy -- nothing gated behind it -- even though the route as a whole
isn't a PUBLIC_PATHS member); adds a revoked_subjects tombstone check to
clerk_auth (issue #159), closing the stale-JWT window a networkless-
verified session leaves open after an account deletion -- placed after
the routing-exception re-raise and identity resolution above, so an
unmatched path or an unauthenticated request still 404s/401s the same
way it did before this check existed; adds an HX-Request-aware branch to
the 401 errorhandler (issue #155) so a stale-session htmx fragment
request gets an HX-Redirect instead of a full HTML document swapped into
a fragment target."""

from __future__ import annotations

import logging
import os

from flask import Flask, abort, g, make_response, render_template, request, session
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


def public_get(view_func):
    """Per-view, GET-only auth opt-out (issue #171): marks `view_func` so
    `clerk_auth`'s before_request gate below renders it for a signed-out
    visitor instead of aborting, while every OTHER method on the same
    view -- critically, POST /consent -- stays fully gated, since the
    marker is keyed to GET/HEAD specifically rather than "this endpoint is
    public."

    Deliberately NOT a PUBLIC_PATHS entry: PUBLIC_PATHS exempts a path for
    every method (clerk_auth's gate above) AND skips clerk-js loading
    entirely (inject_clerk_frontend, issue #158) -- neither is correct for
    a view like GET /consent, which still wants clerk-js loaded (a
    signed-in visitor hitting this same view needs the header nav's authed
    state) and still wants POST /consent hard-gated (issue #171 is
    explicit: consent is an account-level, authed-only decision; only the
    read-only explanatory GET view opens up).

    The marked view still receives whatever identity clerk_auth resolved
    -- None when signed out, a real ClerkIdentity when the visitor happens
    to be signed in -- and is responsible for branching on `g.clerk_user`
    itself, the same signal every other template in this app already
    reads."""
    view_func._auth_optional_get = True
    return view_func


def _is_auth_optional_for_method(view_func, method: str) -> bool:
    """The single predicate `clerk_auth` consults for `public_get`'s
    marker, so a test (or any future consumer) checks the SAME rule the
    gate itself uses rather than re-deriving it against the private
    attribute name. Covers every safe, non-mutating method Flask will
    route to the marked view without ever calling it: GET, HEAD (not a
    literal reading of "GET only" from issue #171 -- HEAD requests to a
    GET-registered view must mirror that GET's status code, that's HEAD's
    whole contract), and OPTIONS (Flask's automatic OPTIONS responder
    answers this itself in dispatch_request -- if the gate aborted 401
    first, a signed-out CORS preflight or an `OPTIONS /consent` probe
    would see 401 on a route that serves GET as 200, the same
    routing-vs-auth-layer confusion issue #173 exists to fix, just one
    layer down). POST is deliberately excluded: it is the one method this
    marker must never open, since POST /consent is the account mutation
    issue #171 keeps fully gated."""
    return bool(getattr(view_func, "_auth_optional_get", False)) and method in (
        "GET",
        "HEAD",
        "OPTIONS",
    )


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


def _is_subject_revoked(identity) -> bool:
    """One DB read per authenticated request, checked in `clerk_auth` right
    after JWT verification succeeds (issue #159). `auth.py` verifies the
    `__session` JWT purely locally (RS256, zero network calls per request),
    so a token stays independently valid until its own `exp` even after
    `jobcannon/web/account.py::post_delete` or the `user.deleted` webhook
    has already tombstoned this subject — this is the read half of that
    tombstone, and the whole reason it exists.

    Fails OPEN (not-revoked) on any error, same shape as `_resolve_consent`
    above -- but the justification differs and is worth stating explicitly,
    since "matches the neighboring function" is not on its own a security
    argument: `connection_factory()` raising here means the DB pool is
    unusable, and EVERY authed route already depends on that same pool
    (consent lookup above, `/account/export`, the feed, etc.) -- so this
    failing open does not create a new "revoked user reaches real data"
    path, it only means a revoked user gets the same degraded response
    every other signed-in user gets during a pool outage. Logged at WARNING
    with a message naming "revocation" specifically (not a generic "lookup
    failed") so this failure mode -- e.g. an instance rolling before the
    worker applies migration m0007 -- is greppable in deploy logs instead
    of silently leaving the feature inert.

    Passes the verified JWT's `iat` claim through to `is_subject_revoked`
    (issue #159 follow-up): without it, a Clerk-delete-call failure in
    account.py::post_delete -- which deliberately leaves the tombstone
    committed even though the account was never actually deleted -- had NO
    recovery path, since a fresh relogin still mints a JWT for the same
    `sub`. See jobcannon/db/_revoked_subjects.py's module docstring for the
    full rationale.
    """
    from jobcannon.db import _revoked_subjects
    from jobcannon.db.pool import connection_factory

    try:
        with connection_factory() as conn:
            return _revoked_subjects.is_subject_revoked(
                conn, identity.user_id, identity.claims.get("iat")
            )
    except Exception:
        logger.warning(
            "revocation lookup failed for user %s (defaulting to not-revoked)",
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
    # alternative instead. `WTF_CSRF_ENABLED` defaults to True in
    # production and False under TESTING — every pre-existing
    # tests/host/*'s POST call site predates CSRF and needs it off to keep
    # passing unmodified — UNLESS the caller's own `config` dict already
    # set WTF_CSRF_ENABLED explicitly (setdefault, checked against
    # app.config as already updated from `config` at the top of this
    # function) — tests/host/test_csrf.py is the one module that opts back
    # in to exercise the real enforcement path end to end.
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
        #
        # `HX-CSRF-Error` (custom, not an htmx-reserved header) is set on
        # BOTH branches so the signal stays consistent regardless of shape.
        # It matters for the htmx one specifically: htmx 2.0.4's default
        # `responseHandling` maps every 4xx to `{swap: false, error: true}`,
        # so the fragment body above is discarded, never swapped into the
        # DOM — only the `htmx:responseError` event fires. base.html's
        # listener on that event reads this header to surface a visible
        # "refresh and try again" toast without it, a CSRF-rejected
        # save/dismiss click did (and looked exactly like) nothing at all.
        if request.headers.get("HX-Request"):
            response = make_response(
                render_template("_csrf_error_fragment.html", reason=error.description), 400
            )
        else:
            response = make_response(
                render_template("error_csrf.html", reason=error.description), 400
            )
        response.headers["HX-CSRF-Error"] = "1"
        return response

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
        # page (error_401.html, issue #151) and pages rendered where the
        # visitor could plausibly be signed in -- never on a page a
        # signed-out visitor can reach with nothing gated behind it, where
        # loading it has no purpose other than Clerk (and Cloudflare in
        # front of it) receiving the visitor's IP/UA and setting cookies
        # before they've done anything. That's PUBLIC_PATHS (/demo, /start,
        # /preview, /privacy, /terms, checked below via the same
        # _is_public_request_path clerk_auth's gate uses, so the two can
        # never diverge) AND, separately, a public_get-marked view (issue
        # #171) rendering ITS OWN signed-out branch -- e.g. GET /consent
        # when g.clerk_user is None: unlike PUBLIC_PATHS, that route is
        # reachable by a signed-in visitor too (the footer link renders on
        # every page), and a signed-in render of it still needs clerk-js
        # for the header nav's authed state, so the exemption is scoped to
        # "this specific request resolved to no identity", not "this
        # endpoint". The two conditions are deliberately ANDed, never
        # collapsed into a bare `identity is None` check: error_401.html is
        # exactly the page where identity IS None and clerk-js IS required
        # (the stale-__client handshake-repair reload, issue #151) -- a
        # bare identity-only gate would silently break that page instead.
        if _is_public_request_path():
            return {"clerk_publishable_key": "", "clerk_frontend_api_host": ""}
        view_func = app.view_functions.get(request.endpoint) if request.endpoint else None
        if (
            _is_auth_optional_for_method(view_func, request.method)
            and getattr(g, "clerk_user", None) is None
        ):
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
        sign-in prompt: `clerk_auth` below still renders this for
        `/static/<path>` (Flask registers that rule unconditionally, and
        it is not in PUBLIC_PATHS -- a missing file 404s from inside the
        view, but the route itself still requires a session first) and
        for any other real, matched, non-public route a signed-out
        visitor hits. An UNMATCHED path or a wrong HTTP method on a real
        route no longer reaches this handler at all (issue #173):
        clerk_auth re-raises `request.routing_exception` before any auth
        logic runs, so Flask's own 404/405 errorhandlers below take over
        instead -- a visitor (or a monitoring tool) can now tell a
        typo'd/removed URL apart from a genuinely gated one.
        The status code stays 401.

        error_401.html's own signup link and base.html's header nav both
        read clerk_sign_up_url (and, for the header nav, clerk_sign_in_url)
        from the global _inject_auth_links context processor above, not a
        route-specific kwarg here -- that processor is the one HOST_CONFIG
        accessor for both fields, reached the same way on every render.

        Issue #155: an HTMX fragment request (HX-Request: true) that lands
        here -- most commonly a stale-session tab firing a swap after the
        server-side session/JWT has gone bad -- must NOT get this full HTML
        document back. htmx would swap the whole document's markup into the
        small fragment target the original request named, corrupting the
        page. Checked case-insensitively (`.lower()`) rather than an exact
        "true" match: htmx itself always sends lowercase "true", but an
        exact-match miss on some other casing would silently fall through
        to the full-document bug this branch exists to prevent, so the
        cheap defensive compare is worth it. That branch returns a tiny,
        non-HTML body (never contains "<html") and an HX-Redirect header
        instead -- HX-Redirect forces htmx to do a full client-side
        navigation (window.location = ...) rather than any swap at all, so
        the stale fragment's target is never touched. Redirect target is
        clerk_sign_in_url when configured, else "/" (the public feed
        preview), same source as the header nav's sign-in link above -- an
        unconfigured sign-in URL must still send the visitor SOMEWHERE
        useful, never to a broken/blank href.

        The non-HX branch (below) is unchanged from before this issue --
        still the full error_401.html document, still status 401 -- so
        #165's existing sign-in/sign-up-link tests on that page stay green.
        """
        if (request.headers.get("HX-Request") or "").lower() == "true":
            sign_in_url = getattr(app.config["HOST_CONFIG"], "clerk_sign_in_url", "") or "/"
            response = app.make_response(("", 401))
            response.headers["HX-Redirect"] = sign_in_url
            return response
        return render_template("error_401.html"), 401

    def _branded_error_response(status_code: int, title: str, message: str):
        """Shared body for the 404/405/400 errorhandlers below (issue
        #173) -- one template (error.html) instead of three near-identical
        ones, and one place that decides the HX-Request branch so all
        three stay consistent.

        `message` must never carry request-derived data (the attempted
        path, the method, a stack trace) -- these render for an anonymous,
        possibly-malicious visitor, so the body has to be as safely
        static/brandable as error_401.html already is.

        HX-Request branch: htmx 2's documented default
        (`htmx.config.responseHandling`) does NOT swap a 4xx/5xx response
        into the DOM at all -- it fires `htmx:responseError` and leaves the
        target untouched, and this app never overrides that default
        anywhere (unlike the 401 case, which needs an HX-Redirect because
        a stale session genuinely has to navigate somewhere). So unlike
        the 401 handler, there is no navigation to force here: the status
        code stays the REAL 404/405/400 in both branches, for a monitoring
        tool and a browser alike. The only thing the HX-Request check
        changes is the BODY SHAPE -- a small, non-templated plain-text
        message instead of the full error.html document (nav, footer,
        clerk-js script tag, the works) -- so that IF this app or some
        future template ever configures htmx to swap on error (the docs'
        own example flips `evt.detail.shouldSwap` for 422), a full embedded
        HTML document never lands inside a small fragment target.
        """
        if (request.headers.get("HX-Request") or "").lower() == "true":
            return message, status_code
        return render_template(
            "error.html", status_code=status_code, title=title, message=message
        ), status_code

    @app.errorhandler(404)
    def not_found(_error):
        return _branded_error_response(
            404,
            "Page not found",
            "This page doesn't exist. Double-check the link, or head back to the feed.",
        )

    @app.errorhandler(405)
    def method_not_allowed(error):
        """Werkzeug's MethodNotAllowed carries the route's real allowed
        methods on `.valid_methods` -- Flask's DEFAULT error handling
        copies that onto an `Allow` response header automatically (via
        HTTPException.get_response()), but registering a custom
        errorhandler here means we own the response object and have to
        copy it ourselves, or a genuinely wrong-method request (issue
        #173's `PUT /postings/1/save` example) would come back 405 with no
        `Allow` header -- exactly the diagnosability gap the issue names."""
        body, status = _branded_error_response(
            405,
            "Method not allowed",
            "This request method isn't supported for this page.",
        )
        response = app.make_response((body, status))
        valid_methods = getattr(error, "valid_methods", None)
        if valid_methods:
            response.headers["Allow"] = ", ".join(sorted(valid_methods))
        return response

    @app.errorhandler(400)
    def bad_request(_error):
        """Covers a Werkzeug BadRequest raised from inside this app (e.g.
        malformed request data Flask/Werkzeug itself rejects) with a
        branded page instead of Werkzeug's plain-text default -- issue
        #182 item 5's "very long query string renders a bare/unstyled
        error page" is the motivating case, but note the scope limit: if
        that report's actual query string exceeded the front proxy's
        request-line size limit (e.g. gunicorn's `limit_request_line`,
        ~4094 bytes by default), the request never reaches this Flask app
        at all -- a WSGI-server/proxy-layer 400 can't be caught by any
        errorhandler here, the same class of gap as #182 item 2's
        Cloudflare HEAD Content-Length artifact. This handler covers every
        BadRequest this app's own code can raise; a below-the-app rejection
        would need a render.yaml / proxy config change, not app code."""
        return _branded_error_response(
            400,
            "Bad request",
            "This request couldn't be processed. Double-check the link and try again.",
        )

    @app.before_request
    def clerk_auth():
        # before_request runs for EVERY request regardless of routing
        # outcome, and by this point URL matching has ALREADY happened
        # (Flask populates request.url_rule / request.routing_exception
        # when the request context is pushed, before any before_request
        # hook runs) -- Flask just defers actually RAISING that exception
        # until dispatch_request, which is later than this hook. Left
        # alone, that gap meant an unmatched path or a wrong HTTP method on
        # a real route fell through every check below and came back as a
        # 401 "Sign-in required" page -- indistinguishable from a
        # genuinely gated route (issue #173: a visitor, or a monitoring
        # tool, can't tell a typo'd URL from a real gate, and a wrong
        # method gets no `Allow` header). Re-raising it here, before ANY
        # auth logic runs, hands control straight back to Flask's normal
        # exception handling -- exactly what would happen if this
        # before_request hook didn't exist at all -- which dispatches to
        # the 404/405 errorhandlers below. Deliberately unconditional (no
        # PUBLIC_PATHS / webhooks check first): a wrong-method request
        # against a PUBLIC path must 405 too, not silently pass through as
        # public and fail deeper in the stack. A matched /static/<path>
        # request is NOT affected -- the rule itself matches regardless of
        # whether the file exists, so routing_exception is None there and
        # a missing file still 404s from inside the view, same as before.
        if request.routing_exception is not None:
            # g.clerk_user must be set before ANY errorhandler renders a
            # template -- error.html (the 404/405/400 branded pages below)
            # extends base.html exactly like error_401.html does, and reads
            # g.clerk_user for the header nav, so this path has to honor
            # the SAME "must never see it unset" invariant the identity
            # assignment a few lines down documents for the 401 path.
            # Deliberately None, not a best-effort identity resolution: URL
            # matching hasn't even confirmed this request hit a real route
            # yet, so calling VERIFY_REQUEST here would run it speculatively
            # on every unmatched-path/wrong-method probe an attacker or a
            # scanner throws at this app, and it must never be allowed to
            # change the 404/405 status code that follows. A signed-in
            # visitor hitting a typo'd URL sees the signed-out header nav on
            # that one page, exactly like the public-path branch immediately
            # below -- an accepted, documented trade-off, not a bug.
            g.clerk_user = None
            g.consent_granted = False
            raise request.routing_exception
        # The public-path exemption rests solely on _is_public_request_path()'s
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
            # public_get's per-view, GET-only opt-out (issue #171): a
            # matched view marked with `_auth_optional_get` renders for a
            # signed-out visitor on GET/HEAD instead of aborting -- every
            # OTHER method on that same view (POST /consent) still falls
            # through to abort(401) below, since the marker is checked
            # against request.method, not the endpoint as a whole.
            view_func = app.view_functions.get(request.endpoint)
            if _is_auth_optional_for_method(view_func, request.method):
                g.consent_granted = False
                ensure_session_ids()
                capture_attribution()
                return None
            abort(401)
        if _is_subject_revoked(identity):
            # Issue #159: the JWT verified (it is cryptographically valid
            # and unexpired), but its subject has an unexpired
            # revoked_subjects tombstone -- a deletion (in-app or via
            # Clerk's Account Portal) already happened for this account,
            # and this token was simply minted/refreshed before that. Undo
            # the g.clerk_user set two lines up before aborting: base.html
            # gates the header sign-in/up nav AND the authed footer links on
            # `g.clerk_user`, and error_401.html extends base.html, so a
            # left-set g.clerk_user would render "Export your data /
            # Delete account" links on the very page telling this visitor
            # they're signed out. session.clear() also runs here, not only
            # in account.py's post_delete -- this is the ONLY gate on the
            # webhook-triggered deletion path (an Account-Portal deletion
            # never goes through post_delete at all), and a stale Flask
            # session cookie must not survive a revoked identity either way.
            g.clerk_user = None
            session.clear()
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
