"""Tests for the board-identity probe capability (ats_registry + ats_prober).

Fetches the board's own display name from the ATS API so
``ats_slug_challenge`` can break identity ties where BOTH the incumbent owner
and the challenger are name-affine to the slug (name-vs-slug affinity alone
can't separate "Mercury Insurance Company" from "Mercury" on
``greenhouse/mercury`` — see that module's docstring). Endpoint shapes verified
live against the real APIs before writing these mocked unit tests:
``boards-api.greenhouse.io/v1/boards/mercury`` -> ``{"name": "Mercury", ...}``;
``api.smartrecruiters.com/v1/companies/AbbVie/postings?limit=1`` -> each
posting embeds ``company: {"name": "AbbVie", ...}``.
"""

from unittest.mock import patch

import jobcannon.engine.ats_prober as ats_prober
from jobcannon.engine import ats_registry


class _FakeResp:
    def __init__(self, status_code: int, payload=None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class TestProbeIdentityGreenhouse:
    def test_returns_board_name_on_200(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"name": "Mercury", "content": ""}),
        ):
            assert ats_prober._probe_identity_greenhouse("mercury") == "Mercury"

    def test_returns_none_on_404(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(404, {"status": 404, "error": "Job board not found"}),
        ):
            assert ats_prober._probe_identity_greenhouse("doesnotexist") is None

    def test_returns_none_on_missing_name_field(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"content": ""}),
        ):
            assert ats_prober._probe_identity_greenhouse("acme") is None

    def test_returns_none_on_blank_name(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"name": "   "}),
        ):
            assert ats_prober._probe_identity_greenhouse("acme") is None

    def test_returns_none_on_non_dict_body(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, ["unexpected", "list"]),
        ):
            assert ats_prober._probe_identity_greenhouse("acme") is None

    def test_returns_none_on_network_exception(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            side_effect=ConnectionError("boom"),
        ):
            assert ats_prober._probe_identity_greenhouse("acme") is None


class TestProbeIdentitySmartrecruiters:
    def test_returns_company_name_from_first_posting(self):
        payload = {
            "content": [{"company": {"identifier": "AbbVie", "name": "AbbVie"}}],
            "totalFound": 1623,
        }
        with patch(
            "jobcannon.engine.ats_prober.requests.get", return_value=_FakeResp(200, payload)
        ):
            assert ats_prober._probe_identity_smartrecruiters("AbbVie") == "AbbVie"

    def test_returns_none_on_404(self):
        with patch("jobcannon.engine.ats_prober.requests.get", return_value=_FakeResp(404, {})):
            assert ats_prober._probe_identity_smartrecruiters("doesnotexist") is None

    def test_returns_none_on_empty_content(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"content": [], "totalFound": 0}),
        ):
            assert ats_prober._probe_identity_smartrecruiters("acme") is None

    def test_returns_none_on_missing_company_key(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            return_value=_FakeResp(200, {"content": [{"id": "1"}]}),
        ):
            assert ats_prober._probe_identity_smartrecruiters("acme") is None

    def test_returns_none_on_network_exception(self):
        with patch(
            "jobcannon.engine.ats_prober.requests.get",
            side_effect=TimeoutError("boom"),
        ):
            assert ats_prober._probe_identity_smartrecruiters("acme") is None


class TestRegistryDispatch:
    def test_dispatches_to_greenhouse_probe(self, monkeypatch):
        monkeypatch.setattr(ats_prober, "_probe_identity_greenhouse", lambda slug: "Widgets Inc")
        assert ats_registry.probe_board_identity("greenhouse", "widgets") == "Widgets Inc"

    def test_dispatches_to_smartrecruiters_probe(self, monkeypatch):
        monkeypatch.setattr(
            ats_prober, "_probe_identity_smartrecruiters", lambda slug: "Widgets Inc"
        )
        assert ats_registry.probe_board_identity("smartrecruiters", "widgets") == "Widgets Inc"

    def test_none_for_platform_without_identity_probe(self):
        # lever has a liveness probe_attr but no identity_probe_attr registered.
        assert ats_registry.PLATFORMS["lever"].identity_probe_attr is None
        assert ats_registry.probe_board_identity("lever", "widgets") is None

    def test_none_for_unknown_platform(self):
        assert ats_registry.probe_board_identity("not_a_platform", "widgets") is None
