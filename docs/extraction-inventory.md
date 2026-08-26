# Engine-extraction inventory (Phase 0 -> Phase 1 contract)

Classification of the private predecessor's modules for the hosted rebuild.
PORT = extract as-is into the engine package. ADAPT = extract with named rework.
HOLD = valuable, not in v1 (stays private until wired). DIES = not ported.
OPEN = surfaced but unclassified; awaiting owner decision.

| Module (private repo) | Verdict | Notes |
|---|---|---|
| `job_finder/ats_platforms/` (27 scanners + registry) | PORT | The core asset. Registry pattern + completeness guard come with it. **Path correction**: actual location is `job_finder/web/ats_platforms/` (35 files, 7,131 LOC), not top-level `job_finder/ats_platforms/` - it lives under the Flask `web` package, not standalone. |
| `job_finder/ats_scanner/` | PORT | Scan orchestration; re-schedule under new scheduler. **Path correction**: actual location is `job_finder/web/ats_scanner/` (7 files, 2,845 LOC), not top-level `job_finder/ats_scanner/`. |
| `job_finder/models.py` (Job + dedup_key) | PORT | Dedup identity is load-bearing. Path verified as-is (117 LOC, top-level). |
| `job_finder/db/` (queries/jobs/persistence/classification) | ADAPT | Raw-SQL layer rewritten for Postgres + multi-tenant (user_id scoping); single-writer postings rule (upsert_posting) carries over. Path verified as-is (18 files, 5,149 LOC). |
| JD extraction chokepoint (`extract_clean_jd` + trafilatura layer) | PORT | Single-chokepoint design is correct as-is. `extract_clean_jd` lives in `job_finder/web/platform_extractor.py`, consumed by `agentic_enricher.py`, `careers_scraper.py`, `enrichment_tiers.py`. |
| `job_finder/web/job_scorer.py` + scoring rubric/prompts | PORT | Feeds BYO-key tier; structural axes also feed ranker labels (spec 4a). Path verified as-is. |
| `job_finder/web/model_provider.py` + `providers/` | ADAPT | Keep cascade; per-user BYO-key credentials instead of owner config; owner-paid providers excluded from free tier. Paths verified as-is (`model_provider.py` 1,119 LOC; `job_finder/web/providers/` 12 files). |
| `job_finder/web/stale_detector.py` + expiry logic | PORT | Powers freshness honesty + lifespan analyses. Path verified as-is. |
| `job_finder/careers_crawler/` | ADAPT | Sitemap/API tiers port; per-user bespoke nav-recipe tier DIES (spec 3 custom-tail cut). Cohort-legitimacy gate comes along. **Path correction**: actual location is `job_finder/web/careers_crawler/` (15 files, 4,610 LOC), not top-level `job_finder/careers_crawler/`. |
| `job_finder/parsers/` (email alert parsers) | HOLD | Spec lists parsers as engine, but v1 has no per-user IMAP; port dormant or defer to a forwarding feature. Do not wire. Path verified as-is (12 files, 3,465 LOC). |
| `job_finder/sources/` (serpapi, dataforseo, portal_search, google_cse) | HOLD | Aggregator sources burn owner API keys per query -> violates cost envelope at multi-user scale; needs per-source cost decision before any port. Path verified as-is (9 files, 2,804 LOC), including `imap_source.py` and `email_senders.py`. |
| `job_finder/web/` Flask app, blueprints, templates | DIES | Rebuilt as the hosted app. Path verified as-is (107 files directly under `web/`, 89,563 LOC counting all subpackages). |
| `job_finder/web/scheduler/` (APScheduler + ledger) | DIES | Rebuilt server-side; shared-corpus scan scheduling is new design. Path verified as-is (9 files). |
| IMAP ingestion (`sources/imap_source.py`, email senders) | DIES | Privacy posture (spec 3). Path verified as-is under `job_finder/sources/`. |
| `job_finder/web/nightly_monitor/` | DIES | Owner-ops apparatus. Path verified as-is (8 files, 921 LOC). |
| Resume pipeline (`resume_drafts`, tailoring, experience bank) | DIES | Owner-personal; PII-heavy (spec 3). `resume_drafts.py`, `resume_tailor.py`, `experience_bank.py` all confirmed under `job_finder/web/`. |
| Process lifecycle (`_takeover`, `_pidfile`, supervisor, tray) | DIES | Single-instance desktop concerns; meaningless hosted. `_takeover.py`, `_pidfile.py`, `supervisor.py` confirmed under `job_finder/web/`; `tray.py` is top-level (`job_finder/tray.py`), not under `web/`. |
| `job_finder/scoring/` (`JobScorer`, legacy pre-v3.0 scorer) | OPEN | 2 files, 200 LOC (verified: `__init__.py` 1 LOC + `scorer.py` 199 LOC). Live import at `job_finder/web/pipeline_runner.py:24` (`from job_finder.scoring.scorer import JobScorer`, verified). Was below the 2026-07-16 appendix's 20-row cutoff, so it never got an independent verdict; it now appears there (see appendix below) at the same count as the row it displaced. **OPEN - owner classification pending (F7 2026-08-26).** |
| `job_finder/db/_company_state.py` | OPEN | New module, no entry in the private `.planning/ported-paths.json` R-8 disposition manifest (one-hop import gap: `job_finder/web/ats_scanner/_run.py` imports it, verified). Part of the WI company-merge/identity-reconciliation epic's scope decision - see public issue #138. **OPEN - owner classification pending (F7 2026-08-26).** |

## Coupling risks Phase 1 must plan around (verified by import inspection)

- Scanners import `Job` from `job_finder.models` and persistence helpers from
  `job_finder.db` - the extraction seam is models+db, not the scanners.
- Scoring imports `config.py` (fail-fast YAML) - hosted config becomes env/DB.
- Coupling grep results (`grep -rn "from job_finder.web" job_finder/ats_platforms/ job_finder/ats_scanner/`):
  The literal paths in that command do not exist in the private repo - both
  directories live at `job_finder/web/ats_platforms/` and
  `job_finder/web/ats_scanner/` (see path corrections in the table above).
  Re-run against the corrected paths, the grep is **not clean** - it returns
  177 hits, contradicting the "extraction seam is models+db, not the
  scanners" claim above. The scanners import directly from sibling `web/`
  modules that are outside the models+db seam, including (non-exhaustive,
  first 20 of 177 shown):

  ```
  job_finder/web/ats_platforms/_detail_fetchers.py:24:from job_finder.web.ats_platforms._http_session import get_session
  job_finder/web/ats_platforms/_detail_fetchers.py:25:from job_finder.web.ats_prober import _PROBE_TIMEOUT
  job_finder/web/ats_platforms/_detail_fetchers.py:26:from job_finder.web.description_formatter import strip_html_to_text
  job_finder/web/ats_platforms/_http_session.py:25:from job_finder.web.ats_platforms._concurrency import HOST_PACING_LIMIT
  job_finder/web/ats_platforms/_platforms_adp.py:37:from job_finder.web.ats_platforms._http_session import get_session
  job_finder/web/ats_platforms/_platforms_adp.py:38:from job_finder.web.ats_platforms._registry import (
  job_finder/web/ats_platforms/_platforms_adp.py:44:from job_finder.web.ats_prober import _PROBE_TIMEOUT
  job_finder/web/ats_platforms/_platforms_adp.py:45:from job_finder.web.location_parser import parse_locations
  job_finder/web/ats_platforms/_platforms_amazon.py:21:from job_finder.web.ats_platforms._http_session import get_session
  job_finder/web/ats_platforms/_platforms_amazon.py:22:from job_finder.web.ats_platforms._registry import (
  job_finder/web/ats_platforms/_platforms_amazon.py:27:from job_finder.web.ats_prober import _PROBE_TIMEOUT
  job_finder/web/ats_platforms/_platforms_amazon.py:28:from job_finder.web.description_formatter import html_to_plain_text
  job_finder/web/ats_platforms/_platforms_amazon.py:29:from job_finder.web.location_parser import parse_locations
  job_finder/web/ats_platforms/_platforms_ashby.py:16:from job_finder.web.ats_platforms._registry import (
  job_finder/web/ats_platforms/_platforms_ashby.py:22:from job_finder.web.ats_platforms._salary import build_salary_fields, period_from_interval
  job_finder/web/ats_platforms/_platforms_ashby.py:23:from job_finder.web.description_formatter import html_to_plain_text
  job_finder/web/ats_platforms/_platforms_ashby.py:24:from job_finder.web.location_canonical import (
  job_finder/web/ats_platforms/_platforms_bamboohr.py:20:from job_finder.web.ats_platforms._http_session import get_session
  job_finder/web/ats_platforms/_platforms_bamboohr.py:21:from job_finder.web.ats_platforms._registry import (
  job_finder/web/ats_platforms/_platforms_bamboohr.py:25:from job_finder.web.ats_prober import _PROBE_TIMEOUT
  ```

  Most hits are intra-package (`ats_platforms` importing its own
  `_http_session`/`_registry`/`_concurrency`/`_salary` submodules, or
  `ats_scanner` importing its own `_probe`/`_promote`/`_run*`/`_upsert`
  submodules) - not a Phase 1 concern, since those submodules move together.
  The real cross-cutting dependencies (distinct target modules outside
  `ats_platforms`/`ats_scanner` themselves) are: `ats_prober`,
  `description_formatter`, `location_parser`, `location_canonical`,
  `ats_registry`, `ats_detection`, `ats_company`, `ats_identity_reconcile`,
  `ats_slug_challenge`, `brand_blocklist`, `dedup_normalizer`,
  `_field_alias`, `db_helpers` (Flask per-request `g.db`, distinct from the
  `job_finder.db` package), `pipeline_runner`, `scoring_orchestrator`,
  `data_enricher`, `careers_scraper`, `homepage_discoverer`,
  `autoheal.health_monitor`, and `careers_crawler._title_filters`. These
  need to be re-homed into the models+db seam (or a shared engine-utils
  module) before the scanners can be extracted cleanly - Phase 1 should
  not assume the scanners are already decoupled.

## Verified listing appendix

(Per-directory totals, generated 2026-08-26:)

```
job_finder total: 138,317 LOC across all *.py files (excl. __pycache__)

Per-directory file counts (top 20 by file count):
    181 job_finder/web/migrations
    114 job_finder/web
     35 job_finder/web/ats_platforms
     25 job_finder/db
     17 job_finder
     16 job_finder/web/careers_crawler
     15 job_finder/web/blueprints
     12 job_finder/parsers
     12 job_finder/web/autoheal
     12 job_finder/web/nightly_monitor
     12 job_finder/web/providers
     11 job_finder/web/scheduler
      9 job_finder/sources
      8 job_finder/web/onboarding
      7 job_finder/web/ats_scanner
      7 job_finder/web/pipeline_detector
      5 job_finder/eval
      5 job_finder/scripts
      3 job_finder/web/scoring_prompts/variants
      2 job_finder/scoring

Per-directory LOC totals (dirs named in the classification table above;
job_finder/web is recursive - it includes ats_platforms, ats_scanner,
careers_crawler, nightly_monitor, blueprints, migrations, etc. as
subdirectories, so its total is not additive with the others below it):
  job_finder/web                     114,228 LOC
  job_finder/web/ats_platforms         7,123 LOC
  job_finder/db                        8,059 LOC
  job_finder/web/careers_crawler       5,469 LOC
  job_finder/parsers                   3,465 LOC
  job_finder/web/ats_scanner           3,533 LOC
  job_finder/sources                   3,201 LOC
  job_finder/web/nightly_monitor       5,526 LOC
  job_finder/models.py                   117 LOC (single file)

Top 20 largest individual modules (job_finder/*.py, excl. __pycache__):
   2309 job_finder/web/nightly_monitor/_morning.py
   2255 job_finder/web/onboarding/blueprint.py
   2153 job_finder/web/scheduler/_runners.py
   2147 job_finder/web/blueprints/jobs.py
   2088 job_finder/web/ai_career_navigator.py
   2081 job_finder/web/scheduler/_jobs.py
   2053 job_finder/web/ats_scanner/_run.py
   1898 job_finder/web/ats_prober.py
   1890 job_finder/web/company_dedup.py
   1862 job_finder/web/supervisor.py
   1612 job_finder/web/data_enricher.py
   1592 job_finder/config.py
   1535 job_finder/web/homepage_discoverer.py
   1410 job_finder/web/model_provider.py
   1402 job_finder/web/ingestion_runner.py
   1352 job_finder/sources/portal_search_source.py
   1346 job_finder/__main__.py
   1292 job_finder/web/claude_client.py
   1275 job_finder/db/_jobs.py
   1175 job_finder/web/enrichment_tiers.py
```
