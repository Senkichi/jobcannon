"""Env-var-backed host config (spec §3.4: hosted has no config.yaml).

Fail-fast on required values (same philosophy as the private repo's config
loader); scan-tuning knobs are OPTIONAL — an unset knob is ABSENT from the
mapping so the engine reader's hardcoded default applies, exactly the
fail-closed semantics runtime_config.get_runtime_config() documents.

"Optional" means UNSET -> absent from the mapping (and unset/whitespace-only
are treated the same way). It does NOT mean best-effort parsing: if the var
IS present with a non-blank value, it must parse as an integer or startup
fails fast with a named RuntimeError identifying the offending env var — a
typo silently changing runtime concurrency is worse than a clear crash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfig:
    # Every env-backed field below carries `metadata={"env": ..., "declare_on":
    # ...}`. `declare_on` names the render.yaml service TYPES ("web" /
    # "worker") that must declare the var — tests/host/test_render_config.py
    # derives its required-env-var set from this metadata rather than
    # restating it, so a field added here without render.yaml coverage fails
    # CI instead of going unnoticed. `runtime` carries no `env` key and is
    # skipped by that derivation; it is a derived mapping, not a single var.
    database_url: str = field(metadata={"env": "DATABASE_URL", "declare_on": ("web", "worker")})
    runtime: dict = field(default_factory=dict)
    posthog_api_key: str | None = field(
        default=None, metadata={"env": "POSTHOG_API_KEY", "declare_on": ()}
    )
    posthog_host: str | None = field(
        default=None, metadata={"env": "POSTHOG_HOST", "declare_on": ()}
    )
    # Flask session signing key. Requiredness is enforced where it's
    # consumed (jobcannon.web.create_app's fail-fast), not here — an unset
    # var must still produce a valid HostConfig so load_host_config's only
    # fail-fast stays DATABASE_URL.
    secret_key: str = field(default="", metadata={"env": "JC_SECRET_KEY", "declare_on": ("web",)})
    clerk_sign_up_url: str = field(
        default="", metadata={"env": "CLERK_SIGN_UP_URL", "declare_on": ("web",)}
    )
    # The worker serves no requests and mints no sessions, so it never reads
    # signup_wave.
    signup_wave: str = field(
        default="0", metadata={"env": "JC_SIGNUP_WAVE", "declare_on": ("web",)}
    )


def _put_int(mapping: dict, section: str, key: str, env_var: str) -> None:
    val = os.environ.get(env_var)
    if val is not None and val.strip():
        try:
            mapping.setdefault(section, {})[key] = int(val)
        except ValueError as exc:
            raise RuntimeError(f"Invalid value for {env_var}: {val!r} (expected integer)") from exc


def load_host_config() -> HostConfig:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required (Postgres DSN)")
    runtime: dict = {}
    # Do NOT add an ats.scan_concurrency pass-through here. The engine's
    # concurrent scan branch (scan_concurrency > 1) deadlocks when the scan
    # deadline trips with submitted work still queued — jobcannon issue #39. Hosted is
    # safe today ONLY because this mapping omits that knob, so the engine
    # resolves its default of 1 and stays on the serial branch
    # (tripwire test: tests/host/test_scan_services_deadline.py). Fix #39
    # before adding the knob.
    _put_int(runtime, "ats", "scan_memo_ttl_s", "JC_SCAN_MEMO_TTL_S")
    _put_int(runtime, "ats", "detail_fetch_concurrency", "JC_DETAIL_FETCH_CONCURRENCY")
    _put_int(runtime, "ats", "page_fetch_concurrency", "JC_PAGE_FETCH_CONCURRENCY")
    statuses = os.environ.get("JC_AUTH_BLOCK_STATUSES")
    if statuses and statuses.strip():
        try:
            runtime.setdefault("health", {})["auth_block_statuses"] = [
                int(s) for s in statuses.split(",") if s.strip()
            ]
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid value for JC_AUTH_BLOCK_STATUSES: {statuses!r} "
                "(expected comma-separated integers)"
            ) from exc
    posthog_api_key = (os.environ.get("POSTHOG_API_KEY") or "").strip() or None
    posthog_host = (os.environ.get("POSTHOG_HOST") or "").strip() or None
    return HostConfig(
        database_url=database_url,
        runtime=runtime,
        posthog_api_key=posthog_api_key,
        posthog_host=posthog_host,
        secret_key=os.environ.get("JC_SECRET_KEY", ""),
        clerk_sign_up_url=os.environ.get("CLERK_SIGN_UP_URL", ""),
        signup_wave=os.environ.get("JC_SIGNUP_WAVE", "0"),
    )
