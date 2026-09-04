"""The five-seam startup (1B spec §1) — the ONE call web and worker make.

1. services.set_services(ScanServices(...))      — persistence/hook seam
2. runtime_config.set_config_provider(provider)  — scan-tuning knob seam
3. extraction_health.set_recorder(...)           — health-log seam
4. posthog_client.set_posthog_client(...)        — analytics fan-out seam,
   plus posthog_client.set_analytics_salt(...) riding along in the same
   step: the pseudonymization salt gates whether that fan-out ever reaches
   PostHog with an identifier at all (posthog_client.pseudonymize's
   fail-closed contract) — it is not a separate seam, just the other half
   of seam 4's configuration. posthog_admin.configure(...) (issue #135's
   PostHog person-purge admin credentials) rides along here too, for the
   same reason.
5. task_app.configure(host_config.database_url) — procrastinate defer seam
   (issues #135/#136 HIGH-1). Unlike every seam above, this one does NOT
   open anything here — it only records the DSN. task_app.py's own
   docstring has the full rationale: gunicorn's --preload means this
   function runs once in the master before every worker forks, and opening
   a real connection pool here would be inherited-but-broken in every
   forked child, the same bug class jobcannon.db.pool's fork hook and this
   module's own _reinit_posthog_after_fork hook exist to solve for their
   resources. task_app.ensure_open() does the actual, lazy, per-process
   open, reactively, on first need, post-fork.
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

Verdict on jobcannon#132 ("does the inherited husk's own atexit(join) —
registered by the SDK itself, never rebuilt, never unregistered — block a
real worker's exit?"): NON-ISSUE. CPython's own
os.register_at_fork(after_in_child=threading._after_fork), registered
internally since 3.7, resets every non-current thread's join state AT FORK
TIME — before the after_in_child hook above even runs — so the dead husk's
Consumer.join() returns immediately rather than blocking. (The SDK's
atexit-registered callable is Client.join, which is consumer.pause() — a
bare attribute set — then consumer.join() = Thread.join(), then
poller.stop() only if a feature-flag Poller exists; Poller is also a Thread,
so the same reset applies, and the poller is None on a client that never
loaded flags. queue.join() lives only in flush(), which is not registered.)
Traced in
threading.py on both Python versions this app runs: 3.12.11 (threading.py:
1649 _after_fork -> _reset_internal_locks(False) sets
_is_stopped=True/_tstate_lock=None [955-959] -> join's
_wait_for_tstate_lock finds lock is None, asserts _is_stopped, returns
[1149-1166] — never acquires) and 3.13.5 (threading.py:1555 _after_fork ->
Thread._after_fork()'s non-current-thread branch [937-940], whose own code
comment states the C-level _PyThread_AfterFork() already marked the handle
done at fork time — that step is confirmed here by the Python-level source
read plus an imposed-state join measurement, not by reading
_PyThread_AfterFork's C implementation directly).
tests/host/test_posthog_fork_atexit.py is the empirical, end-to-end closure:
a real fork() + this real after_in_child hook + real atexit handlers,
asserting the child reaches os._exit(0) well under gunicorn's
graceful_timeout — plus a second, confounder-free variant that joins only
the inherited husk directly, bypassing atexit entirely. jobcannon#160's move
to self-hosted Windows CI runners took this test file's fork-gated pair off
CI (os.fork() does not exist on Windows), and jobcannon#162's investigation
found neither WSL nor Docker installed on the runner box, so there was no
CI-leg path to real os.fork() coverage at the time; jobcannon#248 closed
that gap for a while by shelling from the Windows runner into WSL2. CI moved
again after that (public repo, GitHub-hosted `ubuntu-latest`, see ci.yml),
which makes the WSL workaround unnecessary rather than merely redundant:
os.fork() is native on Linux, so both fork-gated tests run for real inside
the ordinary `test` job's pytest invocation with no opt-out, in addition to
the dedicated `fork-proof` job that also runs them (see ci.yml). Python
version is whatever `astral-sh/setup-uv` resolves for the pinned "3.12"
spec at run time (3.12.14 as of the CI moves referenced above) — the
3.13.5 chain still rests on the source-read citation above plus an
on-demand run of tests/host/test_posthog_fork_atexit.py on a 3.13 POSIX
interpreter, not routine CI coverage. That test file's own
`_require_fork_or_fail_loud` gate still fails CI loudly instead of silently
skipping if real os.fork() coverage is ever lost again without
JC_FORK_TESTS_UNAVAILABLE=1 being restored in ci.yml.
Because of all this, the husk is deliberately left alone — never
shutdown()/flush()/join()ed manually — leaving its already-harmless
atexit(join) in place costs nothing, while calling shutdown()/flush() on it
would risk a stray POST attempt from a dead consumer thread for no benefit.
See jobcannon#137 for a separate, real finding this verdict does NOT cover:
the *rebuilt live* client's own atexit(join) could block up to ~67s flushing
against a down PostHog endpoint, which DOES exceed gunicorn's default
graceful_timeout (30s) — that risk lived in the live client B, not this
husk, so it got its own fix: _build_posthog_client below bounds the
client's retry math (timeout/max_retries) and _install_bounded_atexit_flush
replaces the SDK's own unbounded atexit(join) with a time-boxed equivalent,
so a worst-case flush now completes in ~9s with a 10s hard backstop rather
than ~67s. tests/host/test_posthog_fork_atexit.py's newer tests cover this
end-to-end, including a real subprocess against a host that accepts a
connection but never answers.
"""

from __future__ import annotations

import atexit
import logging
import os

from jobcannon.db import _companies, _direct_link, _jd_full, _jobs
from jobcannon.db import pool as pool_mod
from jobcannon.engine import extraction_health, runtime_config, services
from jobcannon.host import model_provider as _model_provider
from jobcannon.host import posthog_admin, posthog_client, task_app
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

# jobcannon#137: bound the worst-case atexit(join) flush duration for the
# PostHog client this module constructs, so a worker exit during a PostHog
# outage completes comfortably inside gunicorn's graceful_timeout (render.yaml
# pins --graceful-timeout 30; tests/host/test_render_config.py cross-checks
# it against _POSTHOG_ATEXIT_JOIN_TIMEOUT_S below).
#
# posthog.Client's own defaults (timeout=15, max_retries=3 -> backoff's
# max_tries=retries+1=4) retry a batch stuck against a down endpoint for
# ~4*15 + (1+2+4) = 67s of wall-clock time inside the atexit handler — past
# gunicorn's default 30s graceful_timeout, so the platform SIGKILLs the
# worker mid-flush on every deploy during an outage (this is the real
# finding; distinct from #132's already-harmless inherited husk, see the
# module docstring above).
#
# timeout=4, max_retries=1 -> max_tries=2: worst case per consumer is
# 2*4 + 1 (one backoff.expo sleep, factor=1 * base(2)**0) =~ 9s.
# _POSTHOG_ATEXIT_JOIN_TIMEOUT_S is a HARD backstop on top of that bound
# (not just an inference from today's retry math) — see
# _install_bounded_atexit_flush below.
_POSTHOG_TIMEOUT_S = 4
_POSTHOG_MAX_RETRIES = 1
_POSTHOG_ATEXIT_JOIN_TIMEOUT_S = 10.0


def build_scan_services(host_config: HostConfig) -> services.ScanServices:
    return services.ScanServices(
        connection_factory=pool_mod.connection_factory,
        upsert_job=_jobs.upsert_job,
        set_jd_full=_jd_full.set_jd_full,
        upsert_company=_companies.upsert_company,
        set_direct_url=_direct_link.set_direct_url,
        stamp_direct_url_checks=_direct_link.stamp_direct_url_checks,
        annotate_posting_apply_url=_jobs.annotate_posting_apply_url,  # PORT-SEAM: L-0075, flat re-adaptation (jobcannon/db/_jobs.py)
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
        # L-0036 PR-1: the hosted model
        # dispatcher + its cost/usage sink, both module-level functions
        # (not per-tenant closures — call_model takes user_id per call and
        # builds a fresh, per-call CredentialResolver internally; see
        # jobcannon.host.model_provider.call_model's docstring).
        call_model=_model_provider.call_model,
        record_cost=_model_provider.record_cost,
    )


def _build_posthog_client(host_config: HostConfig):
    """Construct a PostHog client when an API key is configured, else None
    (inert no-op fan-out in dev/CI). Constructing the client does not open a
    network connection — events batch on a background consumer thread.

    Passes the bounded timeout/max_retries (jobcannon#137, see the
    _POSTHOG_* constants above) instead of the SDK's own defaults, and
    replaces the SDK's unbounded atexit(join) with a time-boxed equivalent
    via _install_bounded_atexit_flush — every caller (initial wiring AND
    the post-fork rebuild below) gets the bound, from one place."""
    if not host_config.posthog_api_key:
        return None
    import posthog

    kwargs = {
        "project_api_key": host_config.posthog_api_key,
        "timeout": _POSTHOG_TIMEOUT_S,
        "max_retries": _POSTHOG_MAX_RETRIES,
    }
    if host_config.posthog_host:
        kwargs["host"] = host_config.posthog_host
    client = posthog.Posthog(**kwargs)
    _install_bounded_atexit_flush(client)
    return client


def _install_bounded_atexit_flush(client) -> None:
    """Replace the SDK's own unbounded atexit(client.join) with a
    time-boxed equivalent (jobcannon#137).

    posthog.Client.__init__ (posthog 3.25.0, client.py:316) unconditionally
    registers `atexit.register(self.join)` when constructed with the
    library's default `send=True` (this app never overrides it). `Client.
    join()` calls `consumer.pause()` then `Thread.join()` with NO timeout —
    unbounded from atexit's point of view even after the
    _POSTHOG_TIMEOUT_S/_POSTHOG_MAX_RETRIES bound above, since that bound
    is "usually ~9s today", not a hard guarantee (a future posthog release
    could change the retry internals without changing these two kwargs'
    meaning). Unregistering the SDK's callback and registering our own
    bounded one makes worker-exit latency a hard invariant instead of an
    inference from current retry math.

    `atexit.unregister` compares callables by equality, and a bound
    method's equality is defined by (instance, underlying function), so
    this correctly removes the specific registration `Client.__init__` just
    made for THIS client —
    tests/host/test_posthog_fork_atexit.py::test_bounded_atexit_flush_replaces_sdk_default
    spies on the real `atexit.register`/`unregister` and confirms
    `unregister` is called with that exact bound method (the SDK's
    init-time registration) and that a *different* callable is registered
    in its place; it does not itself inspect CPython's atexit registry
    state, so it proves the call was made correctly, not that the
    interpreter's bookkeeping dropped the entry (those are the same thing
    under documented `atexit.unregister` equality semantics, but the test
    verifies the call, not the registry).

    A join that times out leaves the daemon consumer thread running in the
    background — harmless: daemon threads never block interpreter exit on
    their own (only an explicit, unbounded Thread.join() does, which is
    exactly what this replaces), and the process is exiting either way.

    client.poller is always None in this app today (nothing here ever
    calls Client.load_feature_flags(), the only thing that sets it) — the
    `if client.poller` branch below mirrors Client.join()'s own shape for
    fidelity, but note it is NOT itself bounded (Poller.stop() calls a
    plain, timeout-less Thread.join()); if a future change wires up feature
    flag polling, this function needs a bounded poller stop too.
    """
    atexit.unregister(client.join)

    def _bounded_join() -> None:
        for consumer in client.consumers or ():
            consumer.pause()
            try:
                consumer.join(timeout=_POSTHOG_ATEXIT_JOIN_TIMEOUT_S)
            except RuntimeError:
                # Consumer thread was never started (mirrors the SDK's own
                # Client.join(), client.py:791-795) — currently unreachable
                # in this app (_build_posthog_client always constructs with
                # the SDK's default send=True, so every consumer's .start()
                # runs), kept for the same reason the SDK keeps it: a future
                # send=False/thread=0 wiring change shouldn't turn a no-op
                # into an atexit-time traceback.
                continue
            if consumer.is_alive():
                logger.warning(
                    "posthog consumer still flushing after %.0fs at exit; "
                    "abandoning it so worker shutdown is not blocked",
                    _POSTHOG_ATEXIT_JOIN_TIMEOUT_S,
                )
        if client.poller:
            client.poller.stop()

    atexit.register(_bounded_join)


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
    # Issue #135's PostHog person-purge admin credentials ride along in
    # seam 4 too, same rationale as the analytics salt above: not a fifth
    # seam, just more of seam 4's configuration. Threaded unconditionally
    # (both web and worker call init_engine_seams) even though the purge
    # itself only ever runs worker-side (jobcannon.host.tasks.
    # purge_posthog_person) -- HostConfig's three new fields are simply
    # None wherever POSTHOG_PERSONAL_API_KEY/POSTHOG_PROJECT_ID/
    # POSTHOG_ADMIN_API_HOST aren't set (e.g. the web service, which
    # render.yaml never declares them on), so this is a harmless no-op
    # there rather than something that needs process-type branching.
    posthog_admin.configure(
        personal_api_key=host_config.posthog_personal_api_key,
        project_id=host_config.posthog_project_id,
        host=host_config.posthog_admin_api_host,
    )
    _install_posthog_fork_hook()
    # Seam 5 (issues #135/#136 HIGH-1): bookkeeping only, no I/O -- see
    # task_app.py's docstring and the module docstring above for why the
    # actual open is deliberately deferred to task_app.ensure_open().
    task_app.configure(host_config.database_url)


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
    task_app.close()
    task_app.configure(None)
    posthog_admin.configure(personal_api_key=None, project_id=None, host=None)
    posthog_client.set_analytics_salt(None)
    posthog_client.set_posthog_client(None)
    extraction_health.set_recorder(None)
    runtime_config.set_config_provider(None)
    services.clear_services()
    pool_mod.close_pool()
