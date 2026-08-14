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
    """OD-18 drift guard: JC_DB_STORAGE_LIMIT_MB (jobcannon-worker) must stay
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

    # The four Clerk names stay a literal, deliberately: they are read
    # directly from os.environ in jobcannon/web/auth.py (CLERK_SECRET_KEY,
    # CLERK_JWT_KEY, CLERK_AUTHORIZED_PARTIES) and jobcannon/web/__init__.py
    # (CLERK_WEBHOOK_SIGNING_SECRET) rather than through HostConfig, so
    # nothing exists yet to derive them from.
    clerk_required = {
        "CLERK_SECRET_KEY",
        "CLERK_JWT_KEY",
        "CLERK_AUTHORIZED_PARTIES",
        "CLERK_WEBHOOK_SIGNING_SECRET",
    }
    for svc in bp["services"]:
        declared = {e["key"] for e in svc.get("envVars", [])}
        required = {env for env, svcs in derived.items() if svc["type"] in svcs} | clerk_required
        missing = required - declared
        # Worker never verifies Clerk requests or webhooks:
        if svc["type"] == "worker":
            missing -= {
                "CLERK_JWT_KEY",
                "CLERK_AUTHORIZED_PARTIES",
                "CLERK_WEBHOOK_SIGNING_SECRET",
                "CLERK_SECRET_KEY",
            }
        assert not missing, f"{svc['name']} missing env declarations: {missing}"


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
