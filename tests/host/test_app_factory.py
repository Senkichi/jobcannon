"""Non-TESTING create_app / build_clerk_verifier wiring (pins F1/F2/F3a).

No Postgres needed: jobcannon.host.init_engine_seams/load_host_config and
clerk_backend_api.Clerk are stubbed at their call-time import seams, so these
tests exercise the real create_app()/build_clerk_verifier() code paths
without a live database or a real Clerk backend.
"""

import pytest


class _FakeState:
    def __init__(self, is_signed_in, payload):
        self.is_signed_in = is_signed_in
        self.payload = payload


class _FakeClerk:
    """Stands in for clerk_backend_api.Clerk: records init kwargs, and
    authenticate_request returns a fixed signed-in state regardless of the
    request/options passed — sufficient to exercise the verify() seam."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def authenticate_request(self, request, options):
        return _FakeState(is_signed_in=True, payload={"sub": "user_x", "org_id": None})


def _stub_seams(monkeypatch):
    monkeypatch.setattr("jobcannon.host.init_engine_seams", lambda *a, **kw: None)
    monkeypatch.setattr("jobcannon.host.load_host_config", lambda: object())


def _set_clerk_env(monkeypatch, *, jwt_key="jwt_test", authorized_parties="https://example.org"):
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test")
    monkeypatch.setenv("CLERK_JWT_KEY", jwt_key)
    monkeypatch.setenv("CLERK_AUTHORIZED_PARTIES", authorized_parties)


def test_create_app_wires_verify_request_when_seams_stubbed(monkeypatch):
    """create_app(), non-TESTING: VERIFY_REQUEST must be the real
    build_clerk_verifier() output, not left None."""
    _stub_seams(monkeypatch)
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    _set_clerk_env(monkeypatch)
    monkeypatch.setenv("CLERK_WEBHOOK_SIGNING_SECRET", "whsec_dGVzdA==")

    from jobcannon.web import create_app

    app = create_app(config={})
    verify = app.config["VERIFY_REQUEST"]
    assert verify is not None
    assert callable(verify)


def test_create_app_raises_on_missing_webhook_secret(monkeypatch):
    """F3a: an unset CLERK_WEBHOOK_SIGNING_SECRET must fail fast at startup,
    non-TESTING, before any request is served."""
    _stub_seams(monkeypatch)
    monkeypatch.delenv("CLERK_WEBHOOK_SIGNING_SECRET", raising=False)

    from jobcannon.web import create_app

    with pytest.raises(RuntimeError, match="CLERK_WEBHOOK_SIGNING_SECRET"):
        create_app(config={})


def test_build_clerk_verifier_returns_callable_and_identity(monkeypatch):
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    _set_clerk_env(monkeypatch)

    from jobcannon.web.auth import ClerkIdentity, build_clerk_verifier

    verify = build_clerk_verifier()
    assert callable(verify)

    identity = verify(object())  # request is never inspected by the fake SDK
    assert identity == ClerkIdentity(user_id="user_x", claims={"sub": "user_x", "org_id": None})


def test_build_clerk_verifier_raises_on_blank_authorized_parties(monkeypatch):
    """Pins F1: unset/blank CLERK_AUTHORIZED_PARTIES must fail fast rather
    than silently disabling the SDK's azp (replay) check."""
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    _set_clerk_env(monkeypatch, authorized_parties="")

    from jobcannon.web.auth import build_clerk_verifier

    with pytest.raises(RuntimeError, match="CLERK_AUTHORIZED_PARTIES"):
        build_clerk_verifier()


def test_build_clerk_verifier_raises_on_blank_jwt_key(monkeypatch):
    """Pins F2: unset/blank CLERK_JWT_KEY must fail fast rather than
    silently falling back to a per-request JWKS network fetch."""
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    _set_clerk_env(monkeypatch, jwt_key="")

    from jobcannon.web.auth import build_clerk_verifier

    with pytest.raises(RuntimeError, match="CLERK_JWT_KEY"):
        build_clerk_verifier()
