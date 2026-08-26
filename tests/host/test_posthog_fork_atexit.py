"""Decisive Linux-only proof for jobcannon#132.

#132 hypothesized: "a gunicorn worker inherits the master's PostHog client
across fork(); at worker exit, atexit runs the inherited husk's
`Client.join()`, which blocks forever on the inherited (dead-in-child)
Consumer thread's tstate lock -- hanging every worker exit."

The judgment-tier analysis (scratchpad/eng/132-analysis.md) found this to be
a NON-ISSUE: CPython's own `os.register_at_fork(after_in_child=
threading._after_fork)` machinery resets every dead-in-child thread's join
state AT FORK TIME (verified via source read against both the 3.13.5
_ThreadHandle model and the 3.12.11 _tstate_lock model -- see wiring.py's
module docstring for the citations), so the inherited husk's `join()`
returns immediately rather than blocking. This test is the empirical,
end-to-end closure of that finding: it does a REAL `os.fork()`, runs the
REAL production hook (`wiring._reinit_posthog_after_fork`, registered via
`wiring._install_posthog_fork_hook`), and runs REAL `atexit` handlers in the
child -- exactly the sequence a gunicorn worker's `sys.exit(0)` triggers at
shutdown (gunicorn's worker exit path runs interpreter finalization, not
`os._exit`; see wiring.py). It proves the husk `Client.join()` (module
level, from the SDK's own `atexit.register(self.join)` in `Client.__init__`)
and the freshly rebuilt live client's `Client.join()` both complete well
within a bound gunicorn's own `graceful_timeout` (default 30s) would never
even approach.

This test needs no Postgres. It exercises ONLY the PostHog half of
wiring.py's seam, via the real `_build_posthog_client` /
`_install_posthog_fork_hook` / `_reinit_posthog_after_fork` functions,
deliberately bypassing `init_engine_seams` itself -- that function's first
call is `pool_mod.open_pool(host_config.database_url)`, which needs a real
DSN. Nothing here depends on POSTGRES_ADMIN_DSN or tests/host/conftest.py's
Postgres fixtures.

If a future Python (or posthog SDK) version regresses the fork-safety this
test proves, this test hangs the child (bounded, so it fails loudly rather
than wedging CI) or reports a nonzero exit code -- not silently.

Two tests, two different confounder profiles:
- `test_atexit_after_real_fork_does_not_hang_worker_exit` runs
  `atexit._run_exitfuncs()` in the child -- full production fidelity (every
  atexit handler in the process fires, exactly what a real `sys.exit(0)`
  would trigger), but on failure it cannot by itself say WHICH handler
  blocked: the husk A's `join()`, the rebuilt live client B's `join()`, or
  something unrelated that pytest/logging/xdist registered.
- `test_husk_join_alone_after_real_fork_returns_promptly` isolates the
  precise #132 claim: it calls the inherited husk's `client_a.join()`
  directly in the child, bypassing `atexit` entirely, so nothing else in the
  process can produce a false positive or misattribute a failure.

CI caveat (see wiring.py's module docstring for the full citation): this
repo's CI (.github/workflows/ci.yml) runs a single Python 3.12 leg. A green
run here closes the 3.12.11 fork/join chain end-to-end. It does NOT by
itself close the 3.13.5 chain (this app's actual Render deploy target) --
that rests on the threading.py:937-940 comment plus the imposed-state
measurement recorded in scratchpad/eng/132-analysis.md, not on a 3.13 CI run.
"""

from __future__ import annotations

import atexit
import os
import signal
import time
import traceback

import pytest

from jobcannon.host import posthog_client, wiring
from jobcannon.host.config import HostConfig

pytestmark = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork-only (POSIX) -- Windows dev has no os.fork"
)

# TEST-NET-1 (RFC 5737): syntactically valid, guaranteed non-routable. Never
# actually dialed -- this test bypasses init_engine_seams's pool_mod.open_pool
# call entirely, so database_url is present only because HostConfig requires
# a value.
_UNROUTABLE_DSN = "postgresql://u:p@192.0.2.9/db"
# The discard port on loopback: almost certainly nothing is listening, and
# even if something were, posthog.Posthog's constructor never dials out --
# events would only be POSTed by the background Consumer thread, and this
# test never enqueues one (no .capture() call anywhere below).
_UNREACHABLE_POSTHOG_HOST = "http://127.0.0.1:9"

_CHILD_WAIT_DEADLINE_S = 10.0
_MAX_ACCEPTABLE_ELAPSED_S = 5.0


def _host_config() -> HostConfig:
    return HostConfig(
        database_url=_UNROUTABLE_DSN,
        posthog_api_key="phc_test_fork_atexit",
        posthog_host=_UNREACHABLE_POSTHOG_HOST,
        analytics_pseudonym_salt="test-salt-unused",
    )


def _run_in_forked_child_and_wait(child_body, *, timeout_hint: str) -> float:
    """Shared fork/wait/assert skeleton for both tests below. `child_body()`
    runs in the child with no arguments; it must not return control to
    pytest's own harness (raising `SystemExit`/returning normally would both
    unwind into `CallInfo.from_call`, which catches `BaseException` --
    corrupting this test's own exit-status protocol and duplicating output).
    This wrapper enforces that: it always calls `os._exit()` itself, exit
    code 0 on a clean return from `child_body`, 70 if it raised (with the
    traceback printed first, since the child's stdout/stderr is the only
    place that traceback can surface -- pytest's own reporting never sees
    this frame).

    Bounds the parent's wait so a real hang here can never wedge CI: on
    timeout it SIGKILLs the child and fails with `timeout_hint` explaining
    what a timeout would mean for the specific claim that test is checking.
    Returns elapsed wall-clock seconds for the caller's own speed assertion.
    """
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            child_body()
        except BaseException:
            traceback.print_exc()
            code = 70
        os._exit(code)

    start = time.monotonic()
    deadline = start + _CHILD_WAIT_DEADLINE_S
    status = None
    while time.monotonic() < deadline:
        reaped_pid, status = os.waitpid(pid, os.WNOHANG)
        if reaped_pid == pid:
            break
        time.sleep(0.02)
    else:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        pytest.fail(
            f"child was not reaped within {_CHILD_WAIT_DEADLINE_S:.0f}s and "
            f"was SIGKILLed -- {timeout_hint}"
        )
    elapsed = time.monotonic() - start

    assert os.WIFEXITED(status), f"child did not exit normally: status={status!r}"
    assert os.WEXITSTATUS(status) == 0, (
        f"child raised (exit code {os.WEXITSTATUS(status)}, expected 0 -- "
        "see the child's stderr in the pytest output above for the traceback)"
    )
    assert elapsed < _MAX_ACCEPTABLE_ELAPSED_S, (
        f"child exited cleanly but took {elapsed:.2f}s -- well above the "
        f"{_MAX_ACCEPTABLE_ELAPSED_S:.0f}s bound expected if #132 is a "
        "non-issue; investigate before treating this as a flake"
    )
    return elapsed


def test_atexit_after_real_fork_does_not_hang_worker_exit(monkeypatch):
    """A forked child that runs `atexit` at what would be a gunicorn worker's
    `sys.exit(0)` must terminate cleanly and quickly -- proving neither the
    inherited husk's `Client.join()` nor the post-fork rebuilt client's
    `Client.join()` blocks worker exit.

    `atexit` runs registered callbacks in LIFO order: the rebuilt client B is
    registered AFTER the inherited husk A (B is only constructed inside the
    fork hook, which runs after A already exists), so `atexit._run_exitfuncs()`
    below runs B.join() first, then A.join() -- both must complete for the
    child to reach `os._exit(0)`. Reaching it at all, within the bound, is
    the proof: a `join()` that blocked forever would never let the child get
    there, so the parent's own bounded wait is what would fail instead.
    """
    host_config = _host_config()

    # Real seam pieces, exactly what init_engine_seams wires for PostHog
    # (see wiring.init_engine_seams's last three lines) -- built directly to
    # avoid init_engine_seams's pool_mod.open_pool call.
    monkeypatch.setattr(wiring, "_current_host_config", host_config)
    client_a = wiring._build_posthog_client(host_config)
    # Inert-vs-active: an API key must build a REAL client (the husk), not
    # the None wiring.py returns when unconfigured -- otherwise this test
    # would exercise nothing.
    assert client_a is not None
    monkeypatch.setattr(posthog_client, "_client", client_a)
    # Precondition #132 hypothesized would deadlock at exit once this thread
    # dies in fork(): confirm it's genuinely alive pre-fork, not e.g. an SDK
    # version that skips consumer construction.
    assert client_a.consumers and all(c.is_alive() for c in client_a.consumers)

    wiring._install_posthog_fork_hook()  # real os.register_at_fork registration

    def child_body() -> None:
        # os.fork() already ran this synchronously as the registered
        # after_in_child hook, before returning here (that's the production
        # path: the ONLY invocation a real gunicorn worker gets). Calling it
        # again is an idempotent, explicit exercise of the exact function
        # under test -- it builds one extra live client+consumer that also
        # gets join()ed below, bounded by the same flush_interval as the
        # first.
        wiring._reinit_posthog_after_fork()
        rebuilt = posthog_client._client
        assert rebuilt is not None and rebuilt is not client_a
        atexit._run_exitfuncs()  # runs every registered Client.join()

    _run_in_forked_child_and_wait(
        child_body,
        timeout_hint=(
            "some atexit handler blocked worker exit. This runs every "
            "registered handler in the process (posthog's Client.join on "
            "the inherited husk AND the rebuilt live client, plus anything "
            "else pytest/logging/xdist registered), so it cannot say which "
            "one by itself -- run "
            "test_husk_join_alone_after_real_fork_returns_promptly to "
            "isolate the husk specifically before concluding #132 is real"
        ),
    )

    # The parent's own consumer thread (client_a's) was never touched by
    # anything the child did -- fork() gave the child an independent copy of
    # process memory, so this join is purely local cleanup, bounded by the
    # same flush_interval.
    client_a.join()


def test_husk_join_alone_after_real_fork_returns_promptly(monkeypatch):
    """Confounder-free isolation of the precise #132 claim: does the
    inherited husk A's own `Client.join()` -- and nothing else in the
    process -- block after a real `fork()`?

    Calls `client_a.join()` directly in the child rather than going through
    `atexit._run_exitfuncs()`, so no other registered handler (the rebuilt
    live client B's own `atexit(join)`, or anything pytest/logging/xdist
    registered) can produce a false positive or blur which handler is
    responsible if this ever goes red. Complements
    `test_atexit_after_real_fork_does_not_hang_worker_exit`, which proves
    full-stack production fidelity but, on a failure, cannot say which
    handler was at fault.
    """
    host_config = _host_config()
    monkeypatch.setattr(wiring, "_current_host_config", host_config)
    client_a = wiring._build_posthog_client(host_config)
    assert client_a is not None
    assert client_a.consumers and all(c.is_alive() for c in client_a.consumers)
    monkeypatch.setattr(posthog_client, "_client", client_a)

    # Production parity: a real gunicorn master always forks under this
    # hook, so it's installed here too (idempotent -- may already be
    # installed process-wide by the other test in this module, in which
    # case this is a no-op). Either way, os.fork() below will run it
    # automatically and silently build a live client B in the child's
    # background BEFORE child_body() even starts -- that is unavoidable
    # once the hook is registered (os.register_at_fork callbacks are
    # permanent for the process, not scoped to one call site). It does not
    # confound this test: child_body() below only ever calls
    # `client_a.join()` directly, never atexit, so B's own atexit(join) is
    # never invoked here -- B exists but is inert to this assertion.
    wiring._install_posthog_fork_hook()

    def child_body() -> None:
        client_a.join()  # the exact #132 claim: husk join alone, post-fork

    _run_in_forked_child_and_wait(
        child_body,
        timeout_hint=(
            "the inherited husk's own Client.join() blocked worker exit "
            "with nothing else in the process able to confound this result "
            "-- #132's hang is real on this Python/posthog version"
        ),
    )

    client_a.join()  # parent-side cleanup; independent post-fork copy
