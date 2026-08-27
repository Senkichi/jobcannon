"""Tests for scripts/check_migration_collisions.py (issue #211, CI guard).

Loaded by path (matches tests/test_ported_paths_manifest.py's pattern).
Every test that exercises run()/main() injects a fake `transport` callable
or monkeypatches `_default_transport` -- no real network call anywhere in
this file, per the brief's explicit "no network in tests" requirement.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_migration_collisions.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_migration_collisions", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cmc = _load_module()


# ---------------------------------------------------------------------------
# Pure decision logic
# ---------------------------------------------------------------------------


def test_parse_versions_maps_number_to_filename():
    names = ["m0001_initial_schema.py", "m0013_add_thing.py", "types.py", "__init__.py"]
    assert cmc.parse_versions(names) == {1: "m0001_initial_schema.py", 13: "m0013_add_thing.py"}


def test_parse_versions_ignores_non_matching_names():
    assert cmc.parse_versions(["README.md"]) == {}


def test_added_versions_is_head_minus_merge_base():
    head = {1: "m0001_x.py", 12: "m0012_y.py", 13: "m0013_new.py"}
    merge_base = {1: "m0001_x.py", 12: "m0012_y.py"}
    assert cmc.added_versions(head, merge_base) == {13: "m0013_new.py"}


def test_added_versions_empty_when_pr_adds_nothing():
    head = {1: "m0001_x.py"}
    merge_base = {1: "m0001_x.py"}
    assert cmc.added_versions(head, merge_base) == {}


def test_added_versions_uses_merge_base_not_current_main():
    """The exact reason merge-base (not current origin/main) is the
    reference: if main has since gained versions the PR never saw, those
    must NOT be misread as "added by this PR" just because they're absent
    from a naive head-vs-main diff in the other direction."""
    head = {1: "m0001_x.py", 13: "m0013_pr_own.py"}
    merge_base = {1: "m0001_x.py"}
    # main raced ahead independently and now also has 1..1 plus something
    # this PR never touched -- shouldn't change what "added" means.
    assert cmc.added_versions(head, merge_base) == {13: "m0013_pr_own.py"}


def test_find_collisions_flags_origin_main_hit():
    added = {13: "m0013_pr_own.py"}
    others = {"origin/main": {13: "m0013_someone_else.py"}, "PR #7": {}}
    assert cmc.find_collisions(added, others) == {13: ["origin/main"]}


def test_find_collisions_flags_sibling_pr_hit():
    added = {13: "m0013_pr_own.py"}
    others = {"origin/main": {12: "m0012_y.py"}, "PR #7": {13: "m0013_other.py"}}
    assert cmc.find_collisions(added, others) == {13: ["PR #7"]}


def test_find_collisions_flags_multiple_sources_sorted():
    added = {13: "m0013_pr_own.py"}
    others = {"origin/main": {13: "x"}, "PR #9": {13: "y"}, "PR #2": {13: "z"}}
    assert cmc.find_collisions(added, others) == {13: ["PR #2", "PR #9", "origin/main"]}


def test_find_collisions_empty_when_no_overlap():
    added = {13: "m0013_pr_own.py"}
    others = {"origin/main": {12: "m0012_y.py"}, "PR #7": {14: "m0014_z.py"}}
    assert cmc.find_collisions(added, others) == {}


def test_format_collision_message_names_pr_and_sources():
    msg = cmc.format_collision_message({13: ["origin/main", "PR #7"]}, pr_number=42)
    assert "PR #42" in msg
    assert "version 13" in msg
    assert "origin/main, PR #7" in msg
    assert "scripts/new_migration.py" in msg


# ---------------------------------------------------------------------------
# run() -- wiring, with an injected fake transport (no network)
# ---------------------------------------------------------------------------


def _fake_transport(responses: dict[str, object]):
    """responses maps a URL substring -> the JSON body to return for the
    first URL that contains it. Raises AssertionError on an unmapped URL so
    a test can't silently pass on an unexpected call."""

    def transport(method: str, url: str, token: str):
        assert token, "token must be non-empty"
        for key, body in responses.items():
            if key in url:
                return body
        raise AssertionError(f"unexpected URL: {url}")

    return transport


def _base_responses(head_files, base_files, main_files, merge_base_sha="deadbeef"):
    return {
        "compare/main...abc123": {"merge_base_commit": {"sha": merge_base_sha}},
        "contents/jobcannon/db/migrations?ref=abc123": [
            {"name": n, "type": "file"} for n in head_files
        ],
        f"contents/jobcannon/db/migrations?ref={merge_base_sha}": [
            {"name": n, "type": "file"} for n in base_files
        ],
        "contents/jobcannon/db/migrations?ref=main": [{"name": n, "type": "file"} for n in main_files],
    }


def test_run_skips_cleanly_on_non_pr_event():
    rc = cmc.run(
        event_name="push",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=1,
        pr_head_sha="abc123",
        base_ref="main",
        transport=_fake_transport({}),
    )
    assert rc == 0


def test_run_skips_when_pr_adds_no_migrations():
    responses = _base_responses(
        head_files=["m0001_x.py"], base_files=["m0001_x.py"], main_files=["m0001_x.py"]
    )
    rc = cmc.run(
        event_name="pull_request",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=1,
        pr_head_sha="abc123",
        base_ref="main",
        transport=_fake_transport(responses),
    )
    assert rc == 0


def test_run_passes_when_added_version_is_free(monkeypatch):
    responses = _base_responses(
        head_files=["m0001_x.py", "m0013_new.py"],
        base_files=["m0001_x.py"],
        main_files=["m0001_x.py"],
    )
    responses["pulls?state=open"] = []
    rc = cmc.run(
        event_name="pull_request",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=1,
        pr_head_sha="abc123",
        base_ref="main",
        transport=_fake_transport(responses),
    )
    assert rc == 0


def test_run_fails_on_origin_main_collision():
    """origin/main already merged the same version number under a
    different slug -- the #211 race after one sibling PR has landed."""
    responses = _base_responses(
        head_files=["m0001_x.py", "m0013_new.py"],
        base_files=["m0001_x.py"],
        main_files=["m0001_x.py", "m0013_landed_already.py"],
    )
    responses["pulls?state=open"] = []
    messages = []
    rc = cmc.run(
        event_name="pull_request",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=5,
        pr_head_sha="abc123",
        base_ref="main",
        transport=_fake_transport(responses),
        out=messages.append,
    )
    assert rc == 1
    assert "PR #5" in messages[0]
    assert "version 13" in messages[0]
    assert "origin/main" in messages[0]


def test_run_fails_on_sibling_open_pr_collision():
    """The exact #211 scenario before either PR has merged: two open PRs
    both minted the same version independently."""
    responses = _base_responses(
        head_files=["m0001_x.py", "m0013_new.py"],
        base_files=["m0001_x.py"],
        main_files=["m0001_x.py"],
    )
    responses["pulls?state=open"] = [{"number": 8, "head": {"sha": "sibling-sha"}}]
    responses["contents/jobcannon/db/migrations?ref=sibling-sha"] = [
        {"name": "m0001_x.py", "type": "file"},
        {"name": "m0013_sibling.py", "type": "file"},
    ]
    messages = []
    rc = cmc.run(
        event_name="pull_request",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=5,
        pr_head_sha="abc123",
        base_ref="main",
        transport=_fake_transport(responses),
        out=messages.append,
    )
    assert rc == 1
    assert "PR #8" in messages[0]
    assert "version 13" in messages[0]


def test_run_excludes_self_from_open_pr_list():
    """fetch_open_prs must drop the PR under test itself -- otherwise every
    PR would "collide" with its own head forever."""
    responses = _base_responses(
        head_files=["m0001_x.py", "m0013_new.py"],
        base_files=["m0001_x.py"],
        main_files=["m0001_x.py"],
    )
    responses["pulls?state=open"] = [{"number": 5, "head": {"sha": "abc123"}}]
    rc = cmc.run(
        event_name="pull_request",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=5,
        pr_head_sha="abc123",
        base_ref="main",
        transport=_fake_transport(responses),
    )
    assert rc == 0


def test_run_fails_loudly_on_api_error():
    def broken_transport(method, url, token):
        raise cmc.urllib.error.URLError("boom")

    messages = []
    rc = cmc.run(
        event_name="pull_request",
        repo="Senkichi/jobcannon",
        token="tok",
        pr_number=1,
        pr_head_sha="abc123",
        base_ref="main",
        transport=broken_transport,
        out=messages.append,
    )
    assert rc == 1
    assert "GitHub API error" in messages[0]


def test_fetch_open_prs_paginates():
    """`page=` is always the URL's last query param, so anchor the match on
    the exact tail -- a bare substring check ("page=1" in url) would also
    match "per_page=100" and "page=10", turning a wrong-but-plausible test
    double into an infinite loop (caught while writing this test: the fake
    transport below matched every page number and fetch_open_prs spun past
    page 20000+ making real network calls before it was killed -- the real
    function's own loop logic was never at fault)."""
    calls = []

    def transport(method, url, token):
        calls.append(url)
        if url.endswith("&page=1"):
            return [{"number": n, "head": {"sha": "x"}} for n in range(1, 101)]
        if url.endswith("&page=2"):
            return [{"number": 101, "head": {"sha": "x"}}]
        raise AssertionError(url)

    prs = cmc.fetch_open_prs("Senkichi/jobcannon", "tok", transport, exclude_number=999)
    assert len(prs) == 101
    assert any(c.endswith("&page=2") for c in calls)


# ---------------------------------------------------------------------------
# main() -- env var wiring
# ---------------------------------------------------------------------------


def test_main_skips_on_non_pr_event(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    rc = cmc.main()
    assert rc == 0
    assert "skipping" in capsys.readouterr().out


def test_main_fails_on_missing_required_env_var(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("PR_NUMBER", raising=False)
    monkeypatch.delenv("PR_HEAD_SHA", raising=False)
    rc = cmc.main()
    assert rc == 1
    assert "missing/invalid required env var" in capsys.readouterr().err


def test_main_wires_env_vars_into_run(monkeypatch):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REPOSITORY", "Senkichi/jobcannon")
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("PR_NUMBER", "5")
    monkeypatch.setenv("PR_HEAD_SHA", "abc123")
    monkeypatch.setenv("GITHUB_BASE_REF", "main")

    responses = _base_responses(
        head_files=["m0001_x.py"], base_files=["m0001_x.py"], main_files=["m0001_x.py"]
    )
    monkeypatch.setattr(cmc, "_default_transport", lambda method, url, token: _fake_transport(responses)(method, url, token))

    rc = cmc.main()
    assert rc == 0
