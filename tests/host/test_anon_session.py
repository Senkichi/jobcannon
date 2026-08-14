"""jobcannon/web/anon_session.py — the anonymous session id carrier and
attribution capture — plus the HOST_CONFIG accessor's TESTING-branch
availability (jobcannon/web/__init__.py), which this module's
capture_attribution() depends on for `signup_wave`.

No Postgres needed: these are pure Flask app / session tests, same shape as
tests/host/test_auth.py.
"""

import re

from flask import g, session


def _app(**extra_config):
    from jobcannon.web import create_app
    from jobcannon.web.auth import ClerkIdentity

    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda req: ClerkIdentity(user_id="user_1", claims={"sub": "user_1"}),
            "WEBHOOK_SECRET": "whsec_dGVzdA==",
            **extra_config,
        }
    )

    @app.get("/_probe")
    def probe():
        return {
            "anon_session_id": g.get("anon_session_id"),
            "feed_session_id": g.get("feed_session_id"),
            "attribution": session.get("attribution"),
        }

    return app


def test_ids_minted_once_and_stable_across_requests():
    app = _app()
    client = app.test_client()

    first = client.get("/_probe").get_json()
    second = client.get("/_probe").get_json()

    assert first["anon_session_id"].startswith("anon_")
    assert first["feed_session_id"]
    assert first["anon_session_id"] != first["feed_session_id"]
    assert second["anon_session_id"] == first["anon_session_id"]
    assert second["feed_session_id"] == first["feed_session_id"]


def test_g_anon_session_id_is_populated_for_public_and_authed_paths(monkeypatch):
    """Pins that events._anon_id() no longer falls back to the literal
    "anonymous" inside a request, on either kind of path — it did,
    unconditionally, before this module existed. A custom path is added to
    PUBLIC_PATHS (rather than reusing /healthz or /demo, which are already
    registered by create_app and can't carry a second handler) so the
    public-path branch of the before_request gate can be probed directly."""
    monkeypatch.setattr(
        "jobcannon.web.PUBLIC_PATHS", frozenset({"/healthz", "/demo", "/_probe_public"})
    )

    from jobcannon.host.events import _anon_id

    app = _app()

    @app.get("/_probe_public")
    def probe_public():
        return {"anon_id": _anon_id()}

    @app.get("/_probe_authed")
    def probe_authed():
        return {"anon_id": _anon_id()}

    client = app.test_client()
    public_anon_id = client.get("/_probe_public").get_json()["anon_id"]
    authed_anon_id = client.get("/_probe_authed").get_json()["anon_id"]

    assert public_anon_id != "anonymous"
    assert public_anon_id.startswith("anon_")
    assert authed_anon_id != "anonymous"
    assert authed_anon_id.startswith("anon_")


def test_channel_is_normalized_and_truncated():
    app = _app()
    client = app.test_client()

    resp = client.get("/_probe", query_string={"ref": "Hacker News!!"})
    channel = resp.get_json()["attribution"]["channel"]

    assert re.fullmatch(r"[a-z0-9_-]{1,32}", channel)


def test_channel_over_32_chars_is_truncated_to_32():
    """A 13-char cleaned input alone can pass the regex above whether or not
    the [:32] slice exists in the implementation — force actual truncation so
    this test can fail for the reason its name claims."""
    app = _app()
    client = app.test_client()

    resp = client.get("/_probe", query_string={"ref": "a" * 40 + "!!"})
    channel = resp.get_json()["attribution"]["channel"]

    assert len(channel) == 32
    assert channel == "a" * 32


def test_referrer_is_hostname_only():
    app = _app()
    client = app.test_client()

    resp = client.get("/_probe", headers={"Referer": "https://example.com/path/to/page?q=1&x=2"})
    referrer_host = resp.get_json()["attribution"]["referrer_host"]

    assert referrer_host == "example.com"
    assert "/" not in referrer_host
    assert "?" not in referrer_host
    assert ":" not in referrer_host


def test_malformed_referrer_does_not_500():
    """A malformed Referer (urlsplit raises ValueError on this shape, e.g. an
    unclosed bracketed IPv6 host) must never turn an otherwise-successful
    request into a 500 — capture_attribution() runs on every request,
    including the public-path branch, so a single crafted link would
    otherwise lock a visitor out of the site entirely."""
    app = _app()
    client = app.test_client()

    resp = client.get("/_probe", headers={"Referer": "http://[::1"})

    assert resp.status_code == 200
    assert resp.get_json()["attribution"]["referrer_host"] == "unknown"


def test_referrer_host_is_bounded_to_the_payload_validator_cap():
    """referrer_host is exactly the value a later PR puts in a
    user_signed_up payload; jobcannon/db/events_schema.py's validate_payload
    rejects any string over _MAX_STR chars, so an unbounded capture here
    would already be unemittable by the time it reaches that call. Asserted
    at the point of capture, not only at the point of emission."""
    from jobcannon.db.events_schema import _MAX_STR

    app = _app()
    client = app.test_client()

    long_host = "a" * (_MAX_STR + 100) + ".example.com"
    resp = client.get("/_probe", headers={"Referer": f"https://{long_host}/path"})
    referrer_host = resp.get_json()["attribution"]["referrer_host"]

    assert len(referrer_host) <= _MAX_STR


def test_missing_attribution_is_total():
    app = _app()
    client = app.test_client()

    attribution = client.get("/_probe").get_json()["attribution"]

    assert attribution["channel"] == "direct"
    assert attribution["referrer_host"] == "unknown"


def test_attribution_wave_comes_from_host_config_not_the_environment(monkeypatch):
    """Pins that capture_attribution() reads signup_wave off HOST_CONFIG (the
    one wiring site) rather than re-reading the env var directly, which would
    be a second, competing read of the same setting. Setting the env var to a
    different value than the injected HostConfig proves which one wins."""
    from jobcannon.host.config import HostConfig

    monkeypatch.setenv("JC_SIGNUP_WAVE", "env-value-should-be-ignored")
    double = HostConfig(
        database_url="",
        secret_key="sk_flask_test",
        clerk_sign_up_url="https://clerk.test/sign-up",
        signup_wave="7",
    )
    app = _app(HOST_CONFIG=double)
    client = app.test_client()

    wave = client.get("/_probe").get_json()["attribution"]["wave"]

    assert wave == "7"


def test_host_config_is_available_under_testing():
    """Every existing web test sets TESTING: True (tests/host/test_auth.py)
    without ever injecting HOST_CONFIG — this is the assertion that keeps a
    later route reading app.config["HOST_CONFIG"] from KeyError-ing into a
    500 under all of them."""
    from jobcannon.web import create_app

    app = create_app({"TESTING": True})

    host_config = app.config["HOST_CONFIG"]
    assert isinstance(host_config.clerk_sign_up_url, str)
    assert host_config.clerk_sign_up_url


def test_injected_host_config_wins_over_the_testing_default():
    from jobcannon.host.config import HostConfig
    from jobcannon.web import create_app

    double = HostConfig(
        database_url="",
        secret_key="injected-secret",
        clerk_sign_up_url="https://example.com/custom-sign-up",
        signup_wave="7",
    )
    app = create_app({"TESTING": True, "HOST_CONFIG": double})

    assert app.config["HOST_CONFIG"] is double
    assert app.config["SECRET_KEY"] == "injected-secret"
