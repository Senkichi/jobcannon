# PORTED from tests/test_pii_scrub.py @ 28c03ab79e04e417741d77f8f181909acedf671f (private job-cannon). Ledger L-0043.
import logging

import requests

from jobcannon.engine._pii_scrub import (
    DEFAULT_DENYLIST,
    SENSITIVE_QUERY_PARAMS,
    install_credential_redaction_filter,
    redact_url_secrets,
    scrub_text,
)


def test_removes_to_header_lines():
    # PORT-SEAM: private fixture used the owner's own handle as the example
    # recipient; a neutral placeholder is used here instead (never write
    # owner-identity literals into the public repo).
    raw = "From: jobs@x.com\nTo: applicant@example.com\nSubject: hi\nBody here"
    out = scrub_text(raw)
    # PORT-SEAM: placeholder recipient (see note above).
    assert "To: applicant@example.com" not in out
    assert "Body here" in out


def test_redacts_caller_supplied_identifiers_case_insensitively():
    # PORT-SEAM: private test exercised the owner's own name/handle as the
    # caller-supplied identifiers (they also seeded DEFAULT_DENYLIST there);
    # DEFAULT_DENYLIST ships empty publicly (see module docstring PORT-SEAM),
    # so this uses neutral placeholder identifiers instead.
    out = scrub_text("Hello Alice and Alicesmith", identifiers=["alice", "alicesmith"])
    assert "alice" not in out.lower()
    assert "[redacted]" in out.lower()


def test_redacts_bare_emails():
    out = scrub_text("reach me at jane.doe@gmail.com please")
    assert "jane.doe@gmail.com" not in out
    assert "please" in out


def test_default_denylist_is_iterable_of_str():
    # PORT-SEAM: DEFAULT_DENYLIST ships empty publicly — no single tenant's
    # identity belongs in shared code (see module docstring PORT-SEAM). This
    # is the public invariant under test: it fails loudly if someone re-seeds
    # an owner identifier into the shared default.
    assert DEFAULT_DENYLIST == ()


def test_redact_url_secrets_redacts_api_key():
    text = "error for url: https://serpapi.com/search.json?api_key=SECRET&hl=en&start=0"
    out = redact_url_secrets(text)
    assert "SECRET" not in out
    assert "api_key=[REDACTED]" in out
    assert "hl=en" in out
    assert "start=0" in out


def test_redact_url_secrets_redacts_all_sensitive_params():
    url = (
        "https://example.com/search?"
        "api_key=AK&app_key=BK&app_id=AI&token=TOK&access_token=ATOK&key=K&other=keep"
    )
    out = redact_url_secrets(url)
    assert "AK" not in out
    assert "BK" not in out
    assert "AI" not in out
    assert "TOK" not in out
    assert "ATOK" not in out
    assert "K" not in out
    assert "other=keep" in out
    assert out.count("[REDACTED]") == len(SENSITIVE_QUERY_PARAMS)


def test_redact_url_secrets_is_idempotent():
    url = "https://example.com?api_key=SECRET"
    once = redact_url_secrets(url)
    twice = redact_url_secrets(once)
    assert once == twice


def test_redact_url_secrets_leaves_non_url_text_untouched():
    text = "No URLs here, just a secret token xyz and api_key=SECRET"
    assert redact_url_secrets(text) == text


def test_credential_filter_redacts_http_error_url(caplog):
    # PORT-SEAM: prefix rewritten "job_finder" -> "jobcannon" (see
    # _pii_scrub.py PORT-SEAM on install_credential_redaction_filter).
    install_credential_redaction_filter("jobcannon")
    logger = logging.getLogger("jobcannon.engine.test")
    response = requests.Response()
    response.url = "https://serpapi.com/search.json?api_key=UNIQUE_SECRET_TOKEN&hl=en"
    response.status_code = 429
    response.reason = "Too Many Requests"
    error = requests.HTTPError(
        "429 Client Error: Too Many Requests for url: " + response.url,
        response=response,
    )
    logger.warning("SerpAPI search failed for 'Data Analyst' (page 0): %s", error)
    assert "UNIQUE_SECRET_TOKEN" not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_redact_url_secrets_handles_malformed_url_like_text():
    """A URL-like substring with unmatched brackets must not raise.

    Regression for CI failure: ``urlparse`` raises ``ValueError: Invalid IPv6
    URL`` on ``https://x']`` (greedy ``\\S+`` captures the trailing ``']``
    from surrounding list repr text).  The redactor must skip un-parseable
    matches rather than crash the logging pipeline.
    """
    text = "Schema validation failed: ['Data Scientist', 'https://x'] is not of type 'object'"
    out = redact_url_secrets(text)
    # No exception, text preserved (no query string to redact anyway).
    assert "https://x" in out
    assert out == text


def test_credential_filter_never_raises_on_malformed_url(caplog):
    """The logging filter must not raise on malformed URL-like log messages.

    A raising filter aborts ``Logger.handle`` and masks the original error —
    the exact CI failure was ``ValueError: Invalid IPv6 URL`` replacing the
    real schema-validation warning.
    """
    # PORT-SEAM: prefix rewritten "job_finder" -> "jobcannon".
    install_credential_redaction_filter("jobcannon")
    logger = logging.getLogger("jobcannon.engine.malformed_test")
    # Simulate the CI failure: a log message whose str() contains a
    # bracket-terminated URL-like fragment.
    logger.warning("Result %r is not valid", ["Data Scientist", "https://x"])
    # The record must reach the handler (no exception propagated).
    assert "Data Scientist" in caplog.text
