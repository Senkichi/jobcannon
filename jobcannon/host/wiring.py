"""The four-seam startup (1B spec §1) — the ONE call web and worker make.

1. services.set_services(ScanServices(...))      — persistence/hook seam
2. runtime_config.set_config_provider(provider)  — scan-tuning knob seam
3. extraction_health.set_recorder(...)           — health-log seam
4. posthog_client.set_posthog_client(...)        — analytics fan-out seam,
   plus posthog_client.set_analytics_salt(...) riding along in the same
   step: the pseudonymization salt gates whether that fan-out ever reaches
   PostHog with an identifier at all (posthog_client.pseudonymize's
   fail-closed contract) — it is not a fifth seam, just the other half of
   seam 4's configuration.
ScanServices.prober_extensions stays None (spec §3.6 ruling: fail-closed;
multi-tenant identity is a Phase 2 design item, consequence C-2).

The PostHog seam is inert (None) unless POSTHOG_API_KEY is set — dev/CI runs
with no PostHog account keep log_event's fan-out a documented no-op; setting
the key on Render turns it on without any code change. Setting
POSTHOG_API_KEY without also setting JC_ANALYTICS_PSEUDONYM_SALT does NOT
turn fan-out on: log_event fails closed (no salt -> no distinct_id -> no
capture call) rather than ever sending the raw Clerk user id.

Seam 4 also registers a post-fork rebuild hook (#129), mirroring
jobcannon.db.pool's after_in_child hook: gunicorn --preload (#128) builds
this module's PostHog client once in the master, and posthog.Posthog spawns
a background Consumer daemon thread at construction time. A fork does not
carry threads into the child, so an inherited client's consumer is dead on
arrival in every worker — capture() calls enqueue into a queue nothing
drains, silently. See _install_posthog_fork_hook / _reinit_posthog_after_fork
below.
"""

from __future__ import annotations

import logging
import os

from jobcannon.db import _companies, _jd_full, _jobs
from jobcannon.db import pool as pool_mod
from jobcannon.engine import extraction_health, runtime_config, services
from jobcannon.host import posthog_client
from jobcannon.host.config import HostConfig
from jobcannon.host.health_recorder import record_scan_health

logger = logging.getLogger(__name__)

_JD_STORAGE_MAX_CHARS = 50_000  # parity with the private repo's JD_STORAGE_MAX_CHARS
_SCAN_DEADLINE_S = (
    # Parity with the private deployment's job-level runtime cap for the ATS
    # scan. Calibration caveat: that cap bounds one whole-fleet scan, while a
    # hosted scan task covers a single company — so the same number is a much
    # looser per-unit bound here, a backstop against a pathologically hung
    # single-company scan, not a fleet budget. Retuning it is a deliberate
    # operator decision; numeric parity is what the port preserves.
    12_600.0
)


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
        scan_deadline_s=_SCAN_DEADLINE_S,
    )


def _build_posthog_client(host_config: HostConfig):
    """Construct a PostHog client when an API key is configured, else None
    (inert no-op fan-out in dev/CI). Constructing the client does not open a
    network connection — events batch on a background consumer thread."""
    if not host_config.posthog_api_key:
        return None
    import posthog

    kwargs = {"project_api_key": host_config.posthog_api_key}
    if host_config.posthog_host:
        kwargs["host"] = host_config.posthog_host
    return posthog.Posthog(**kwargs)


# Module-level stash of the most recently wired HostConfig, read by
# _reinit_posthog_after_fork at fork time — NOT a value frozen at
# registration time, because init_engine_seams (and therefore this stash)
# can be updated by a later call after the hook is already registered.
_current_host_config: HostConfig | None = None

# os.register_at_fork registrations are permanent and accumulate for the
# life of the process; init_engine_seams is called repeatedly (the test
# suite, and multiple create_app() calls within one process), so registering
# unconditionally would stack a growing pile of duplicate after_in_child
# callbacks. This guard makes registration idempotent per process. Unlike
# pool.py's _install_fork_hook (called once at import time, at module
# scope), this one is invoked from inside init_engine_seams itself, so it
# needs its own one-time check rather than relying on Python's
# single-import semantics.
_POSTHOG_FORK_HOOK_INSTALLED = False


def _reinit_posthog_after_fork() -> None:
    """After_in_child hook (#129): rebuild the PostHog client in the forked
    worker instead of running with the master's inherited one.

    posthog.Posthog spawns a background Consumer daemon thread at
    construction time; threads do not survive fork(), so the inherited
    client's consumer is dead in every worker and capture() calls would
    silently pile up in an undrained queue. The fix is to build a fresh
    client against the current host_config, exactly the pattern
    jobcannon.db.pool's _reinit_after_fork uses for the DB pool.

    Unlike the DB pool, there is no shared-socket hazard here to guard
    against: PostHog batches over per-request HTTP, not a long-held
    connection, so there's nothing to stash in an orphan list. The old
    (dead-consumer) client reference is simply replaced, never closed —
    calling shutdown()/flush() on it would try to join a thread that no
    longer exists in this process.

    Must never raise: this runs inside fork machinery, where an exception
    would surface far from anything that could sensibly handle it.
    """
    if _current_host_config is None:
        # No config was ever wired in this process (e.g. a fork before
        # init_engine_seams ran) — nothing to rebuild against.
        return
    try:
        client = _build_posthog_client(_current_host_config)
        posthog_client.set_posthog_client(client)
        if client is not None:
            logger.info("posthog client rebuilt after fork in pid %d", os.getpid())
        else:
            logger.info(
                "posthog client absent after fork in pid %d (no API key configured)",
                os.getpid(),
            )
    except Exception:
        logger.exception("post-fork posthog client rebuild failed in pid %d", os.getpid())


def _install_posthog_fork_hook() -> bool:
    global _POSTHOG_FORK_HOOK_INSTALLED
    if _POSTHOG_FORK_HOOK_INSTALLED:
        return False
    register_at_fork = getattr(os, "register_at_fork", None)
    if register_at_fork is None:
        # Windows (dev) has no os.register_at_fork; there is no fork() to
        # guard against there, so this is a silent no-op rather than an error.
        return False
    register_at_fork(after_in_child=_reinit_posthog_after_fork)
    _POSTHOG_FORK_HOOK_INSTALLED = True
    return True


def init_engine_seams(host_config: HostConfig) -> None:
    global _current_host_config
    _current_host_config = host_config
    pool_mod.open_pool(host_config.database_url)
    services.set_services(build_scan_services(host_config))
    runtime_config.set_config_provider(lambda: host_config.runtime)
    # min_meaningful_len left at its default (0): that padding gate exists for
    # email-body semantics (a very short body is a meta/empty email); for an
    # ATS-only host every API response — including [] — is meaningful.
    extraction_health.set_recorder(record_scan_health)
    posthog_client.set_posthog_client(_build_posthog_client(host_config))
    posthog_client.set_analytics_salt(host_config.analytics_pseudonym_salt)
    _install_posthog_fork_hook()


def teardown_engine_seams() -> None:
    global _current_host_config
    # Clear the stash so a fork that (improbably) races a teardown rebuilds
    # against "no config" (client -> None) rather than resurrecting a client
    # teardown just deliberately removed. Deliberately does NOT reset
    # _POSTHOG_FORK_HOOK_INSTALLED: that flag tracks a permanent OS-level
    # registration, not the current wiring state, so un-registering it here
    # would make the next init_engine_seams() call register a second
    # after_in_child hook alongside the still-installed first one.
    _current_host_config = None
    posthog_client.set_analytics_salt(None)
    posthog_client.set_posthog_client(None)
    extraction_health.set_recorder(None)
    runtime_config.set_config_provider(None)
    services.clear_services()
    pool_mod.close_pool()
