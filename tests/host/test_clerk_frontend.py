"""jobcannon.web.clerk_frontend.frontend_api_host — issue #149's FAPI-host
derivation. Pure function, no Flask/DB needed. Expectations are hand-computed
base64 (not re-derived via base64.b64encode in the test body) so a bug in
the encode/decode symmetry inside the function under test can't cancel out
against the same bug in the test."""

import pytest

from jobcannon.web.clerk_frontend import frontend_api_host


def test_live_key_decodes_to_fapi_host():
    # base64("clerk.jobcannon.dev$") == "Y2xlcmsuam9iY2Fubm9uLmRldiQ=" — the
    # actual production key (jobcannon issue #149 investigation).
    assert frontend_api_host("pk_live_Y2xlcmsuam9iY2Fubm9uLmRldiQ=") == "clerk.jobcannon.dev"


def test_test_key_decodes_to_fapi_host():
    # base64("clerk.test$") == "Y2xlcmsudGVzdCQ="
    assert frontend_api_host("pk_test_Y2xlcmsudGVzdCQ=") == "clerk.test"


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "not-a-clerk-key",
        "sk_live_Y2xlcmsuam9iY2Fubm9uLmRldiQ=",  # secret-key prefix, not publishable
        "pk_live_",  # prefix with nothing after it
        "pk_live_***not-base64***",
        "pk_live_Y2xlcmsuam9iY2Fubm9uLmRldg==",  # valid base64, missing trailing '$'
        "pk_live_JA==",  # decodes to bare "$" -> empty host
    ],
)
def test_malformed_key_raises_value_error(bad_key):
    with pytest.raises(ValueError):
        frontend_api_host(bad_key)
