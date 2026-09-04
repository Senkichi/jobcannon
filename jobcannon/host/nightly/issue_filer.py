"""ADAPTED from job_finder/web/nightly_monitor/_morning.py (_gh,
_list_open_issues, _file_issue, _issue_number_from_url,
_cross_check_prior_filings) @ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8
(private job-cannon). Ledger L-0387.

GitHub issue filing and dedup-reference fetching for the morning review,
over the GitHub REST API.

# PORT-SEAM: private shelled out to the `gh` CLI (`_gh`, a single
# subprocess seam the driver called for both `issue list` and `issue
# create`). There is no `gh` binary and no local shell on this host, so
# both calls are ported to direct GitHub REST requests
# (`GET /repos/{repo}/issues`, `POST /repos/{repo}/issues`) via `requests`
# (already a pinned dependency; no new dependency added). `repo` and
# `token` are both caller-supplied parameters, not resolved here -- the
# repo comes from JC_NIGHTLY_ISSUE_REPO (config.py) and the token from
# whatever env var wiring PR-B's cron/env-key work adds; this module stays
# a pure REST client so it is testable without either concern.
#
# `automated-ready` is the one hand-off label this design uses (design note
# §6 Q4) -- there is no `chore` label convention carried over.
#
# `_cross_check_prior_filings` (#1506) is ported near-identical, but its
# "prior night" source changes: private read `filed_issues.json` from the
# last few local artifact directories. This host has no local artifact
# directories (no local filesystem logs -- see jobcannon/host/nightly's own
# convention), so the caller (morning_driver.py) threads in `prior_filed`
# from `jobcannon.host.nightly.state`'s `last_filed_issues` field instead --
# `cross_check_prior_filings` itself stays a pure function over whatever
# list it is given, same signature shape as the pure half of the private
# function.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 30
_API_ROOT = "https://api.github.com"
_ISSUE_TITLE_CLIP = 200
_ISSUE_BODY_CLIP = 15_000
_FILED_ISSUE_REASON_CLIP = 300


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def list_open_issues(repo: str, token: str) -> dict:
    """Fetch open issues (not PRs) in *repo* via REST, paginated.

    Returns a self-describing envelope: {"status": "ok" | "unavailable",
    "reason": str | None, "issues": [{"number", "title", "body", "url"}]}.
    Never raises -- every failure mode (network, non-2xx, malformed body)
    is represented by status="unavailable" so a caller can distinguish
    "fetched and truly empty" from "could not fetch" (mirrors private's own
    #1506 rationale: a laundered empty list silently disables dedup).
    """
    issues: list[dict] = []
    url = f"{_API_ROOT}/repos/{repo}/issues"
    params: dict[str, Any] | None = {"state": "open", "per_page": 100}
    try:
        while url:
            resp = requests.get(
                url, headers=_headers(token), params=params, timeout=_REQUEST_TIMEOUT_S
            )
            if resp.status_code != 200:
                reason = f"GitHub issues list returned {resp.status_code}: {resp.text[:300]}"
                logger.warning("nightly review: %s", reason)
                return {"status": "unavailable", "reason": reason, "issues": []}
            page = resp.json()
            if not isinstance(page, list):
                reason = "GitHub issues list returned non-list JSON"
                logger.warning("nightly review: %s", reason)
                return {"status": "unavailable", "reason": reason, "issues": []}
            issues.extend(
                {
                    "number": e.get("number"),
                    "title": e.get("title"),
                    "body": e.get("body"),
                    "url": e.get("html_url"),
                }
                for e in page
                if isinstance(e, dict) and "pull_request" not in e
            )
            # requests parses the RFC 5988 Link header into resp.links;
            # the "next" URL already carries every query param, so drop
            # `params` after the first request to avoid re-appending them.
            url = resp.links.get("next", {}).get("url")
            params = None
    except requests.RequestException as exc:
        reason = f"GitHub issues list failed: {exc}"
        logger.warning("nightly review: %s", reason)
        return {"status": "unavailable", "reason": reason, "issues": []}
    return {"status": "ok", "reason": None, "issues": issues}


def file_issue(repo: str, token: str, title: str, body: str, labels: list[str]) -> dict:
    """File one issue against *repo* via REST.

    Returns {"title", "labels", "outcome" ("created" | "failed"), "url",
    "number", "reason"}. Never raises.
    """
    title = title.strip()[:_ISSUE_TITLE_CLIP]
    body = body.strip()[:_ISSUE_BODY_CLIP]
    record: dict[str, Any] = {
        "title": title,
        "labels": list(labels),
        "outcome": "failed",
        "url": None,
        "number": None,
        "reason": None,
    }
    logger.info(
        "nightly review: file issue repo=%r title=%r labels=%r body_len=%d",
        repo,
        title,
        labels,
        len(body),
    )
    try:
        resp = requests.post(
            f"{_API_ROOT}/repos/{repo}/issues",
            headers=_headers(token),
            json={"title": title, "body": body, "labels": list(labels)},
            timeout=_REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        reason = f"GitHub issue create failed: {exc}"
        logger.warning("nightly review: %s", reason)
        record["reason"] = reason[:_FILED_ISSUE_REASON_CLIP]
        return record
    if resp.status_code != 201:
        reason = f"GitHub issue create returned {resp.status_code}: {resp.text[:300]}"
        logger.warning("nightly review: %s", reason)
        record["reason"] = reason[:_FILED_ISSUE_REASON_CLIP]
        record["status_code"] = resp.status_code
        return record
    data = resp.json() if resp.content else {}
    url = data.get("html_url")
    if not url:
        reason = "GitHub issue create returned no html_url"
        logger.warning("nightly review: %s", reason)
        record["reason"] = reason
        return record
    record["outcome"] = "created"
    record["url"] = url
    record["number"] = data.get("number")
    record["reason"] = None
    return record


def _issue_number_from_url(url: str | None) -> str | None:
    """Extract "NNN" from a GitHub issue URL, or None if unparseable."""
    path_tail = (url or "").rstrip("/").split("/")[-1]
    return path_tail if path_tail.isdigit() else None


def cross_check_prior_filings(open_issues: dict, prior_filed: list[dict]) -> dict:
    """Downgrade a successful open-issues fetch to "unavailable" when
    *prior_filed* (last night's filed-issue records) contains a `created`
    issue none of which appear in the freshly fetched open list (#1506).

    A fetch that reports success but cannot account for issues filed the
    night before is not a trustworthy dedup reference. Returns the
    envelope unchanged when it already failed, when there is no prior
    filing history, or when at least one prior filing is visible.
    """
    if open_issues.get("status") != "ok":
        return open_issues
    fetched_numbers = {
        str(i.get("number"))
        for i in open_issues.get("issues", [])
        if isinstance(i, dict) and i.get("number") is not None
    }
    prior_created = [r for r in prior_filed if r.get("outcome") == "created"]
    if not prior_created:
        return open_issues

    def _prior_number(rec: dict) -> str | None:
        number = rec.get("number") or _issue_number_from_url(rec.get("url"))
        return str(number) if number is not None else None

    visible = [rec for rec in prior_created if _prior_number(rec) in fetched_numbers]
    if visible:
        return open_issues

    refs = [
        f"#{n}" if (n := _prior_number(rec)) else rec.get("url", "unknown") for rec in prior_created
    ]
    reason = f"prior filings not visible in open issue list: {', '.join(refs)}"
    logger.warning(
        "nightly review: open issue list does not contain any of the %d issue(s) "
        "filed the previous night (%s); downgrading fetch to unavailable so "
        "filing is deferred",
        len(prior_created),
        ", ".join(refs),
    )
    return {"status": "unavailable", "reason": reason, "issues": open_issues.get("issues", [])}
