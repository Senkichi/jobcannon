"""Non-TESTING create_app / build_clerk_verifier wiring (pins F1/F2/F3a).

No Postgres needed: jobcannon.host.init_engine_seams/load_host_config and
clerk_backend_api.Clerk are stubbed at their call-time import seams, so these
tests exercise the real create_app()/build_clerk_verifier() code paths
without a live database or a real Clerk backend.

The four CLERK_* values flow through HostConfig (issue #47), not monkeypatched
os.environ — _stub_seams/_clerk_host_config below build the HostConfig
directly rather than setting env vars build_clerk_verifier no longer reads.
"""

import pytest

from jobcannon.host.config import HostConfig


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


def _clerk_host_config(**overrides) -> HostConfig:
    fields = dict(
        database_url="postgresql:///stub",
        secret_key="sk_flask_test",
        clerk_sign_up_url="https://clerk.test/sign-up",
        signup_wave="0",
        clerk_secret_key="sk_test",
        clerk_jwt_key="jwt_test",
        # base64("clerk.test$") == "Y2xlcmsudGVzdCQ=" -> FAPI host "clerk.test".
        clerk_publishable_key="pk_test_Y2xlcmsudGVzdCQ=",
        clerk_authorized_parties="https://example.org",
        clerk_webhook_signing_secret="whsec_dGVzdA==",
    )
    fields.update(overrides)
    return HostConfig(**fields)


def _stub_seams(monkeypatch, **overrides):
    monkeypatch.setattr("jobcannon.host.init_engine_seams", lambda *a, **kw: None)
    monkeypatch.setattr(
        "jobcannon.host.load_host_config",
        lambda: _clerk_host_config(**overrides),
    )


def test_create_app_wires_verify_request_when_seams_stubbed(monkeypatch):
    """create_app(), non-TESTING: VERIFY_REQUEST must be the real
    build_clerk_verifier() output, not left None."""
    _stub_seams(monkeypatch)
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)

    from jobcannon.web import create_app

    app = create_app(config={})
    verify = app.config["VERIFY_REQUEST"]
    assert verify is not None
    assert callable(verify)


def test_create_app_wires_and_shares_one_clerk_client(monkeypatch):
    """CLERK_CLIENT must land on app.config as the SAME instance handed to
    build_clerk_verifier — jobcannon.web.account reuses that object for its
    user-delete management call rather than constructing a second client.
    Counting constructions (not just asserting non-None) is what actually
    pins "shared", since a naive re-implementation could build two clients
    and still leave a non-None CLERK_CLIENT behind."""
    _stub_seams(monkeypatch)
    calls = []

    class _CountingFakeClerk(_FakeClerk):
        def __init__(self, **kwargs):
            calls.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr("clerk_backend_api.Clerk", _CountingFakeClerk)

    from jobcannon.web import create_app

    app = create_app(config={})

    assert len(calls) == 1
    assert isinstance(app.config["CLERK_CLIENT"], _CountingFakeClerk)


def test_create_app_raises_on_missing_webhook_secret(monkeypatch):
    """F3a: an unset CLERK_WEBHOOK_SIGNING_SECRET must fail fast at startup,
    non-TESTING, before any request is served.

    secret_key is stubbed blank here (not the default "sk_flask_test") so
    this test actually pins the fail-fast ORDER: the webhook-secret check
    must raise before the secret-key check gets a chance to. With a truthy
    secret_key the two orderings are indistinguishable — this test's outcome
    would be identical either way, and the comment at the SECRET_KEY call
    site in jobcannon/web/__init__.py claiming this test pins the ordering
    would be false."""
    _stub_seams(monkeypatch, secret_key="", clerk_webhook_signing_secret="")

    from jobcannon.web import create_app

    with pytest.raises(RuntimeError, match="CLERK_WEBHOOK_SIGNING_SECRET"):
        create_app(config={})


def test_create_app_raises_on_missing_flask_secret_key(monkeypatch):
    """A blank JC_SECRET_KEY (surfaced via HostConfig.secret_key) must fail
    fast at startup, non-TESTING, before any request is served — same
    rationale and shape as the webhook-secret fail-fast above, and ordered
    after it (see the comment at the call site in jobcannon/web/__init__.py)."""
    _stub_seams(monkeypatch, secret_key="")

    from jobcannon.web import create_app

    with pytest.raises(RuntimeError, match="JC_SECRET_KEY"):
        create_app(config={})


def test_create_app_raises_on_missing_clerk_publishable_key(monkeypatch):
    """Issue #149: a blank CLERK_PUBLISHABLE_KEY must fail fast at startup,
    non-TESTING — the same shape as the other CLERK_*/SECRET_KEY fail-fasts
    above. Silently booting without it would silently reproduce #149
    (clerk-js never loads, so the hosted sign-in never hands this host a
    session)."""
    _stub_seams(monkeypatch, clerk_publishable_key="")
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)

    from jobcannon.web import create_app

    with pytest.raises(RuntimeError, match="CLERK_PUBLISHABLE_KEY"):
        create_app(config={})


def test_create_app_raises_on_malformed_clerk_publishable_key(monkeypatch):
    """A non-blank but malformed key (no pk_live_/pk_test_ prefix, bad
    base64, missing '$' sentinel) must also fail fast rather than boot with
    a broken/empty FAPI host that would load clerk-js against nothing."""
    _stub_seams(monkeypatch, clerk_publishable_key="not-a-real-clerk-key")
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)

    from jobcannon.web import create_app

    with pytest.raises(RuntimeError, match="CLERK_PUBLISHABLE_KEY"):
        create_app(config={})


def test_build_clerk_verifier_returns_callable_and_identity(monkeypatch):
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    host_config = _clerk_host_config()

    from jobcannon.web.auth import ClerkIdentity, build_clerk_verifier

    verify = build_clerk_verifier(host_config)
    assert callable(verify)

    identity = verify(object())  # request is never inspected by the fake SDK
    assert identity == ClerkIdentity(user_id="user_x", claims={"sub": "user_x", "org_id": None})


def test_build_clerk_verifier_reuses_a_passed_in_client(monkeypatch):
    """When a client is supplied, build_clerk_verifier must not construct a
    second one — pinned here by a blank CLERK_SECRET_KEY, which
    build_clerk_client would reject, not raising because the passed client
    bypasses that construction path entirely."""
    constructed = []
    monkeypatch.setattr(
        "clerk_backend_api.Clerk",
        lambda **kw: constructed.append(kw) or _FakeClerk(**kw),
    )
    host_config = _clerk_host_config(clerk_secret_key="")
    client = _FakeClerk()

    from jobcannon.web.auth import build_clerk_verifier

    verify = build_clerk_verifier(host_config, client=client)

    assert constructed == []
    assert callable(verify)


def test_build_clerk_verifier_raises_on_blank_secret_key(monkeypatch):
    """A blank CLERK_SECRET_KEY (surfaced via HostConfig.clerk_secret_key)
    must fail fast rather than initializing the Clerk SDK with an empty
    bearer token."""
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    host_config = _clerk_host_config(clerk_secret_key="")

    from jobcannon.web.auth import build_clerk_verifier

    with pytest.raises(RuntimeError, match="CLERK_SECRET_KEY"):
        build_clerk_verifier(host_config)


def test_build_clerk_verifier_raises_on_blank_authorized_parties(monkeypatch):
    """Pins F1: unset/blank CLERK_AUTHORIZED_PARTIES must fail fast rather
    than silently disabling the SDK's azp (replay) check."""
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    host_config = _clerk_host_config(clerk_authorized_parties="")

    from jobcannon.web.auth import build_clerk_verifier

    with pytest.raises(RuntimeError, match="CLERK_AUTHORIZED_PARTIES"):
        build_clerk_verifier(host_config)


def test_build_clerk_verifier_normalizes_authorized_parties(monkeypatch):
    """Issue #149 point 3: Render's CLERK_AUTHORIZED_PARTIES was set to
    "https://jobcannon.dev/" (trailing slash), which would exact-match-fail
    against every token's bare-origin `azp` claim
    (TOKEN_INVALID_AUTHORIZED_PARTIES) even after the __session fix. Each
    configured party must be trimmed of whitespace AND a trailing slash
    before being handed to the SDK. The expected list below is a literal,
    not re-derived via the same split/strip/rstrip the code under test
    uses."""
    captured = {}

    class _CapturingFakeClerk(_FakeClerk):
        def authenticate_request(self, request, options):
            captured["authorized_parties"] = options.authorized_parties
            return super().authenticate_request(request, options)

    monkeypatch.setattr("clerk_backend_api.Clerk", _CapturingFakeClerk)
    host_config = _clerk_host_config(
        clerk_authorized_parties="https://jobcannon.dev/, https://www.jobcannon.dev"
    )

    from jobcannon.web.auth import build_clerk_verifier

    verify = build_clerk_verifier(host_config)
    verify(object())  # request is never inspected by the fake SDK

    assert captured["authorized_parties"] == [
        "https://jobcannon.dev",
        "https://www.jobcannon.dev",
    ]


def test_build_clerk_verifier_raises_on_blank_jwt_key(monkeypatch):
    """Pins F2: unset/blank CLERK_JWT_KEY must fail fast rather than
    silently falling back to a per-request JWKS network fetch."""
    monkeypatch.setattr("clerk_backend_api.Clerk", _FakeClerk)
    host_config = _clerk_host_config(clerk_jwt_key="")

    from jobcannon.web.auth import build_clerk_verifier

    with pytest.raises(RuntimeError, match="CLERK_JWT_KEY"):
        build_clerk_verifier(host_config)
