"""Env-var-backed host config (spec §3.4: hosted has no config.yaml).

Fail-fast on required values (same philosophy as the private repo's config
loader); scan-tuning knobs are OPTIONAL — an unset knob is ABSENT from the
mapping so the engine reader's hardcoded default applies, exactly the
fail-closed semantics runtime_config.get_runtime_config() documents.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfig:
    database_url: str
    runtime: dict = field(default_factory=dict)


def _put_int(mapping: dict, section: str, key: str, env_var: str) -> None:
    val = os.environ.get(env_var)
    if val is not None and val.strip():
        mapping.setdefault(section, {})[key] = int(val)


def load_host_config() -> HostConfig:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required (Postgres DSN)")
    runtime: dict = {}
    _put_int(runtime, "ats", "scan_memo_ttl_s", "JC_SCAN_MEMO_TTL_S")
    _put_int(runtime, "ats", "detail_fetch_concurrency", "JC_DETAIL_FETCH_CONCURRENCY")
    _put_int(runtime, "ats", "page_fetch_concurrency", "JC_PAGE_FETCH_CONCURRENCY")
    statuses = os.environ.get("JC_AUTH_BLOCK_STATUSES")
    if statuses and statuses.strip():
        runtime.setdefault("health", {})["auth_block_statuses"] = [
            int(s) for s in statuses.split(",") if s.strip()
        ]
    return HostConfig(database_url=database_url, runtime=runtime)
