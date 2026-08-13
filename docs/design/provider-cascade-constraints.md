# Provider-cascade design constraints

This codebase has no model-provider cascade yet. These two constraints are
binding on whichever change introduces one, recorded now so the implementer
inherits them as requirements rather than re-deriving them from an incident.
Both were learned the hard way in the private predecessor codebase and are
cited by the commit that fixed them there.

## 1. A single monotonic deadline governs the whole cascade

One deadline, computed once at the cascade entry point from the caller's
timeout, decremented at **every** choke point — each provider attempt, each
schema-validation retry, each throttle sleep, each rate-limit backoff sleep.
No per-provider timeout that resets on fallback.

**Why.** Per-provider timeouts multiply instead of bounding: an N-provider
chain with a T-second timeout each has an N×T-second worst case, before
counting retry and backoff sleeps that a per-provider scheme typically does
not charge against any budget at all. That is how a "30-second" call becomes
two minutes. Enforcement must live at the one place that owns the whole
call's lifetime, so a provider added later inherits the budget without having
to remember to.

**Evidence.** Private-repo commit `d61809c5`: the caller's timeout was handed
to every adapter unshrunk on every retry and every fallback, and throttle /
backoff sleeps were never counted against it. The fix is the shape this
constraint prescribes — a single monotonic deadline decremented at every
choke point via one helper, with no per-adapter deadline logic.

## 2. No silent timeout-parameter drops

A provider adapter that cannot honor the timeout it was passed must fail
loudly — refuse at registration or raise at call time — never accept the
parameter and proceed unbounded.

**Why.** A dropped timeout parameter is invisible in green tests: every
assertion about return values still passes, and the defect only manifests as
an unbounded hang under real provider latency, attributed to the provider
rather than the adapter. Loud failure converts a production hang into a
development-time error.

**Evidence.** Private-repo commit `d61809c5` (same incident): two of the
registered provider adapters accepted the timeout argument and silently
discarded it. A related trap fixed in the same commit: `timeout or default`
falsy checks turned an explicit `timeout=0` into the provider default instead
of failing fast.

---

*Scope note.* Both constraints above derive from the deadline-semantics
incident (`d61809c5`). A second private-repo cascade incident (`3c285709`)
concerns content routing across providers rather than deadline semantics; it
is outside this document's scope, which is deliberately limited to the two
timeout constraints.
