"""render.yaml is inert config until the owner deploys it — these guards make
CI the thing that keeps it honest: every start command references code that
actually exists, the health check path is a real route, and every env var the
host code REQUIRES appears in every service's env list."""

import pathlib
import re

import yaml

from tests.host.conftest import requires_postgres

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _blueprint():
    return yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))


def test_blueprint_parses_and_has_expected_service_inventory():
    bp = _blueprint()
    services = {s["name"]: s for s in bp["services"]}
    assert set(services) == {"jobcannon-web", "jobcannon-worker"}
    assert services["jobcannon-web"]["type"] == "web"
    assert services["jobcannon-worker"]["type"] == "worker"
    assert [d["name"] for d in bp["databases"]] == ["jobcannon-db"]


def test_worker_start_command_module_is_importable():
    bp = _blueprint()
    worker = next(s for s in bp["services"] if s["type"] == "worker")
    m = re.search(r"-m\s+([\w.]+)", worker["startCommand"])
    assert m, "worker startCommand must use python -m <module>"
    import importlib

    assert importlib.import_module(m.group(1) + ".__main__")


def test_web_start_command_app_target_resolves():
    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    m = re.search(r"\"?([\w.]+):([\w()]+)\"?\s*$", web["startCommand"])
    assert m, "web startCommand must end with the gunicorn app target"
    import importlib

    mod = importlib.import_module(m.group(1))
    assert callable(getattr(mod, m.group(2).replace("()", "")))


def test_web_start_command_preloads_app():
    """--preload is load-bearing, not an optimization: the fork-safety hook
    in jobcannon.db.pool is registered when the gunicorn master imports the
    app pre-fork, and each worker rebuilds its own pool in the
    after_in_child callback. Without the flag the topology is whatever the
    platform happens to do (production preloaded even without it — 2026-08-26
    incident); with it, the topology the hook assumes is pinned. Removing
    this flag must be a deliberate act that also revisits the hook."""
    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    assert "--preload" in web["startCommand"].split()


def test_health_check_path_is_a_registered_db_free_route():
    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    from jobcannon.web import create_app

    app = create_app({"TESTING": True, "VERIFY_REQUEST": lambda r: None, "WEBHOOK_SECRET": "x"})
    paths = {r.rule for r in app.url_map.iter_rules()}
    assert web["healthCheckPath"] in paths


def test_health_check_path_responds_200_unauthenticated():
    """url_map membership alone doesn't prove the route is reachable without
    auth — an auth-gated /healthz would still pass that check and then fail
    Render's checker with 401. VERIFY_REQUEST here always returns None (i.e.
    would 401 any non-exempt route), so a 200 here proves /healthz never
    reaches the before_request auth gate at all."""
    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    from jobcannon.web import create_app

    app = create_app({"TESTING": True, "VERIFY_REQUEST": lambda r: None, "WEBHOOK_SECRET": "x"})
    resp = app.test_client().get(web["healthCheckPath"])
    assert resp.status_code == 200


def test_storage_limit_env_matches_disk_size():
    """Drift guard: JC_DB_STORAGE_LIMIT_MB (jobcannon-worker) must stay
    derived from jobcannon-db's diskSizeGB (5GB * 1024 = 5120MB), never a
    stale/hand-edited figure that silently diverges from real disk capacity."""
    bp = _blueprint()
    db = next(d for d in bp["databases"] if d["name"] == "jobcannon-db")
    worker = next(s for s in bp["services"] if s["type"] == "worker")
    limit_mb = next(
        int(e["value"]) for e in worker["envVars"] if e["key"] == "JC_DB_STORAGE_LIMIT_MB"
    )
    assert limit_mb == db["diskSizeGB"] * 1024


def test_every_required_env_var_is_declared_on_both_services():
    """The env-var requirement is DERIVED from HostConfig's field metadata,
    not restated here as a literal — a var added to HostConfig without
    matching render.yaml coverage must fail this test, not go unnoticed
    (see the sabotage check named in the PR that added this derivation)."""
    import dataclasses

    from jobcannon.host.config import HostConfig

    bp = _blueprint()
    derived = {
        f.metadata["env"]: set(f.metadata.get("declare_on", ()))
        for f in dataclasses.fields(HostConfig)
        if f.metadata.get("env")
    }
    # Anti-vacuity control: an empty or mis-keyed `derived` would otherwise
    # make the whole test pass by asserting nothing.
    assert derived, "HostConfig declares no env metadata — the derivation is broken, not the yaml"
    assert "web" in derived["DATABASE_URL"] and "worker" in derived["DATABASE_URL"]

    # The four Clerk vars (issue #47) used to be a hand-maintained literal
    # here because nothing derived them from HostConfig. They're now real
    # HostConfig fields (read at their jobcannon/web/auth.py and
    # jobcannon/web/__init__.py consumption sites instead of os.environ), so
    # this pins them into the same metadata-derived set rather than a second
    # hardcoded list — an omission from HostConfig would silently drop them
    # from `derived` and this assertion would catch it.
    assert {
        "CLERK_SECRET_KEY",
        "CLERK_JWT_KEY",
        "CLERK_AUTHORIZED_PARTIES",
        "CLERK_WEBHOOK_SIGNING_SECRET",
    } <= derived.keys()

    for svc in bp["services"]:
        declared = {e["key"] for e in svc.get("envVars", [])}
        # Worker never verifies a Clerk request or a webhook — that's
        # expressed once, as declare_on=("web",) on the four Clerk fields
        # in HostConfig, not as a second exemption list here.
        required = {env for env, svcs in derived.items() if svc["type"] in svcs}
        missing = required - declared
        assert not missing, f"{svc['name']} missing env declarations: {missing}"


def test_posthog_env_declared_identically_on_both_services():
    """EU ingestion routing: POSTHOG_HOST is a committed literal (not
    sync:false) so it can't drift per-service via the dashboard, and both
    services must declare the same value — jobcannon-web and
    jobcannon-worker each build their own PostHog client through the same
    wiring.py seam, so a mismatch would silently split analytics between
    regions. POSTHOG_API_KEY stays sync:false (a real secret) but must be
    declared on both services too."""
    bp = _blueprint()
    hosts = {}
    for svc in bp["services"]:
        declared = {e["key"] for e in svc["envVars"]}
        assert {"POSTHOG_API_KEY", "POSTHOG_HOST"} <= declared, svc["name"]
        host_entry = next(e for e in svc["envVars"] if e["key"] == "POSTHOG_HOST")
        assert host_entry.get("value") == "https://eu.i.posthog.com", svc["name"]
        hosts[svc["name"]] = host_entry["value"]
    assert hosts["jobcannon-web"] == hosts["jobcannon-worker"]


def test_web_graceful_timeout_covers_posthog_atexit_bound():
    """jobcannon#137: gunicorn's --graceful-timeout must leave real headroom
    over jobcannon.host.wiring's hard backstop on the PostHog client's
    atexit flush (_POSTHOG_ATEXIT_JOIN_TIMEOUT_S) -- otherwise a worker
    exiting during a PostHog outage could still get SIGKILLed mid-flush.
    Derives both sides from their real sources (render.yaml, wiring.py) so
    neither value drifting alone can silently violate the invariant."""
    from jobcannon.host import wiring

    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    m = re.search(r"--graceful-timeout[= ](\d+)", web["startCommand"])
    assert m, "web startCommand must pin --graceful-timeout explicitly"
    graceful_timeout_s = int(m.group(1))

    assert graceful_timeout_s >= wiring._POSTHOG_ATEXIT_JOIN_TIMEOUT_S
    # Real headroom, not just "greater than": leaves margin for everything
    # ELSE gunicorn's graceful shutdown also has to do besides this one
    # atexit handler (finishing an in-flight request, other cleanup).
    assert graceful_timeout_s - wiring._POSTHOG_ATEXIT_JOIN_TIMEOUT_S >= 10


def test_web_predeploy_command_runs_migrations():
    """jobcannon#196: web and worker deploy independently with no ordering
    guarantee (docs/deploy-runbook.md §3), so web's preDeployCommand is THE
    mechanism that makes schema migrations land before the new web code ever
    serves a request against the old schema. Derives the assertion from the
    parsed startCommand-shaped string rather than restating a literal command
    copy, mirroring test_web_start_command_preloads_app's pattern."""
    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    predeploy = web.get("preDeployCommand", "")
    assert predeploy, "jobcannon-web must declare preDeployCommand"
    assert predeploy.split()[:2] == ["uv", "run"], predeploy
    assert "--no-sync" in predeploy.split(), predeploy
    m = re.search(r"-m\s+([\w.]+)", predeploy)
    assert m and m.group(1) == "jobcannon.db.migrate", (
        f"preDeployCommand must invoke `python -m jobcannon.db.migrate`, got: {predeploy!r}"
    )


@requires_postgres
def test_migrate_module_runs_end_to_end_as_a_subprocess():
    """L4-functional proof that `python -m jobcannon.db.migrate` (the command
    render.yaml's preDeployCommand runs) actually works when invoked exactly
    the way Render invokes it: as a subprocess reading DATABASE_URL from the
    environment, not by calling run_migrations() in-process. Always
    sys.executable (Windows App Execution Alias hazard — see
    test_scan_block_report_help above)."""
    import os
    import subprocess
    import sys

    from tests.host.conftest import create_throwaway_db, drop_throwaway_db

    dsn, db_name = create_throwaway_db("jobcannon_predeploy_e2e")
    try:
        env = dict(os.environ)
        env["DATABASE_URL"] = dsn

        first = subprocess.run(
            [sys.executable, "-m", "jobcannon.db.migrate"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert first.returncode == 0, (
            f"first run must apply everything and exit 0; stderr:\n{first.stderr}"
        )

        second = subprocess.run(
            [sys.executable, "-m", "jobcannon.db.migrate"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert second.returncode == 0, (
            f"second run must be a no-op and still exit 0; stderr:\n{second.stderr}"
        )
    finally:
        drop_throwaway_db(db_name)


def test_migrate_module_exits_nonzero_on_bogus_dsn():
    """Negative counterpart: a broken DSN must abort (non-zero exit), never
    silently succeed — this is what makes a Render pre-deploy failure actually
    block promotion of the new web code (render.com/docs/deploys: "If any
    command fails or times out, the entire deploy fails").

    connect_timeout=3 is load-bearing, not decoration: an unreachable
    127.0.0.1 port is silently dropped rather than RST on this Windows CI
    box, so a bare psycopg connect() hangs on the OS-level TCP timeout
    (tens of seconds) instead of failing fast — verified directly against
    this port before adding the bound.

    Asserting returncode != 0 alone would pass "vacuously" for the wrong
    reason too (e.g. an ImportError or a typo'd module path also exits
    non-zero) — this negative test is not @requires_postgres-gated, so on a
    box without POSTGRES_ADMIN_DSN it is the ONLY one of the two subprocess
    tests that runs, with no positive-control sibling to catch that. Also
    assert main()'s own logged failure line is present, so this can only
    pass on a genuine connect failure."""
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["DATABASE_URL"] = "postgresql://bogus:bogus@127.0.0.1:1/does_not_exist?connect_timeout=3"

    result = subprocess.run(
        [sys.executable, "-m", "jobcannon.db.migrate"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "pre-deploy migration run failed" in result.stderr, result.stderr


def test_scan_block_report_help():
    """Windows self-hosted CI hazard: a bare 'python' subprocess can be
    hijacked by Windows App Execution Alias stubs — always sys.executable.
    --help must exit 0 without DATABASE_URL set (argparse exits before main()
    ever touches the environment)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "scan_block_report.py"), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
