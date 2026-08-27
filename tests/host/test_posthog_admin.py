"""jobcannon.host.posthog_admin — issue #135's PostHog person-purge admin
API client. No real HTTP: `requests.get`/`requests.delete` are monkeypatched
at the module's own `requests` reference, mirroring how other suites fake
network layers without a real server."""

from __future__ import annotations

import logging

import pytest
import requests

from jobcannon.host import posthog_admin


class _FakeResponse:
    def __init__(self, *, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}", response=self)


def _configure(monkeypatch, *, key="pk_pers_x", project="12345", host="https://eu.posthog.com"):
    posthog_admin.configure(personal_api_key=key, project_id=project, host=host)


def test_is_configured_false_by_default():
    assert posthog_admin.is_configured() is False


def test_is_configured_true_once_all_three_set():
    posthog_admin.configure(
        personal_api_key="pk_pers_x", project_id="12345", host="https://eu.posthog.com"
    )
    assert posthog_admin.is_configured() is True


def test_is_configured_false_if_any_one_missing():
    posthog_admin.configure(
        personal_api_key="pk_pers_x", project_id=None, host="https://eu.posthog.com"
    )
    assert posthog_admin.is_configured() is False


def test_purge_person_skips_when_unconfigured(caplog):
    caplog.set_level(logging.INFO, logger="jobcannon.host.posthog_admin")

    result = posthog_admin.purge_person("pseudo_abc")

    assert result == {"status": "skipped", "reason": "unconfigured"}


def test_purge_person_logs_unconfigured_exactly_once(caplog):
    caplog.set_level(logging.INFO, logger="jobcannon.host.posthog_admin")

    posthog_admin.purge_person("pseudo_abc")
    posthog_admin.purge_person("pseudo_def")

    unset_logs = [r for r in caplog.records if "not fully" in r.message]
    assert len(unset_logs) == 1


def test_purge_person_not_found_when_no_results(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(payload={"results": []}))

    result = posthog_admin.purge_person("pseudo_missing")

    assert result == {"status": "not_found"}


def test_purge_person_deletes_when_distinct_id_matches(monkeypatch):
    _configure(monkeypatch)
    delete_calls = []

    def _fake_get(url, params=None, headers=None, timeout=None):
        assert params["distinct_id"] == "pseudo_hit"
        return _FakeResponse(
            payload={
                "results": [{"id": "person_1", "distinct_ids": ["pseudo_hit", "pseudo_hit_alt"]}]
            }
        )

    def _fake_delete(url, params=None, headers=None, timeout=None):
        delete_calls.append((url, params))
        return _FakeResponse(status_code=200)

    monkeypatch.setattr(requests, "get", _fake_get)
    monkeypatch.setattr(requests, "delete", _fake_delete)

    result = posthog_admin.purge_person("pseudo_hit")

    assert result == {"status": "deleted", "person_id": "person_1"}
    assert len(delete_calls) == 1
    url, params = delete_calls[0]
    assert url.endswith("/persons/person_1/")
    assert params == {"delete_events": "true"}


def test_purge_person_refuses_delete_on_distinct_id_mismatch(monkeypatch):
    """Defensive check: a lookup that returns a person NOT actually listing
    the requested distinct_id must never be deleted -- fail closed rather
    than risk purging the wrong person's data on a fuzzy PostHog match."""
    _configure(monkeypatch)
    delete_calls = []

    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResponse(
            payload={"results": [{"id": "person_2", "distinct_ids": ["someone_else"]}]}
        ),
    )
    monkeypatch.setattr(
        requests, "delete", lambda *a, **k: delete_calls.append(1) or _FakeResponse()
    )

    result = posthog_admin.purge_person("pseudo_mismatch")

    assert result == {"status": "not_found", "reason": "distinct_id_mismatch"}
    assert delete_calls == []


def test_purge_person_raises_on_http_error(monkeypatch):
    """A genuine HTTP failure is NOT swallowed here -- this call only ever
    runs inside jobcannon.host.tasks.purge_posthog_person, an async worker
    task, so a raised exception cannot block the local deletion cascade
    (which has already committed by then). See module docstring."""
    _configure(monkeypatch)

    def _fake_get(*a, **k):
        raise requests.ConnectionError("simulated network failure")

    monkeypatch.setattr(requests, "get", _fake_get)

    with pytest.raises(requests.ConnectionError):
        posthog_admin.purge_person("pseudo_err")


def test_purge_person_raises_on_non_2xx_delete_response(monkeypatch):
    """raise_for_status() on the DELETE call must also propagate, not just
    the GET -- a 4xx/5xx from PostHog's delete endpoint is a genuine
    failure, not a "not found"."""
    _configure(monkeypatch)
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _FakeResponse(
            payload={"results": [{"id": "person_3", "distinct_ids": ["pseudo_bad_delete"]}]}
        ),
    )
    monkeypatch.setattr(requests, "delete", lambda *a, **k: _FakeResponse(status_code=500))

    with pytest.raises(requests.HTTPError):
        posthog_admin.purge_person("pseudo_bad_delete")


def test_purge_posthog_person_task_delegates_to_purge_person(monkeypatch):
    """jobcannon.host.tasks.purge_posthog_person's own wiring: a thin
    delegate to posthog_admin.purge_person, no extra logic to duplicate-test
    here."""
    from jobcannon.host import tasks

    calls = []

    def _fake_purge(distinct_id):
        calls.append(distinct_id)
        return {"status": "deleted", "person_id": "person_9"}

    monkeypatch.setattr(posthog_admin, "purge_person", _fake_purge)

    result = tasks.purge_posthog_person("pseudo_task_test")

    assert calls == ["pseudo_task_test"]
    assert result == {"status": "deleted", "person_id": "person_9"}
