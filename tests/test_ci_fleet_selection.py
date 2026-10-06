"""Test impact selection wiring (ci-fleet 0.7): ci.yml's nightly and test job,
ci-fleet-bisect.yml, ci-fleet.toml and the Aviator skip-line label.

YAML 1.1 reads the bare key ``on`` as ``True``, so ``_triggers`` accepts
either. The literals mirror ci-fleet's own names (the map artifact, the bisect
run name and inputs, the ledger kill switch); ci-fleet's workflow contract
tests pin the producer side, so a rename there fails there first.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SELECTOR = 'uvx --from "ci-fleet~=0.7.0" ci-fleet'
BISECT_INPUTS = {"good", "bad", "nodeids", "seed", "break_id"}
TOML_KEYS = {
    "enabled",
    "full_threshold",
    "max_map_age_h",
    "min_level",
    "full_triggers",
    "inert",
    "path_rules",
    "ready_label",
}
PYTEST = "uv run pytest -q --tb=short"
NIGHTLY = "(github.event_name == 'schedule' || github.event_name == 'workflow_dispatch')"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    return doc.get("on", doc.get(True)) or {}


def _step(job: dict, name: str) -> dict:
    [step] = [s for s in job["steps"] if s.get("name") == name]
    return step


def _uses(job: dict, action: str) -> list[dict]:
    return [s for s in job["steps"] if str(s.get("uses", "")).startswith(action)]


@pytest.fixture(scope="module")
def ci() -> dict:
    return _load(WORKFLOWS / "ci.yml")


@pytest.fixture(scope="module")
def bis() -> dict:
    return _load(WORKFLOWS / "ci-fleet-bisect.yml")


@pytest.fixture(scope="module")
def selection() -> dict:
    with open(ROOT / "ci-fleet.toml", "rb") as handle:
        return tomllib.load(handle)["test_selection"]


def test_the_nightly_maps_the_suite(ci) -> None:
    assert "workflow_dispatch" in _triggers(ci)
    assert _triggers(ci)["schedule"] == [{"cron": "0 9 * * *"}]  # the repo's one nightly
    job = ci["jobs"]["test"]
    assert job["timeout-minutes"] == f"${{{{ {NIGHTLY} && 60 || 20 }}}}"
    install = _step(job, "Install ci-fleet[map]")
    assert install["if"] == NIGHTLY
    assert install["run"] == 'uv pip install "ci-fleet[map]~=0.7.0"'
    step = _step(job, "Run tests (map mode)")
    assert step["if"] == NIGHTLY
    assert step["env"] == {
        "CI_FLEET_MAP": "1",
        "COVERAGE_CORE": "ctrace",
        "CI_FLEET_LEDGER_CONTEXT": "nightly",
    }
    assert "map_args=$(uv run --no-sync ci-fleet map prepare)" in step["run"]
    assert 'uv run --no-sync pytest -q --tb=short "${map_args[@]}"' in step["run"]
    build = _step(job, "Build the map")
    assert build["if"] == f"{NIGHTLY} && !cancelled()"
    assert "--map-sha ${{ github.sha }}" in build["run"]
    uploads = {s["with"]["name"]: s["with"] for s in _uses(job, "actions/upload-artifact@")}
    assert uploads["ci-fleet-map"]["path"] == ".ci-fleet-map/ci-fleet-map.json.gz"
    assert uploads["ci-fleet-map"]["retention-days"] == 30
    assert uploads["map-coverage-data"]["retention-days"] == 7
    # Both live under `.ci-fleet-map/`, which upload-artifact@v4 skips by default.
    assert uploads["ci-fleet-map"]["include-hidden-files"] is True
    assert uploads["map-coverage-data"]["include-hidden-files"] is True
    assert [n for n in uploads if n.startswith("ci-fleet-map")] == ["ci-fleet-map"]


def test_map_mode_never_leaves_its_step(ci) -> None:
    holders = [
        (key, s.get("name"))
        for key, job in ci["jobs"].items()
        for s in job.get("steps", [])
        if "CI_FLEET_MAP" in (s.get("env") or {}) or "--github-env" in str(s.get("run", ""))
    ]
    assert holders == [("test", "Run tests (map mode)")]


def test_pull_requests_run_the_selection(ci) -> None:
    assert ci["permissions"]["actions"] == "read"  # the selector lists map artifacts
    job = ci["jobs"]["test"]
    [checkout] = _uses(job, "actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0  # the selector diffs head against base
    step = _step(job, "Run tests (selection)")
    assert step["if"] == "github.event_name == 'pull_request'"
    assert step["env"] == {
        "CI_FLEET_SELECT_LEVEL": "${{ vars.CI_FLEET_SELECT_LEVEL }}",
        "CI_FLEET_SELECT": "${{ vars.CI_FLEET_SELECT }}",
        "GH_TOKEN": "${{ github.token }}",
    }
    # Enforced since the shadow window was met (TIS-JCP-2): no `--shadow`.
    assert step["run"] == (
        f"{SELECTOR} test --repo ${{{{ github.repository }}}} "
        "--base ${{ github.event.pull_request.base.sha }} "
        f"--context ci -- {PYTEST}"
    )
    plain = _step(job, "Run tests")
    assert plain["if"] == "github.event_name == 'push'"
    assert plain["run"] == PYTEST


def test_bisect_matches_what_triage_dispatches_and_polls(bis) -> None:
    inputs = _triggers(bis)["workflow_dispatch"]["inputs"]
    assert set(inputs) == BISECT_INPUTS
    assert {k for k, v in inputs.items() if v.get("required")} == BISECT_INPUTS - {"seed"}
    assert set(_triggers(bis)) == {"workflow_dispatch"}  # never a second nightly
    assert bis["run-name"] == "ci-fleet-bisect break-${{ inputs.break_id }}"
    [job] = bis["jobs"].values()
    assert job["env"]["CI_FLEET_LEDGER"] == "off"
    assert job["env"]["UV_PROJECT_ENVIRONMENT"] == ".venv"
    step = _step(job, "Bisect")
    assert set(step["env"]) == {"GOOD", "BAD", "NODEIDS", "SEED"}
    assert "${{" not in step["run"]  # inputs reach the shell only through env
    assert f"{SELECTOR} nightly bisect" in step["run"]
    assert "--out ci-fleet-bisect" in step["run"]
    [upload] = _uses(job, "actions/upload-artifact@")
    assert upload["with"]["name"] == "ci-fleet-bisect-${{ inputs.break_id }}"
    assert upload["with"]["path"] == "ci-fleet-bisect/"
    assert upload["if"] == "always()"


def test_bisect_runs_where_the_suite_runs(ci, bis) -> None:
    [job] = bis["jobs"].values()
    test = ci["jobs"]["test"]
    assert job["runs-on"] == test["runs-on"]
    assert job["timeout-minutes"] == 270  # ci-fleet's own bound is 4 h
    assert job["services"] == test["services"]  # the tests need the same Postgres
    assert job["env"]["POSTGRES_ADMIN_DSN"] == test["env"]["POSTGRES_ADMIN_DSN"]
    [setup] = _uses(job, "astral-sh/setup-uv@")
    [suite_setup] = _uses(test, "astral-sh/setup-uv@")
    assert setup["with"]["python-version"] == suite_setup["with"]["python-version"]
    [checkout] = _uses(job, "actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0


def test_ci_fleet_toml_is_on_and_known(selection) -> None:
    assert selection["enabled"] is True
    assert set(selection) <= TOML_KEYS


def test_aviator_moves_skip_line_prs_to_the_front() -> None:
    labels = _load(ROOT / ".aviator" / "config.yml")["merge_rules"]["labels"]
    assert labels == {"trigger": "mergequeue", "skip_line": "mergequeue-skip-line"}
