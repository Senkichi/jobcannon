"""Decisive Linux-only proof for jobcannon#132, plus jobcannon#137's bounded
PostHog atexit flush (the latter's newer tests near the bottom of this file
need no os.fork() and also run on Windows dev).

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

CI caveat (see wiring.py's module docstring for the full citation, and
jobcannon#162/#248 for the investigations this paragraph reflects): as of
jobcannon#160, this repo's CI ran ONLY on self-hosted Windows runners --
os.fork() does not exist on Windows, so neither test in this file's
fork-gated pair could execute in the `test` job itself. jobcannon#162 found
WSL and Docker both absent from the runner box at the time, leaving no
CI-leg path to real os.fork() coverage; jobcannon#248 closed that gap by
having a dedicated `fork-proof` job shell from the Windows runner into
WSL2. CI has since moved again (public repo, GitHub-hosted `ubuntu-latest`
-- see ci.yml), which makes the whole Windows/WSL problem moot rather than
merely worked around: os.fork() is native on Linux, so this file's two
fork-gated tests now execute for real (PASS, not skip) inside the ordinary
`test` job's pytest invocation, with no `JC_FORK_TESTS_UNAVAILABLE`
opt-out set there anymore. A `fork-proof` job still exists in ci.yml and
still runs this same pair on its own, deliberately, as a dedicated
check-run signal -- see that job's own comment. The
`_require_fork_or_fail_loud` gate below (not a plain skipif) is what made
the original silent-skip failure structurally impossible to repeat
unnoticed: CI would FAIL rather than silently re-skip if a future CI leg
lost real os.fork() coverage without JC_FORK_TESTS_UNAVAILABLE=1 being set
to explicitly acknowledge the gap.
"""

from __future__ import annotations

import atexit
import os
import pathlib
import signal
import subprocess
import sys
import time
import traceback

import pytest

from jobcannon.host import posthog_client, wiring
from jobcannon.host.config import HostConfig

ROOT = pathlib.Path(__file__).resolve().parents[2]

_JC_FORK_TESTS_UNAVAILABLE_ENV = "JC_FORK_TESTS_UNAVAILABLE"


def _require_fork_or_fail_loud() -> None:
    """Gate for the two real os.fork() tests below (POSIX-only). Local
    Windows dev quietly skips -- same as any other platform-gated test.
    Caveat: detection below treats ANY non-empty `CI` env var as on-CI, so
    a dev box with a stray `CI` value set by unrelated tooling (some
    npm/yarn/cargo wrappers use it too) and no opt-out would `pytest.fail`
    here instead of skipping -- not narrowed to `GITHUB_ACTIONS` alone
    since this repo's CI is exclusively GitHub Actions and erring toward
    on_ci=True is the fail-loud-safe direction this gate wants anyway.

    CI is currently fork-capable (ubuntu-latest, native os.fork()), so this
    gate is a no-op today -- it returns at the `hasattr` check above before
    ever reaching the CI logic below. That logic stays as a structural
    guard against the exact failure jobcannon#162 found: after jobcannon#160
    moved this repo's CI to self-hosted Windows runners, os.fork() stopped
    existing there, and a plain skipif made both tests SKIP silently on
    every CI run forever -- CI stayed green either way, so nobody noticed
    the #132 proof lost its only CI coverage. If CI ever moves back to a
    platform without os.fork() again, missing os.fork() FAILS the test
    unless the workflow explicitly opts out via JC_FORK_TESTS_UNAVAILABLE=1
    -- ci.yml does not set that today (deleted when CI moved to
    ubuntu-latest), so restoring it (with a comment citing this history) is
    what a future non-fork-capable CI leg would need to add.
    """
    if hasattr(os, "fork"):
        return
    on_ci = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))
    opted_out = os.environ.get(_JC_FORK_TESTS_UNAVAILABLE_ENV) == "1"
    if on_ci and not opted_out:
        pytest.fail(
            "os.fork() is unavailable on this CI runner and "
            f"{_JC_FORK_TESTS_UNAVAILABLE_ENV}=1 is not set -- refusing to "
            "silently skip (jobcannon#162). Either this CI leg gained a "
            "path to real os.fork() coverage (great -- then this failure "
            "is a real regression to chase down, not a config problem), "
            f"or the workflow needs {_JC_FORK_TESTS_UNAVAILABLE_ENV}=1 set "
            "explicitly to acknowledge the gap."
        )
    pytest.skip("fork-only (POSIX) -- Windows dev has no os.fork")


def test_require_fork_or_fail_loud_fails_on_ci_without_optout(monkeypatch):
    """jobcannon#162 proof, case 1: CI set, no opt-out, no os.fork -> must
    FAIL, not skip. This is the exact silent-skip failure mode #162 found
    (CI stayed green while losing the #132 proof's only coverage) made
    structurally impossible to repeat unnoticed."""
    monkeypatch.delattr(os, "fork", raising=False)
    monkeypatch.setenv("CI", "1")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv(_JC_FORK_TESTS_UNAVAILABLE_ENV, raising=False)
    with pytest.raises(pytest.fail.Exception):
        _require_fork_or_fail_loud()


def test_require_fork_or_fail_loud_skips_on_ci_with_optout(monkeypatch):
    """Case 2: CI set, workflow's documented opt-out present -> skips. This
    is ci.yml's actual configuration today (JC_FORK_TESTS_UNAVAILABLE=1 on
    the pytest step, with a comment citing #162)."""
    monkeypatch.delattr(os, "fork", raising=False)
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv(_JC_FORK_TESTS_UNAVAILABLE_ENV, "1")
    with pytest.raises(pytest.skip.Exception):
        _require_fork_or_fail_loud()


def test_require_fork_or_fail_loud_skips_off_ci(monkeypatch):
    """Case 3: neither CI nor GITHUB_ACTIONS set (ordinary local Windows
    dev) -> skips, same as every other platform-gated test. A local dev
    run must never fail just for lacking os.fork()."""
    monkeypatch.delattr(os, "fork", raising=False)
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv(_JC_FORK_TESTS_UNAVAILABLE_ENV, raising=False)
    with pytest.raises(pytest.skip.Exception):
        _require_fork_or_fail_loud()


def test_require_fork_or_fail_loud_noop_when_fork_exists(monkeypatch):
    """Case 4: os.fork present (the real POSIX path, or a hypothetical
    future fork-capable CI leg) -> returns normally, no skip or fail,
    regardless of CI env -- the gate only ever engages when os.fork is
    actually missing."""
    monkeypatch.setattr(os, "fork", lambda: 0, raising=False)
    monkeypatch.setenv("CI", "1")
    monkeypatch.delenv(_JC_FORK_TESTS_UNAVAILABLE_ENV, raising=False)
    _require_fork_or_fail_loud()  # must not raise


def test_require_fork_or_fail_loud_noop_when_fork_exists_and_optout_set(monkeypatch):
    """Case 5 (PR #213 refuter-1 MED): fork present + CI set + the opt-out
    ALSO set -> still returns normally, never skip/fail. Pins the exact
    ordering the gate depends on -- `hasattr(os, "fork")` must be checked
    BEFORE JC_FORK_TESTS_UNAVAILABLE. Cases 1-3 all monkeypatch os.fork
    away, so none of them can distinguish "gate checks fork first" from
    "gate checks the opt-out first"; only a fork-present + opt-out-set
    case can. That reordering would silently re-skip these tests on a real
    future fork-capable CI leg that still carries today's opt-out, which
    is the precise #162 failure mode this gate exists to make impossible.

    Deliberately does NOT call `_require_fork_or_fail_loud()` bare: a
    hoisted opt-out check makes it raise `pytest.skip.Exception`, and an
    *uncaught* skip would report this test as SKIPPED, not FAILED --
    which would NOT break CI's `tests-passed` gate (skips are not
    failures), silently defeating the point of this case. Catching both
    outcome exceptions and converting them to an explicit `pytest.fail()`
    is what makes the regression an actual red build.
    """
    monkeypatch.setattr(os, "fork", lambda: 0, raising=False)
    monkeypatch.setenv("CI", "1")
    monkeypatch.setenv(_JC_FORK_TESTS_UNAVAILABLE_ENV, "1")
    try:
        _require_fork_or_fail_loud()  # must return normally -- fork wins over opt-out
    except (pytest.skip.Exception, pytest.fail.Exception) as exc:
        pytest.fail(
            "_require_fork_or_fail_loud() must return normally when "
            "os.fork() is present, even with the opt-out set -- got "
            f"{type(exc).__name__}: {exc}. This means the opt-out check "
            "ran before the fork check, reintroducing jobcannon#162's "
            "silent-skip failure mode on a future fork-capable CI leg."
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

    `atexit` runs registered callbacks in LIFO order: the rebuilt clients are
    registered AFTER the inherited husk A (one B is constructed inside the fork
    hook, and `child_body` below builds a second, B', by calling the hook
    function explicitly), so `atexit._run_exitfuncs()` runs B'.join(), then
    B.join(), then A.join() -- all three must complete for the
    child to reach `os._exit(0)`. Reaching it at all, within the bound, is
    the proof: a `join()` that blocked forever would never let the child get
    there, so the parent's own bounded wait is what would fail instead.
    """
    _require_fork_or_fail_loud()
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
    _require_fork_or_fail_loud()
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


def test_build_posthog_client_bounds_worst_case_flush_under_graceful_timeout():
    """jobcannon#137's explicit ask: guard the constructed client's
    timeout/max_retries product against a silent library-default bump
    silently reopening the ~67s worst case (posthog defaults: timeout=15,
    max_retries=3 -> max_tries=4 -> 4*15 + (1+2+4) = 67s).

    Computes the same worst-case formula wiring.py's own comment derives
    (max_tries * timeout + the backoff.expo sleeps between attempts) from
    the REAL constructed client/consumer's attributes, not from the
    wiring.py constants directly -- so this would also catch a mismatch
    between what wiring.py intends to pass and what the client actually
    ends up configured with.

    No fork/subprocess needed: runs on Windows dev and CI alike.
    """
    host_config = _host_config()
    client = wiring._build_posthog_client(host_config)
    assert client is not None
    try:
        assert client.timeout == wiring._POSTHOG_TIMEOUT_S
        assert len(client.consumers) == 1
        consumer = client.consumers[0]
        assert consumer.retries == wiring._POSTHOG_MAX_RETRIES

        max_tries = consumer.retries + 1
        # backoff.expo default: factor=1, base=2 -> sleep(n) = 2**n, one
        # sleep between each pair of attempts (max_tries - 1 of them).
        backoff_sleeps_s = sum(2**n for n in range(max_tries - 1))
        worst_case_s = max_tries * consumer.timeout + backoff_sleeps_s

        assert worst_case_s <= wiring._POSTHOG_ATEXIT_JOIN_TIMEOUT_S
        # The hard backstop itself must leave real headroom under
        # gunicorn's graceful_timeout (render.yaml's committed value,
        # cross-checked against this same constant in
        # test_render_config.py) -- not just be numerically less than it.
        assert wiring._POSTHOG_ATEXIT_JOIN_TIMEOUT_S <= 15
    finally:
        client.join()  # cleanup: no queued items, returns promptly


def _captured_bounded_join_closure(monkeypatch, host_config):
    """Shared helper: build a real client via _build_posthog_client while
    spying on atexit.register, and return the exact `_bounded_join` closure
    it installs plus the client itself. Lets a test invoke the closure
    directly without waiting for process exit.

    Takes the LAST register call, not a fixed index/count: `_build_posthog_client`
    does `import posthog` internally, and on this process's FIRST-EVER import
    of posthog/requests/certifi, certifi's own `exit_cacert_ctx` atexit
    registration fires too (module-level, so only once per process — whether
    it happens here depends on test execution order, not on anything this
    function does). Regardless of that, `_install_bounded_atexit_flush`
    always runs last inside `_build_posthog_client` (client.py:316's own
    atexit(self.join) registers when Posthog(**kwargs) is constructed, then
    _install_bounded_atexit_flush unregisters it and registers the bounded
    replacement — see the ["register", "unregister", "register"] tail
    test_bounded_atexit_flush_replaces_sdk_default asserts), so the bounded
    closure is always the last register call, whether it's call 2 or 3."""
    calls: list[tuple[str, object]] = []
    orig_register = atexit.register

    def spy_register(func):
        calls.append(("register", func))
        return orig_register(func)

    monkeypatch.setattr(atexit, "register", spy_register)
    client = wiring._build_posthog_client(host_config)
    assert client is not None
    assert len(calls) >= 2, (
        "expected at least the SDK's init-time register plus the bounded replacement"
    )
    bounded_join = calls[-1][1]
    assert bounded_join != client.join, (
        "last register must be the bounded replacement, not the SDK's join"
    )
    return bounded_join, client


def test_bounded_join_swallows_runtimeerror_from_unstarted_consumer(monkeypatch):
    """Mirrors the SDK's own Client.join() (client.py:791-795): a consumer
    whose thread never started raises RuntimeError from Thread.join(), and
    the SDK swallows it with a bare `except RuntimeError: pass`. Corroborated
    finding (refuter-1 LOW #1 / devin LEAD 4): _bounded_join must not let
    that divergence surface an unhandled exception inside an atexit handler.

    Currently unreachable in production (this app always builds with the
    SDK's default send=True, so every consumer's .start() runs) — this test
    exercises the guard directly via a fake consumer rather than trying to
    construct a real never-started one.
    """
    host_config = _host_config()
    bounded_join, client = _captured_bounded_join_closure(monkeypatch, host_config)
    try:

        class _NeverStartedConsumer:
            def pause(self):
                pass

            def join(self, timeout=None):
                raise RuntimeError("threads can only be started once")

            def is_alive(self):
                pytest.fail("is_alive() must not be reached after a RuntimeError")

        monkeypatch.setattr(client, "consumers", [_NeverStartedConsumer()])
        bounded_join()  # must not raise
    finally:
        client.join()


def test_bounded_atexit_flush_replaces_sdk_default(monkeypatch):
    """L3-wired check for _install_bounded_atexit_flush (jobcannon#137):
    confirms _build_posthog_client actually swaps the SDK's own
    atexit(client.join) registration for a different callable, in the
    right order -- not just that _install_bounded_atexit_flush exists and
    is theoretically correct in isolation.

    Spies on the real atexit.register/unregister (still calling through to
    the originals, so the process's real atexit state stays correct) to
    record the exact call sequence _build_posthog_client produces.
    """
    calls: list[tuple[str, object]] = []
    orig_register = atexit.register
    orig_unregister = atexit.unregister

    def spy_register(func):
        calls.append(("register", func))
        return orig_register(func)

    def spy_unregister(func):
        calls.append(("unregister", func))
        return orig_unregister(func)

    monkeypatch.setattr(atexit, "register", spy_register)
    monkeypatch.setattr(atexit, "unregister", spy_unregister)

    host_config = _host_config()
    client = wiring._build_posthog_client(host_config)
    assert client is not None
    try:
        # Exactly three atexit calls happen while building one client:
        # (1) posthog.Client.__init__ itself registers atexit(self.join)
        #     (client.py:316, unconditional when send=True, the default) --
        #     BEFORE _build_posthog_client gets a chance to touch anything.
        # (2) _install_bounded_atexit_flush unregisters that exact
        #     registration.
        # (3) _install_bounded_atexit_flush registers its own replacement.
        kinds = [kind for kind, _ in calls]
        assert kinds == ["register", "unregister", "register"], kinds
        assert calls[0][1] == client.join, "SDK's own init-time registration"
        assert calls[1][1] == client.join, "must unregister that exact callable"
        assert calls[2][1] != client.join, (
            "must register a DIFFERENT callable, not just re-register the SDK's own unbounded join"
        )
    finally:
        client.join()


_NEVER_ANSWERS_CHILD_SCRIPT = '''
"""Child process for
test_atexit_flush_bounded_when_posthog_host_never_answers. Builds a real
PostHog client against a local TCP listener that accepts the connection but
never responds -- "a socket that never answers" -- enqueues one event, then
exits via sys.exit(0) exactly like gunicorn's worker exit path. Success is
measured by the PARENT (this file), as wall-clock time for the whole
subprocess."""

import socket
import sys
import threading
import time

from jobcannon.host import wiring
from jobcannon.host.config import HostConfig

listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen(5)
port = listener.getsockname()[1]


def _accept_and_stall():
    while True:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        # Deliberately never conn.recv()/conn.sendall() anything: the
        # client's own request `timeout=` is what has to fire, not a
        # server-side RST/refusal.


threading.Thread(target=_accept_and_stall, daemon=True).start()

host_config = HostConfig(
    database_url="postgresql://u:p@192.0.2.9/db",
    posthog_api_key="phc_test_never_answers",
    posthog_host=f"http://127.0.0.1:{port}",
    analytics_pseudonym_salt="test-salt-unused",
)
client = wiring._build_posthog_client(host_config)
assert client is not None
client.capture(distinct_id="test-user", event="test-event", properties={})
# Give the consumer thread time to dequeue the item and be well into its
# first request() attempt before we exit -- otherwise a lucky-fast exit
# could reach atexit before any upload even started, proving nothing.
time.sleep(1.0)
sys.exit(0)
'''


def test_atexit_flush_bounded_when_posthog_host_never_answers(tmp_path):
    """End-to-end proof of jobcannon#137's fix: a queued event whose target
    host accepts the TCP connection but never answers must not block worker
    exit past a generous bound.

    Runs in a REAL subprocess (not this test process), so the measurement
    is a genuine process-exit wall-clock time -- and, unlike the fork-based
    tests above, this scenario needs no os.fork() at all (it only exercises
    the retry-bound + bounded-atexit-join half of #137's fix, not the
    fork/atexit interaction those tests cover), so it runs on Windows dev
    too, not just POSIX CI.

    Generous margin per this repo's wall-clock flake lessons: assert the
    BOUND (well under gunicorn's 30s graceful_timeout and the hard
    _POSTHOG_ATEXIT_JOIN_TIMEOUT_S backstop's own 10s), not a tight number
    close to the ~9s worst case the retry math predicts. Before this fix,
    the equivalent scenario against library defaults took ~67s -- this
    would fail loudly (subprocess.TimeoutExpired) rather than silently
    passing if the fix regressed.
    """
    script = tmp_path / "never_answers_child.py"
    script.write_text(_NEVER_ANSWERS_CHILD_SCRIPT, encoding="utf-8")

    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=25,  # hard ceiling: a real regression fails fast, not hangs pytest
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, (
        f"child exited nonzero ({result.returncode}); stderr:\\n{result.stderr}"
    )
    assert elapsed < 20.0, (
        f"child took {elapsed:.2f}s to exit against a host that accepts but "
        "never answers -- expected well under gunicorn's 30s "
        "graceful_timeout (generous margin over the ~9-10s worst case the "
        "retry math + hard backstop predict); investigate before treating "
        "this as a flake"
    )
