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
