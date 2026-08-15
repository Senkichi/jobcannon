"""Jobvite ATS platform scanner — stub for URL detection only.

**Why a stub?** Jobvite does not expose an unauthenticated public
job-board API. The hosted career sites at ``jobs.jobvite.com/{slug}``
serve HTML (often redirecting to a tenant-custom domain like
``careers.victaulic.com``) without embedded structured data
(no JSON-LD, no Inertia/Next.js initial state, no JSON-API endpoint).
A scraper would need per-tenant HTML parsing and is too fragile to
ship without significant per-tenant testing.

**What this module provides:**

- A ``SCANNER`` constant that returns an empty list for every call.
  Companies tagged ``ats_platform='jobvite'`` will be "scanned" but
  produce zero jobs; they fall back to whatever non-ATS feed populates
  them (Gmail alerts, DataForSEO, etc.).
- A probe (``_probe_jobvite`` in ``ats_prober.py``) that checks the
  hosted career page returns 200, so URL-evidence promotion via the
  B2 fast-path works.
- A URL detection regex (``_JOBVITE_HUMAN_URL`` in
  ``ats_detection.py``) so ``jobs.jobvite.com/{slug}`` careers URLs
  reconcile to ``('jobvite', slug)``.

**Follow-up work** (see FOLLOWUPS round 6+): Build a real scraper. The
audit identified 7 companies with ``jobs.jobvite.com`` careers URLs
(Victaulic, Capcom, ASH, The Institutes, Havas, PulsePoint,
NeoGenomics). Most redirect to custom domains; the scraper will likely
need to follow redirects and parse the destination HTML using a
per-tenant fingerprint.
"""

from __future__ import annotations

from jobcannon.engine.ats_platforms._registry import PlatformScanner


def _fetch_postings(_slug: str, max_pages: int | None = None) -> list[dict]:
    """Returns []. See module docstring for the stub rationale.

    Args:
        max_pages: Unused (this scanner is a permanent stub). Accepted for
            signature compatibility with ``run_platform_scan``, which
            unconditionally calls ``scanner.fetch_postings(slug,
            max_pages=max_pages)``.
    """
    return []


def _posting_to_job(_posting: dict, _slug: str) -> dict:
    """Never called -- _fetch_postings always returns []."""
    return {}


SCANNER = PlatformScanner(
    name="jobvite",
    company_source="Jobvite",
    fetch_postings=_fetch_postings,
    title_of=lambda _posting: "",
    posting_to_job=_posting_to_job,
)
