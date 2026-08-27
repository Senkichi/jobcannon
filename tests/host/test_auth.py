def _app(verify=None):
    from jobcannon.web import create_app

    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
        }
    )


def test_health_route_is_public():
    app = _app(verify=lambda req: None)
    client = app.test_client()
    assert client.get("/healthz").status_code == 200


def test_protected_route_401_when_signed_out():
    app = _app(verify=lambda req: None)

    @app.get("/private")
    def private():
        return "secret"

    assert app.test_client().get("/private").status_code == 401


def test_protected_route_passes_identity_when_signed_in():
    from jobcannon.web.auth import ClerkIdentity

    seen = {}
    app = _app(verify=lambda req: ClerkIdentity(user_id="user_123", claims={"sub": "user_123"}))

    @app.get("/private")
    def private():
        from flask import g

        seen["user"] = g.clerk_user
        return "ok"

    assert app.test_client().get("/private").status_code == 200
    assert seen["user"].user_id == "user_123"


def test_webhook_route_is_exempt_from_session_auth():
    app = _app(verify=lambda req: None)
    # 400 (bad signature), NOT 401 — the webhook path must not require a session.
    resp = app.test_client().post("/webhooks/clerk", data=b"{}")
    assert resp.status_code == 400


# --- issue #155: the 401 errorhandler's HX-Request branch -----------------


def test_401_without_hx_request_still_renders_the_full_error_page():
    """Regression guard for the non-HX branch: byte-identical to before
    #155 — still the full error_401.html document, still status 401, still
    no HX-Redirect header — so #165's sign-in/sign-up-link tests on this
    page stay meaningful."""
    app = _app(verify=lambda req: None)

    resp = app.test_client().get("/")

    assert resp.status_code == 401
    assert "HX-Redirect" not in resp.headers
    assert b"Sign-in required" in resp.data


def test_401_with_hx_request_returns_a_tiny_body_and_hx_redirect():
    """A stale-session htmx fragment request (HX-Request: true) must never
    get the full HTML document back — htmx would swap it into whatever
    small fragment target the original request named. Asserts both halves
    of the fix: no "<html" anywhere in the body, and an HX-Redirect header
    that forces a full client-side navigation instead of any swap."""
    app = _app(verify=lambda req: None)

    resp = app.test_client().get("/", headers={"HX-Request": "true"})

    assert resp.status_code == 401
    assert b"<html" not in resp.data.lower()
    assert resp.headers.get("HX-Redirect") == "https://clerk.test/sign-in"


def test_401_with_hx_request_redirects_to_root_when_sign_in_url_is_unset():
    from jobcannon.host.config import HostConfig

    host_config = HostConfig(
        database_url="",
        secret_key="testing-secret-key",
        clerk_sign_up_url="",
        clerk_sign_in_url="",
        signup_wave="0",
    )
    from jobcannon.web import create_app

    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda req: None,
            "WEBHOOK_SECRET": "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q=",
            "HOST_CONFIG": host_config,
        }
    )

    resp = app.test_client().get("/", headers={"HX-Request": "true"})

    assert resp.status_code == 401
    assert resp.headers.get("HX-Redirect") == "/"
