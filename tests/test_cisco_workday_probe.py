"""Test Cisco Workday probe (#555).

Verifies that the Cisco Workday slug "cisco.wd5/Cisco_Careers" resolves to a
live Workday board and returns job postings. This confirms the wiring from
issue #555 is correct.
"""

from __future__ import annotations

import pytest

from jobcannon.engine.ats_prober import _probe_workday


@pytest.mark.integration
def test_cisco_workday_slug_resolves():
    """Cisco's Workday slug should resolve to a live board.

    Marked `integration` because it is exactly what the name says: an HTTP call
    to Cisco's production Workday API. It had no marker from #555 until now, so
    every CI run on every PR since then has hit a third party's servers to
    assert their board still exists. That is not a property of this codebase.
    `addopts` excludes `integration`, so it now runs only on request; the
    lockdown manifest entry lets it use a socket when it does.
    """
    slug = "cisco.wd5/Cisco_Careers"
    assert _probe_workday(slug), f"Cisco Workday slug {slug} should resolve to a live board"


def test_cisco_workday_slug_format():
    """Cisco's Workday slug should follow the correct format."""
    slug = "cisco.wd5/Cisco_Careers"
    parts = slug.split("/", 1)
    assert len(parts) == 2, "Slug should be in 'subdomain/board' format"
    subdomain, board = parts
    assert subdomain == "cisco.wd5", f"Expected subdomain 'cisco.wd5', got '{subdomain}'"
    assert board == "Cisco_Careers", f"Expected board 'Cisco_Careers', got '{board}'"
