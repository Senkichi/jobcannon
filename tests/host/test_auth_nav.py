"""base.html's header sign-in/sign-up nav (issue #145): before this, every
signed-out surface (/, /start, /preview, /demo, /privacy, the 401 page) had
zero link to Clerk's hosted sign-up/sign-in pages, so a visitor who
completed the /start -> /preview funnel had no discoverable path to create
an account.

The nav is gated on `not g.clerk_user` in base.html, not on request.path —
the same auth-state signal the footer's existing export/delete links use
(`{% if g.clerk_user %}`) — so it renders on every public page AND the 401
page (both leave g.clerk_user unset/None) and hides itself on an authed
page. Each of the two links (clerk_sign_up_url / clerk_sign_in_url) is
independently optional: an unset URL renders nothing, never a bare href="".

No Postgres needed: these hit either the errorhandler (no DB), the
/privacy route (jobcannon.web.legal, no DB), or a custom throwaway route
registered the same way test_auth.py's `/private` tests do — same shape as
tests/host/test_clerk_loader_template.py."""

import logging

import pytest

from jobcannon.host.config import HostConfig
from jobcannon.web import _signup_cta_url, _warn_if_auth_links_unset, create_app
from jobcannon.web.auth import ClerkIdentity

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="
_SIGN_UP_URL = "https://accounts.jobcannon.test/sign-up"
_SIGN_IN_URL = "https://accounts.jobcannon.test/sign-in"


def _host_config(**overrides) -> HostConfig:
    fields = dict(database_url="", secret_key="testing-secret-key")
    fields.update(overrides)
    return HostConfig(**fields)


def _app(host_config: HostConfig, verify):
    return create_app(
        config={
            "TESTING": True,
            "HOST_CONFIG": host_config,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def test_public_page_shows_both_links_when_both_urls_configured():
    app = _app(
        _host_config(clerk_sign_up_url=_SIGN_UP_URL, clerk_sign_in_url=_SIGN_IN_URL),
        verify=lambda req: None,
    )
    html = app.test_client().get("/privacy").get_data(as_text=True)

    assert f'href="{_SIGN_UP_URL}"' in html
    assert f'href="{_SIGN_IN_URL}"' in html
    assert ">Sign up<" in html
    assert ">Sign in<" in html


def test_401_page_shows_both_links_when_both_urls_configured():
    """The 401 page is the OTHER signed-out surface the header nav must
    cover — g.clerk_user is set to None before abort(401)
    (jobcannon/web/__init__.py's clerk_auth), same as a PUBLIC_PATHS
    request, so the same `not g.clerk_user` gate renders it here too.

    error_401.html's OWN inline paragraph (distinct from base.html's header
    nav) also gates each link on its own URL and independently emits the
    matching text ("Sign in" / "sign up") — asserted by requiring AT LEAST
    two occurrences of each href (header + content block), not merely one,
    so a broken content-block link can't hide behind the header nav's
    identical href satisfying the same substring check. The two
    content-block-only tests below (sign-up-url-unset / sign-in-url-unset)
    are the tolerant-default mirror pair for this block specifically."""
    app = _app(
        _host_config(clerk_sign_up_url=_SIGN_UP_URL, clerk_sign_in_url=_SIGN_IN_URL),
        verify=lambda req: None,
    )
    resp = app.test_client().get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 401
    assert html.count(f'href="{_SIGN_UP_URL}"') >= 2
    assert html.count(f'href="{_SIGN_IN_URL}"') >= 2
    assert ">Sign in</a>" in html
    assert ">sign up</a>" in html


def test_401_page_content_block_omits_sign_up_link_when_sign_up_url_unset():
    """Tolerant defaults on the 401 page's OWN content block (distinct from
    the header nav, which test_public_page_omits_sign_up_link_when_sign_up_url_unset
    already covers): clerk_sign_up_url unset must drop the lowercase 'sign up'
    link and the 'or' separator, leaving a lone 'Sign in' link — never a bare
    href=""."""
    app = _app(_host_config(clerk_sign_in_url=_SIGN_IN_URL), verify=lambda req: None)
    html = app.test_client().get("/").get_data(as_text=True)

    assert f'href="{_SIGN_IN_URL}"' in html
    assert ">Sign in</a>" in html
    assert ">sign up</a>" not in html
    assert 'href=""' not in html


def test_401_page_content_block_omits_sign_in_link_when_sign_in_url_unset():
    """Mirror of the test above: clerk_sign_in_url unset must drop the
    'Sign in' link and the 'or' separator from the 401 page's own content
    block, leaving a lone lowercase 'sign up' link."""
    app = _app(_host_config(clerk_sign_up_url=_SIGN_UP_URL), verify=lambda req: None)
    html = app.test_client().get("/").get_data(as_text=True)

    assert f'href="{_SIGN_UP_URL}"' in html
    assert ">sign up</a>" in html
    assert ">Sign in</a>" not in html
    assert 'href=""' not in html


def test_authed_page_hides_the_header_nav():
    """Negative control: a signed-in visitor doesn't need a sign-in/sign-up
    prompt — the nav must be absent even though both URLs are configured,
    proving the gate is `not g.clerk_user`, not "URLs are set"."""
    app = _app(
        _host_config(clerk_sign_up_url=_SIGN_UP_URL, clerk_sign_in_url=_SIGN_IN_URL),
        verify=lambda req: ClerkIdentity(user_id="user_123", claims={"sub": "user_123"}),
    )

    @app.get("/render-base")
    def render_base():
        from flask import render_template

        return render_template("base.html")

    html = app.test_client().get("/render-base").get_data(as_text=True)

    assert "data-auth-nav" not in html
    assert _SIGN_UP_URL not in html
    assert _SIGN_IN_URL not in html


def test_public_page_omits_sign_in_link_when_sign_in_url_unset():
    """Tolerant defaults, first state: clerk_sign_in_url unset (dataclass
    default "") must render nothing for that link specifically — never a
    bare href="" — while the configured sign-up link still renders."""
    app = _app(_host_config(clerk_sign_up_url=_SIGN_UP_URL), verify=lambda req: None)
    html = app.test_client().get("/privacy").get_data(as_text=True)

    assert f'href="{_SIGN_UP_URL}"' in html
    assert ">Sign in<" not in html
    assert 'href=""' not in html


def test_public_page_omits_sign_up_link_when_sign_up_url_unset():
    """Tolerant defaults, second state: the mirror image of the test above —
    clerk_sign_up_url unset, clerk_sign_in_url configured."""
    app = _app(_host_config(clerk_sign_in_url=_SIGN_IN_URL), verify=lambda req: None)
    html = app.test_client().get("/privacy").get_data(as_text=True)

    assert f'href="{_SIGN_IN_URL}"' in html
    assert ">Sign up<" not in html
    assert 'href=""' not in html


def test_public_page_renders_neither_link_when_both_urls_unset():
    """Both blank (the bare HostConfig default) must render the nav
    container with neither link, never a bare href="" for either."""
    app = _app(_host_config(), verify=lambda req: None)
    html = app.test_client().get("/privacy").get_data(as_text=True)

    assert ">Sign up<" not in html
    assert ">Sign in<" not in html
    assert 'href=""' not in html


def test_warn_if_auth_links_unset_logs_one_warning_per_missing_url(caplog):
    """Non-fatal boot-time signal (unlike CLERK_PUBLISHABLE_KEY/WEBHOOK_SECRET's
    fail-fast): a blank URL degrades the render silently everywhere else in
    this module's tests, so the warning is the ONLY observable signal that
    issue #145 has silently regressed. Both fields unset -> both warnings,
    each naming its own env var."""
    with caplog.at_level(logging.WARNING, logger="jobcannon.web"):
        _warn_if_auth_links_unset(_host_config())

    assert any("CLERK_SIGN_UP_URL" in r.message for r in caplog.records)
    assert any("CLERK_SIGN_IN_URL" in r.message for r in caplog.records)


def test_warn_if_auth_links_unset_silent_when_both_configured(caplog):
    """Positive control for the test above: both URLs set -> zero warnings,
    proving the check doesn't fire unconditionally at import/call time."""
    with caplog.at_level(logging.WARNING, logger="jobcannon.web"):
        _warn_if_auth_links_unset(
            _host_config(clerk_sign_up_url=_SIGN_UP_URL, clerk_sign_in_url=_SIGN_IN_URL)
        )

    assert caplog.records == []


@pytest.mark.parametrize(
    ("case_id", "is_anonymous", "sign_up_url", "sign_in_url", "expected"),
    [
        ("anonymous, sign-up set", True, _SIGN_UP_URL, _SIGN_IN_URL, _SIGN_UP_URL),
        ("anonymous, only sign-in set", True, "", _SIGN_IN_URL, _SIGN_IN_URL),
        ("anonymous, neither set", True, "", "", None),
        ("authed, sign-up set", False, _SIGN_UP_URL, _SIGN_IN_URL, None),
    ],
)
def test_signup_cta_url_gates_purely_on_anonymity(
    case_id, is_anonymous, sign_up_url, sign_in_url, expected
):
    """The pure derivation half of issue #174's fix (jobcannon/web/__init__.py's
    _signup_cta_url): sign-up preferred, sign-in fallback, but ONLY when the
    caller says the visitor is anonymous -- an authed visitor gets None
    regardless of what's configured, never the fallback chain. This is the
    same value _posting_row.html, preview.html, and demo.html all now gate
    on, replacing each template's own `clerk_sign_up_url or
    clerk_sign_in_url` fallback. Identity resolution itself (the
    is_anonymous input) is covered separately by the authed/anonymous
    request-level tests throughout this module and test_demo_feed.py /
    test_preview.py / test_feed_events.py -- this test only proves the pure
    function's four branches."""
    host_config = _host_config(clerk_sign_up_url=sign_up_url, clerk_sign_in_url=sign_in_url)

    assert _signup_cta_url(host_config, is_anonymous=is_anonymous) == expected, case_id


def test_auth_link_context_tolerates_a_bare_host_config_double():
    """Regression guard mirroring
    tests/host/test_pages.py::test_footer_source_link_tolerates_a_bare_host_config_double:
    _inject_auth_links runs on EVERY request, including the 401 path, so a
    HOST_CONFIG double that predates clerk_sign_in_url entirely (a bare
    types.SimpleNamespace carrying only clerk_sign_up_url, the exact shape
    tests/host/test_empty_states.py uses) must not raise AttributeError."""
    import types

    host_config = types.SimpleNamespace(clerk_sign_up_url=_SIGN_UP_URL)
    app = create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": lambda req: None,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
            "HOST_CONFIG": host_config,
        }
    )

    resp = app.test_client().get("/")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 401
    assert f'href="{_SIGN_UP_URL}"' in html
    assert ">Sign in<" not in html
