"""render.yaml is inert config until the owner deploys it — these guards make
CI the thing that keeps it honest: every start command references code that
actually exists, the health check path is a real route, and every env var the
host code REQUIRES appears in every service's env list."""

import pathlib
import re

import yaml

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
        # expressed once, as declare_on=("web",) on three of the four Clerk
        # fields in HostConfig (clerk_secret_key is the exception — issue
        # #136's reconciliation sweep runs on the worker and needs its own
        # Clerk Backend API client), not as a second exemption list here.
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


def test_posthog_admin_env_declared_on_worker_only():
    """Issue #135's PostHog admin/private-API credentials
    (POSTHOG_PERSONAL_API_KEY, POSTHOG_PROJECT_ID, POSTHOG_ADMIN_API_HOST)
    are declare_on=() in HostConfig (optional/fail-soft, like
    POSTHOG_API_KEY/POSTHOG_HOST) so the generic derivation test above
    doesn't cover them — this is their dedicated coverage. Unlike
    POSTHOG_API_KEY/POSTHOG_HOST (declared identically on both services),
    these are WORKER-ONLY: the purge only ever runs inside
    jobcannon.host.tasks.purge_posthog_person, a worker-side procrastinate
    task; jobcannon-web never calls posthog_admin.purge_person, so
    declaring these on web would be dead config with no consumer."""
    bp = _blueprint()
    web = next(s for s in bp["services"] if s["type"] == "web")
    worker = next(s for s in bp["services"] if s["type"] == "worker")
    admin_keys = {"POSTHOG_PERSONAL_API_KEY", "POSTHOG_PROJECT_ID", "POSTHOG_ADMIN_API_HOST"}

    worker_declared = {e["key"] for e in worker["envVars"]}
    web_declared = {e["key"] for e in web["envVars"]}
    assert admin_keys <= worker_declared
    assert not (admin_keys & web_declared)

    host_entry = next(e for e in worker["envVars"] if e["key"] == "POSTHOG_ADMIN_API_HOST")
    # Distinct from POSTHOG_HOST's ingestion value (eu.i.posthog.com) —
    # PostHog's private/admin REST API lives on a different host.
    assert host_entry.get("value") == "https://eu.posthog.com"


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
