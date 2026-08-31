"""PostHog admin-API person purge (issue #135): deletes the PostHog person
record for a user's PSEUDONYMOUS distinct_id (never the raw Clerk user id —
`jobcannon.host.posthog_client.pseudonymize` is the only producer of the
value passed in here) via PostHog's private/admin REST API, on account
deletion.

Distinct host from ingestion (jobcannon#137-adjacent gotcha, verified
against PostHog's own docs 2026-08): `POSTHOG_HOST`
(`jobcannon/host/posthog_client.py`, `https://eu.i.posthog.com`) is the
EU INGESTION endpoint (`capture`/`batch`) — it does not serve the private
REST/admin API used here (`/api/projects/:id/persons/...`), which lives on
`https://eu.posthog.com` instead. Rather than deriving one host string from
the other with a `.i.` substring transform (fragile: silently wrong if
PostHog ever restructures either hostname, and a wrong-host purge fails
soft by design below, so a bad derivation could go unnoticed indefinitely),
this module takes the admin host as its OWN explicit, committed-literal
config value (`POSTHOG_ADMIN_API_HOST`, render.yaml) — same "routing
choice, not a secret" precedent as `POSTHOG_HOST` itself.

Deliberately fail-soft when the three admin-API values are not ALL
configured: an unset personal API key / project id / host is an expected,
PERMANENT state until the owner sets them (see the #135 PR body — the
published privacy policy keeps disclosing surviving PostHog copies until
then). `purge_person()` returns a "skipped" status and logs ONCE per
process (not per call) rather than raising.

A genuine HTTP failure (network error, non-2xx from PostHog) DOES raise —
that's fine specifically because `purge_person()` only ever runs inside
`jobcannon.host.tasks.purge_posthog_person`, an async procrastinate task on
the worker's own `maintenance` queue, never inline in the webhook request
thread: by the time this task even runs, the local deletion cascade
(`jobcannon.host.user_deletion.cascade_delete_user`) has already committed,
so a raised exception here cannot block or delay account deletion — it only
fails that one procrastinate job. That job is not fire-and-forget:
`purge_posthog_person` is registered with
`RetryStrategy(max_attempts=5, linear_wait=30)` (jobcannon.host.tasks), so a
transient HTTP failure here is retried (up to 5 attempts total, 30s linear
backoff) before the job is finally logged as failed (matches this module's
general fail-soft-on-non-essential posture only once retries are exhausted).
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_S = 10

_personal_api_key: str | None = None
_project_id: str | None = None
_api_host: str | None = None
_logged_unset_once = False


def configure(*, personal_api_key: str | None, project_id: str | None, host: str | None) -> None:
    """Wiring seam (jobcannon.host.wiring.init_engine_seams, seam 4).
    Called with all-None on teardown to reset module state between app
    instances — mirrors jobcannon.host.posthog_client.set_analytics_salt's
    reset contract, and the same reason: module globals leak across tests
    (and across create_app() calls in one process) without an explicit
    reset."""
    global _personal_api_key, _project_id, _api_host, _logged_unset_once
    _personal_api_key = personal_api_key
    _project_id = project_id
    _api_host = host
    _logged_unset_once = False


def is_configured() -> bool:
    """Public predicate (used by jobcannon.host.wiring's seam-threading
    test to assert configuration without triggering a real HTTP call)."""
    return bool(_personal_api_key and _project_id and _api_host)


def purge_person(distinct_id: str) -> dict:
    """Delete the PostHog person matching `distinct_id`, including their
    events (`delete_events=true`). Returns a status dict:
    {"status": "deleted", "person_id": ...} | {"status": "not_found"} |
    {"status": "skipped", "reason": "unconfigured"}. Raises on a genuine
    HTTP failure (see module docstring for why that's safe here)."""
    global _logged_unset_once
    if not is_configured():
        if not _logged_unset_once:
            logger.info(
                "posthog_admin.purge_person: POSTHOG_PERSONAL_API_KEY / "
                "POSTHOG_PROJECT_ID / POSTHOG_ADMIN_API_HOST not fully "
                "configured -- skipping PostHog person purge (the published "
                "privacy policy still discloses surviving PostHog copies "
                "until all three are set; see issue #135)"
            )
            _logged_unset_once = True
        return {"status": "skipped", "reason": "unconfigured"}

    headers = {"Authorization": f"Bearer {_personal_api_key}"}
    base = f"{_api_host.rstrip('/')}/api/projects/{_project_id}/persons/"
    resp = requests.get(
        base, params={"distinct_id": distinct_id}, headers=headers, timeout=_REQUEST_TIMEOUT_S
    )
    resp.raise_for_status()
    results = resp.json().get("results") or []
    if not results:
        return {"status": "not_found"}

    person = results[0]
    # Defensive: only delete if the returned person actually lists this
    # distinct_id (PostHog's lookup can return a merged/adjacent person on
    # an unexpected match) -- deleting the wrong person's events is strictly
    # worse than not purging at all.
    if distinct_id not in (person.get("distinct_ids") or []):
        logger.warning(
            "posthog_admin.purge_person: distinct_id lookup returned a "
            "person that does not list it in distinct_ids -- refusing to "
            "delete (fail closed on a mismatched lookup)"
        )
        return {"status": "not_found", "reason": "distinct_id_mismatch"}

    person_id = person["id"]
    del_resp = requests.delete(
        f"{base}{person_id}/",
        params={"delete_events": "true"},
        headers=headers,
        timeout=_REQUEST_TIMEOUT_S,
    )
    del_resp.raise_for_status()
    return {"status": "deleted", "person_id": person_id}
