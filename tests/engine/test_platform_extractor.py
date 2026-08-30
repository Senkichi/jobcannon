"""detect_platform pins (jobcannon/engine/platform_extractor.py).

The classifier is hostname-parsed, not substring-matched — CodeQL's
py/incomplete-url-substring-sanitization flagged the original
`"linkedin.com" in url` form, and these tests pin the structural fix:
only the URL's actual host (linkedin.com or a subdomain) with /jobs/ in
the PATH classifies as linkedin. Everything else falls through to
whole-page extraction (None).
"""

import pytest

from jobcannon.engine.platform_extractor import detect_platform


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/3894732",
        "https://linkedin.com/jobs/view/3894732",
        "https://LINKEDIN.com/JOBS/view/1",  # case-insensitive
    ],
)
def test_linkedin_job_urls_detected(url):
    assert detect_platform(url) == "linkedin"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://example.com/careers/42",
        # Host must actually BE linkedin, not merely contain the string:
        "https://evil.example/linkedin.com/jobs/view/1",
        "https://notlinkedin.com/jobs/view/1",
        # /jobs/ must be in the path, not the query:
        "https://www.linkedin.com/feed/?next=/jobs/view/1",
        # linkedin host without a jobs path is chrome-heavy profile/feed HTML:
        "https://www.linkedin.com/in/someone",
    ],
)
def test_non_job_urls_fall_through(url):
    assert detect_platform(url) is None
