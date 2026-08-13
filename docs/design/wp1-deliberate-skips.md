# Deliberate non-ports (WP-1 resync)

A skip that is not written down is indistinguishable from an oversight at the
next resync. This document records the deliberate non-ports from the private
predecessor codebase, each with its evidence commit (or an explicit
private-only marker) and the rationale. Entries are mechanical records, not
roadmap statements.

For skips of the "cannot occur hosted" class, the entry must carry all four
ruling steps: **(1)** name the private failure mode; **(2)** name the hosted
mechanism that makes it unreachable; **(3)** cite the file:line of that
mechanism; **(4)** state what would have to change for the skip to become
invalid. A skip missing step 4 is not a ruling.

---

## 1. Desktop connect/read timeout split for HTTP callers

**Private evidence:** commit `09432fe4` — adds an explicit connect/read
timeout split (`_CONNECT_TIMEOUT` / `_READ_TIMEOUT` / `_REQUEST_TIMEOUT`) to
the private shared-constants module (`job_finder/web/_http_constants.py`)
and threads it through the enrichment/search and careers-scraper call sites
and the desktop scheduler factories.

**Skip:** the commit's *delta* is not ported. The base constants module
itself **is** ported and live — `jobcannon/engine/_http_constants.py`,
imported by `jobcannon/engine/ats_detection.py:82` and
`jobcannon/engine/ats_prober.py:260` — so this entry records a partial
skip of a ported module, not a skipped module. What stays behind is only
`09432fe4`'s additions, because every caller they serve (the
enrichment/search modules, the careers scraper, the desktop scheduler
factories) is itself
deliberately not ported. The values are desktop HTTP tuning — sized for a
residential connection and a single-user process. If those consumers are
ever ported, the timeout values must be re-derived for hosted egress
characteristics (datacenter networking, shared workers, per-request
budgets) rather than copied.

## 2. Local-inference KV-cache threshold

**Private evidence:** commit `e504cd7d`.

**Skip:** the private KV-cache admission threshold (and its parallelism
pinning) tunes a locally-running inference server on the desktop GPU. No
hosted local-inference path exists today, so there is nothing for the
threshold to govern. Whether one is ever admitted is exactly the open
question entry 4 records; if that question resolves yes, this entry must be
revisited alongside it.

## 3. `stale_detector` disk-refresh semantics

**Private evidence:** commit `cb9df5af` — a not-ported part of a partially
ported module.

Cannot-occur-hosted ruling, all four steps:

1. **Private failure mode:** the long-lived desktop process cached
   `opaque_redirect_sources` from disk and served stale values until process
   restart; the private fix re-reads the file on each stale-detection run.
2. **Hosted mechanism making it unreachable:** the hosted stale-detect entry
   point is reserved and raises — stale detection cannot run at all — and
   hosted runtime configuration arrives through an injected provider
   (`jobcannon/engine/runtime_config.py`), not from a config file read off
   disk, so there is no on-disk value to go stale.
3. **Citation:** `jobcannon/host/scan_tasks.py:93`
   (`raise NotImplementedError("stale-detect is reserved; see module docstring")`,
   `def run_stale_detect_task` at `:89`).
4. **What would invalidate this skip:** wiring `run_stale_detect_task` to a
   real implementation (removing that `NotImplementedError`), any hosted
   caller invoking the engine's `run_stale_detection` entry point directly
   (today its only callers are tests), or introducing any disk-file
   configuration read into the hosted stale-detection path. Any of these
   changes reopens the disk-refresh decision and this entry must be
   resolved before that change merges.

## 4. Local-inference environment pinning

**Private evidence:** private-only, no SHA (environment configuration, not a
commit).

**Skip status: ESCALATE [OWNER]** — undecided, recorded rather than guessed.

The private deployment pins environment variables for its locally-running
inference server. Whether the hosted deployment ever runs against a
self-hosted inference endpoint is not yet decided: the engine carries an
inference-tuning config default (`jobcannon/engine/job_scorer.py:95` reads
`providers.ollama.num_ctx`), but no provider implementation or endpoint
wiring exists in this codebase.

**Owner question (yes/no):** will the hosted deployment admit a self-hosted
inference endpoint (an operator-supplied local model server)? If **yes**, the
environment pinning becomes load-bearing and must be ported alongside that
provider. If **no**, this skip is permanent and the entry closes.

## 5. Wizard scoring-leg call timeout

**Private evidence:** commit `bbae3449`.

**Skip:** the private fix threads a per-call timeout through the wizard's
scoring leg. The hosted scoring seam accepts no timeout parameter — scoring
routes through an injected `call_model` callable
(`jobcannon/engine/job_scorer.py:410`, required keyword-only at `:455`) — and
the provider-cascade constraints
([provider-cascade-constraints.md](provider-cascade-constraints.md)) rule
that a single monotonic deadline governs the whole cascade, owned at the
cascade entry point. Porting a per-leg timeout would install exactly the
per-provider multiplication that constraint forbids. When a cascade is
implemented, the wizard's scoring leg inherits the cascade deadline instead.

## 6. Scrape-blocklist enforcement comment on the HTML-fallback scrape call

**Private evidence:** commit `c2793bb9` — its only `ats_scanner` hunk is a
comment block above the HTML-fallback scan's scrape call documenting that the
private `careers_scraper` internally enforces the scrape-host blocklist gate
(`_is_blocklisted_scrape_host`), so callers need no gate of their own.

**Skip:** comment-only, no behavior delta. The hosted engine reaches scraping
through the `ScanServices.scrape_careers_page` hook
(`jobcannon/engine/ats_scanner/_run_html.py` — the module header records that
`careers_scraper` itself does not port), so whatever implementation the host
injects may or may not carry that internal gate. Porting the comment would
assert an enforcement property this repository cannot guarantee about an
injected callable.

**What would invalidate this skip:** porting `careers_scraper` (or shipping
any first-party implementation of `scrape_careers_page`) — at that point the
blocklist gate becomes this repository's property to enforce and document,
and the comment (and the gate itself) must come along.
