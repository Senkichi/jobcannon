"""Shared HTTP constants for outbound requests.

These constants are imported by `ats_detection.py` and `ats_prober.py`.
Centralizing them here avoids duplicating the same User-Agent and timeout
values across every module that makes an outbound scan/probe request.
"""

# User-Agent + accept headers for crawler/scraper traffic. Identifies the
# project so site operators can attribute traffic.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (compatible; JobFinder/1.0; +https://github.com/job-finder)")
}

# Default timeout (seconds) for external HTTP API calls. Short enough to keep
# the per-request budget bounded in batch crawls, long enough to absorb a
# typical TLS + first-byte round trip on slow careers pages.
_TIMEOUT = 10
