"""Shared deadlines for tests that spawn a real subprocess.

Why this exists
---------------
A test that spawns a fresh interpreter and imports ``job_finder`` is measuring a
*cold* import of a large package tree, on a box whose disk is shared by up to
eight CI runners across three repositories. On 2026-07-26, with the disk at queue
depth 31 and 60ms average write latency, one such spawn exceeded its 30-second
ceiling and failed the ``main`` branch:

    subprocess.TimeoutExpired: Command '[...python.exe, -c, ...]'
        timed out after 30 seconds

Nothing was wrong with the code under test. The 30 was a *performance budget*
wearing a timeout's clothing.

The distinction that matters
----------------------------
A subprocess timeout in a test is a **hang detector**, not an assertion about
speed. Its only job is to stop a wedged child from burning the CI job's whole
budget. That means:

- Too tight is a real bug: it converts ordinary load into a red build, and a
  gate that goes red for reasons unrelated to the diff is a gate people learn
  to bypass.
- Too loose costs nothing on a healthy run. The timeout is a ceiling that is
  never reached, not a cost that is always paid. A passing spawn takes the same
  handful of seconds whether the ceiling is 30 or 180.

The asymmetry is total, so the value should be generous. 180s still bounds a
genuinely wedged child far below the job's own ``timeout-minutes``, while
sitting roughly two orders of magnitude above a healthy spawn.

Do NOT use this for assertions about how long something *should* take. Those
belong in a test that states its contract as a lower bound on elapsed time --
see ``test_phase_c_cascade_runtime_limit`` in ``tests/test_expiry_checker.py``
for the reasoning, since a sleep can only overrun, never undershoot.
"""

from __future__ import annotations

# Ceiling for `subprocess.run(...)` calls that spawn a fresh Python and import
# the package. Detects a wedged child; deliberately not a speed assertion.
SUBPROCESS_HANG_TIMEOUT_S = 180

# Ceiling for thread-synchronization primitives (`Event.wait`, `Semaphore.acquire`,
# `Thread.join`) inside tests that drive a real ThreadPoolExecutor on a daemon
# thread. Detects a wedged scan/worker; deliberately not a speed assertion.
#
# Same asymmetry as SUBPROCESS_HANG_TIMEOUT_S: too tight converts ordinary CI
# load into a red build (the self-hosted runner shares its CPU across up to
# eight concurrent CI legs, so a thread-pool worker's first scheduling can
# legitimately take seconds — issue #1877 observed >10 s on a loaded box);
# too loose costs nothing because a healthy run reaches the primitive in
# milliseconds and the ceiling is never paid.
#
# 60 s sits roughly two orders of magnitude above a healthy thread start while
# still bounding a genuinely wedged scan far below the CI job's own
# ``timeout-minutes``. Do NOT use this for assertions about how long something
# *should* take.
THREAD_SYNC_HANG_TIMEOUT_S = 60
