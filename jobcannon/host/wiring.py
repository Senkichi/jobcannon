"""The three-seam startup (1B spec §1) — the ONE call web and worker make.

1. services.set_services(ScanServices(...))      — persistence/hook seam
2. runtime_config.set_config_provider(provider)  — scan-tuning knob seam
3. extraction_health.set_recorder(...)           — health-log seam
ScanServices.prober_extensions stays None (spec §3.6 ruling: fail-closed;
multi-tenant identity is a Phase 2 design item, consequence C-2).
"""

from __future__ import annotations

from jobcannon.db import _companies, _jd_full, _jobs
from jobcannon.db import pool as pool_mod
from jobcannon.engine import extraction_health, runtime_config, services
from jobcannon.host.config import HostConfig
from jobcannon.host.health_recorder import record_scan_health

_JD_STORAGE_MAX_CHARS = 50_000  # parity with the private repo's JD_STORAGE_MAX_CHARS


def build_scan_services(host_config: HostConfig) -> services.ScanServices:
    return services.ScanServices(
        connection_factory=pool_mod.connection_factory,
        upsert_job=_jobs.upsert_job,
        set_jd_full=_jd_full.set_jd_full,
        upsert_company=_companies.upsert_company,
        # Deliberately the SAME object runtime_config.set_config_provider below
        # hands back (not a copy) — one source of truth, so the services
        # snapshot and the live provider can never drift apart.
        config=host_config.runtime,
        get_secret=lambda name, *, config=None: (
            None
        ),  # operational secrets: Render env vars, resolved per-name in later waves
        jd_storage_max_chars=_JD_STORAGE_MAX_CHARS,
        # prober_extensions deliberately omitted -> None (fail-closed, spec §3.6)
    )


def init_engine_seams(host_config: HostConfig) -> None:
    pool_mod.open_pool(host_config.database_url)
    services.set_services(build_scan_services(host_config))
    runtime_config.set_config_provider(lambda: host_config.runtime)
    # min_meaningful_len left at its default (0): that padding gate exists for
    # email-body semantics (a very short body is a meta/empty email); for an
    # ATS-only host every API response — including [] — is meaningful.
    extraction_health.set_recorder(record_scan_health)


def teardown_engine_seams() -> None:
    extraction_health.set_recorder(None)
    runtime_config.set_config_provider(None)
    services.clear_services()
    pool_mod.close_pool()
