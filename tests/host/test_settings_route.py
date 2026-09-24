"""GET /settings + POST /settings/keys + POST /settings/keys/deactivate
(jobcannon/web/settings.py) — issue #332's BYO-key settings UI.

Monkeypatched-module-attribute pattern (tests/host/test_profile_route.py
style): the DAL functions, the crypto helper, and the live-check seam the
route module imported are replaced on the module itself, so no Postgres and
no real provider call are needed; the DB-backed CSRF cases live in
tests/host/test_csrf.py.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone

from flask import url_for
import pytest

from jobcannon.host.credentials import KekNotConfiguredError
from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.settings as settings_module

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="
USER_ID = "user_settings_123"


def _app(verify=lambda req: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID})):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


@pytest.fixture()
def stubs(monkeypatch):
    """Stub every seam the route module touches. Returns a state dict the
    tests inspect: `calls` records (name, args) in invocation order so the
    encrypt -> validate -> upsert ordering is assertable, `creds` is the
    per-provider get_credential answer."""
    state = {
        "creds": {},
        "calls": [],
        "validate_error": None,
        "deactivate_result": True,
    }
    monkeypatch.setattr(
        settings_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(
        settings_module,
        "get_credential",
        lambda conn, user_id, provider: state["creds"].get(provider),
    )

    def fake_encrypt(plaintext):
        state["calls"].append(("encrypt", plaintext))
        return b"encrypted-blob"

    monkeypatch.setattr(settings_module, "encrypt_api_key", fake_encrypt)

    def fake_validate(provider, key):
        state["calls"].append(("validate", provider, key))
        if state["validate_error"] is not None:
            raise state["validate_error"]

    monkeypatch.setattr(settings_module, "validate_api_key", fake_validate)

    def fake_upsert(conn, user_id, provider, encrypted_key):
        state["calls"].append(("upsert", user_id, provider, encrypted_key))

    monkeypatch.setattr(settings_module, "upsert_credential", fake_upsert)

    def fake_deactivate(conn, user_id, provider):
        state["calls"].append(("deactivate", user_id, provider))
        return state["deactivate_result"]

    monkeypatch.setattr(settings_module, "deactivate_credential", fake_deactivate)
    return state


def _cred(**overrides):
    cred = {
        "provider": "gemini",
        "encrypted_key": b"cipher",
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "last_used_at": datetime.now(timezone.utc),
    }
    cred.update(overrides)
    return cred


# --- routing / auth -------------------------------------------------------


def test_unauthenticated_get_and_posts_are_401(stubs):
    client = _app(verify=lambda req: None).test_client()

    assert client.get("/settings").status_code == 401
    assert (
        client.post("/settings/keys", data={"provider": "gemini", "api_key": "k"}).status_code
        == 401
    )
    assert client.post("/settings/keys/deactivate", data={"provider": "gemini"}).status_code == 401
    assert stubs["calls"] == []


def test_url_for_settings_routes_are_exact_literals():
    """base.html's nav link is the literal "/settings" (same convention as
    the "/profile" link, which test_profile_route.py pins the same way) —
    these pins keep both literals honest."""
    app = _app()
    with app.test_request_context("/"):
        assert url_for("settings.index") == "/settings"
        assert url_for("settings.save_key") == "/settings/keys"
        assert url_for("settings.deactivate_key") == "/settings/keys/deactivate"


# --- GET ------------------------------------------------------------------


def test_get_renders_each_provider_status(stubs):
    stubs["creds"] = {
        "gemini": _cred(provider="gemini", is_active=True),
        "groq": _cred(provider="groq", is_active=False, last_used_at=None),
        # cerebras: no row
    }
    html = _app().test_client().get("/settings").get_data(as_text=True)

    assert 'data-settings-provider="gemini"' in html
    assert 'data-settings-provider="groq"' in html
    assert 'data-settings-provider="cerebras"' in html
    assert "Gemini API key" in html
    assert "Groq API key" in html
    assert "Cerebras API key" in html
    assert "Active" in html
    assert "Deactivated" in html
    assert "Not set" in html
    # Deactivate control only renders for a configured, active row.
    assert html.count("data-settings-deactivate") == 1


def test_get_renders_nav_link(stubs):
    html = _app().test_client().get("/settings").get_data(as_text=True)
    assert 'href="/settings"' in html
    assert "data-settings-nav-link" in html


def test_get_read_failure_renders_unavailable_without_forms(stubs, monkeypatch):
    def boom():
        raise RuntimeError("pool down")

    monkeypatch.setattr(settings_module, "connection_factory", boom)
    html = _app().test_client().get("/settings").get_data(as_text=True)

    assert "data-settings-unavailable" in html
    assert "data-settings-provider" not in html


def test_get_saved_and_deactivated_params_render_stamps(stubs):
    client = _app().test_client()
    html = client.get("/settings?saved=gemini&deactivated=groq").get_data(as_text=True)

    assert "data-settings-saved" in html
    assert "Gemini API key verified and saved." in html
    assert "data-settings-deactivated" in html
    assert "Groq API key deactivated." in html


def test_get_unknown_stamp_params_render_no_stamp(stubs):
    html = (
        _app().test_client().get("/settings?saved=bogus&deactivated=bogus").get_data(as_text=True)
    )
    assert "data-settings-saved" not in html
    assert "data-settings-deactivated" not in html


# --- POST /settings/keys --------------------------------------------------


def test_post_save_key_encrypts_validates_then_upserts_in_order(stubs):
    resp = (
        _app()
        .test_client()
        .post("/settings/keys", data={"provider": "groq", "api_key": "gsk_live_key"})
    )

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/settings?saved=groq")
    names = [c[0] for c in stubs["calls"]]
    assert names == ["encrypt", "validate", "upsert"]
    assert ("encrypt", "gsk_live_key") in stubs["calls"]
    assert ("validate", "groq", "gsk_live_key") in stubs["calls"]
    assert ("upsert", USER_ID, "groq", b"encrypted-blob") in stubs["calls"]


def test_post_save_key_strips_surrounding_whitespace(stubs):
    _app().test_client().post(
        "/settings/keys", data={"provider": "gemini", "api_key": "  sk-padded  "}
    )
    assert ("validate", "gemini", "sk-padded") in stubs["calls"]


def test_post_save_key_rotation_uses_same_upsert_path(stubs):
    """Rotation is not a separate flow: re-upserting a provider with an
    existing (even deactivated) row re-activates it inside
    upsert_credential — the route just has to reach that call."""
    stubs["creds"]["groq"] = _cred(provider="groq", is_active=False)
    resp = (
        _app()
        .test_client()
        .post("/settings/keys", data={"provider": "groq", "api_key": "gsk_new_key"})
    )
    assert resp.status_code == 303
    assert ("upsert", USER_ID, "groq", b"encrypted-blob") in stubs["calls"]


def test_post_save_key_unknown_provider_is_400_and_stores_nothing(stubs):
    resp = (
        _app().test_client().post("/settings/keys", data={"provider": "ollama", "api_key": "sk-x"})
    )
    assert resp.status_code == 400
    assert stubs["calls"] == []


def test_post_save_key_blank_key_errors_and_stores_nothing(stubs):
    resp = (
        _app().test_client().post("/settings/keys", data={"provider": "gemini", "api_key": "   "})
    )
    assert resp.status_code == 200
    assert "data-settings-error" in resp.get_data(as_text=True)
    assert stubs["calls"] == []


def test_post_save_key_oversized_key_errors_and_stores_nothing(stubs):
    resp = (
        _app()
        .test_client()
        .post(
            "/settings/keys",
            data={"provider": "gemini", "api_key": "k" * (settings_module._MAX_KEY_LENGTH + 1)},
        )
    )
    assert resp.status_code == 200
    assert "data-settings-error" in resp.get_data(as_text=True)
    assert stubs["calls"] == []


def test_post_save_key_failed_live_check_stores_nothing(stubs):
    """The issue's core gate: a key that fails the live provider round-trip
    is never upserted."""
    stubs["validate_error"] = RuntimeError("401 Unauthorized")
    resp = (
        _app()
        .test_client()
        .post("/settings/keys", data={"provider": "gemini", "api_key": "sk-bad"})
    )

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "data-settings-error" in html
    # The apostrophe in "didn't" renders escaped (&#39;) under Jinja's
    # autoescape — assert the unescaped-fragment half of the message.
    assert "pass a live check" in html
    assert not any(c[0] == "upsert" for c in stubs["calls"])


def test_post_save_key_live_check_error_never_echoes_the_key(stubs):
    """Plaintext hygiene: even when the provider's error text contains the
    submitted key (upstreams sometimes echo request material), the rendered
    page and nothing else may carry it back out."""
    stubs["validate_error"] = RuntimeError("key sk-secret-123 was rejected")
    resp = (
        _app()
        .test_client()
        .post("/settings/keys", data={"provider": "gemini", "api_key": "sk-secret-123"})
    )
    html = resp.get_data(as_text=True)
    assert "sk-secret-123" not in html
    assert "[redacted]" in html


def test_post_save_key_kek_unavailable_is_502_and_stores_nothing(stubs, monkeypatch):
    def boom(plaintext):
        raise KekNotConfiguredError("JC_BYO_KEY_KEK is not set")

    monkeypatch.setattr(settings_module, "encrypt_api_key", boom)
    resp = (
        _app().test_client().post("/settings/keys", data={"provider": "gemini", "api_key": "sk-x"})
    )

    assert resp.status_code == 502
    html = resp.get_data(as_text=True)
    assert "data-settings-error" in html
    assert "sk-x" not in html
    # The live check is never reached when the key cannot be encrypted.
    assert not any(c[0] in ("validate", "upsert") for c in stubs["calls"])


def test_post_save_key_error_rerender_shows_current_statuses(stubs):
    """An error re-render is the same page minus nothing: provider statuses
    are re-read so a failed save doesn't blank the list."""
    stubs["creds"]["gemini"] = _cred(provider="gemini", is_active=True)
    stubs["validate_error"] = RuntimeError("bad key")
    html = (
        _app()
        .test_client()
        .post("/settings/keys", data={"provider": "gemini", "api_key": "sk-bad"})
        .get_data(as_text=True)
    )
    assert "data-settings-provider" in html
    assert "Active" in html


# --- POST /settings/keys/deactivate ---------------------------------------


def test_post_deactivate_calls_dal_and_redirects(stubs):
    stubs["creds"]["groq"] = _cred(provider="groq", is_active=True)
    resp = _app().test_client().post("/settings/keys/deactivate", data={"provider": "groq"})

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/settings?deactivated=groq")
    assert stubs["calls"] == [("deactivate", USER_ID, "groq")]


def test_post_deactivate_unknown_provider_is_400(stubs):
    resp = _app().test_client().post("/settings/keys/deactivate", data={"provider": "ollama"})
    assert resp.status_code == 400
    assert stubs["calls"] == []


def test_post_deactivate_no_row_renders_error(stubs):
    stubs["deactivate_result"] = False
    resp = _app().test_client().post("/settings/keys/deactivate", data={"provider": "gemini"})

    assert resp.status_code == 200
    assert "nothing to deactivate" in resp.get_data(as_text=True)
