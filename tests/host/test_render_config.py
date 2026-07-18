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


def test_every_required_env_var_is_declared_on_both_services():
    bp = _blueprint()
    required = {
        "DATABASE_URL",
        "CLERK_SECRET_KEY",
        "CLERK_JWT_KEY",
        "CLERK_AUTHORIZED_PARTIES",
        "CLERK_WEBHOOK_SIGNING_SECRET",
    }
    for svc in bp["services"]:
        declared = {e["key"] for e in svc.get("envVars", [])}
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
