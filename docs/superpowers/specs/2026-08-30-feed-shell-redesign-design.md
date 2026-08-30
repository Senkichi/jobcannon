# Feed & shell redesign — design

Date: 2026-08-30
Status: design sections approved by owner in discussion; this written spec
is pending owner review
Scope: Spec 1 of 2. Spec 2 (the profile editor) is designed separately after
this ships its plan; its packaging decision is recorded below.

## Context

Owner feedback on the Living Journal UI (adopted in #257), verbatim themes:
wasted space on the home page; job cards uninformative (salary missing from
view); green text blurs together and highlights unworthy info; whole card
should be clickable and expand to detail; redundant REMOTE classification;
a mystery "default" filter; "Why this rank?" refers to an invisible ranking;
the demo page's purpose unclear; the wordmark should link home; no page icon;
Feed/Demo nav duplication; no way to see one's profile.

Four parallel investigations (card content, navigation/IA, design-system
usage, backend data inventory) established the facts cited throughout. Key
ones:

- No ranker writes `feed_state.rank_score` anywhere
  (`tests/host/test_feed_state_not_written.py` guards this); every feed is
  recency-sorted and reports `unranked-v0`. Real scoring is gated on a
  `jd_adjudicated_version` writer that does not exist
  (`tests/test_scoring_precheck_wiring_guard.py`).
- `jc.css` defines unused recipes (`jc-meta-num`, `jc-meta-lab`, `jc-index`,
  `jc-stamp--green`) built for denser, more informative rows.
- Spacing in `jc.css` is hand-authored — zero token-pipeline lock. The 48rem
  page width is a deliberate spec decision and stays.
- `postings.jd_full`, `description`, `comp_data_json`,
  `locations_structured`, `source_urls`/`sightings`, and all four structural
  axes are stored but unreachable from the UI; `list_feed_postings`'
  projection (`jobcannon/db/_feed.py:69-74`) deliberately excludes the heavy
  columns.
- `/start` has no DB prefill (`onboarding.py` never imports `get_profile`);
  a blank resubmit silently wipes saved picks. After Clear, `has_selections`
  stays true, so the picker link never reappears — a profile-access dead end.

## Decisions (owner-ratified)

1. **Demo page**: signed-out showcase only. Nav link hidden for authed
   visitors; the route stays publicly reachable by URL.
2. **Profile surface**: full edit form — deferred to Spec 2, including new
   write paths for `experience_summary` and `target_locations` (no writer
   exists today) and per-user pipeline stats.
3. **Packaging**: two specs. This one covers the feed-page overhaul,
   navigation/identity/favicon, the expandable card detail view, and the
   `/start` prefill safety patch.

Everything below is within the existing Living Journal identity rules
(`docs/design/living-journal.md`) except the favicon, which needs a rule-5
amendment (§4).

## 1. Card redesign

Three emphasis tiers replace the flat card in `_posting_row.html`:

- **Primary — title + salary.** Salary renders via the existing `jc-meta-num`
  recipe (20px bold serif, tabular numerals, ink; `jc.css:198-212`, currently
  referenced by zero templates). A new Python template filter (living beside
  `why.py`/`apply_url.py`) formats compactly: `$150k–200k/yr`;
  `from $150k` / `up to $200k` for one-sided ranges; period mapped
  annual→`/yr`, hourly→`/hr`, monthly→`/mo`, `unknown`→omitted; currency
  renders `$` for `USD`, the bare ISO code as prefix for any other known
  currency (`EUR 80k–100k/yr` — no symbol table to hand-maintain), and is
  omitted entirely for `UNKNOWN`. Sentinel cases are case-sensitive by schema:
  currency uses uppercase `UNKNOWN`, period lowercase `unknown`
  (`m0001_initial_schema.py:64-67`). No salary data → no salary line.
- **Secondary — company · location.** A dedup helper in
  `jobcannon/web/feed_entries.py` (beside `build_entry`; pure and testable):
  suppress `location` when it only restates `workplace_type` (e.g. location
  token-matches "remote" while `workplace_type == "REMOTE"`); render the
  workplace-type badge only when location does not already communicate it.
  The badge demotes from a `jc-note` beside the title to the 10px small-caps
  `jc-meta-lab` treatment.
- **Tertiary — chips**, capped at 3, priority: title-overlap > freshness >
  seniority > JD-quality (ties impossible; order total). The `_salary_chip` ("salary listed") is deleted —
  redundant once the number is prominent. Selection/cap logic lives in
  `feed_entries.py`, NOT `why.py` (which stays the pure restatement source
  per its own docstring). The "signals still computing" marker stays, still
  keyed on NULL `structural_axes`.

Density (all hand-authored `jc.css` values, no token pipeline touch):
`.jc-row` padding 16→12px; intra-card `.jc-stack` gap 12→8px; `.jc-page`
top padding 64→40px; the h1 + accent rule + ordering label group into one
masthead wrapper (new `jc-*` composition class) with tight internal rhythm.
The 48rem column width is retained deliberately — the fix for "wasted space"
is vertical density, not a wider journal page.

## 2. Green discipline & honest labeling

- `.jc-chip` becomes ink/gray by default. New modifier `jc-chip--why`
  carries `--lj-green-text`, applied to **at most one chip per row** — the
  top-priority signal (overlap if present, else freshness). Green on a feed
  page drops from up to ~125 chip elements to ≤1 per row plus the masthead
  accent rule. Identity rule 1 caps where green may appear; it never
  mandated green chips, so this is within-rules. The closure test
  (`tests/test_design_templates.py`) self-updates from `jc.css`.
- Chip header **"Why this rank" → "Highlights"**, unconditionally. The chips
  are literal restatements (`why.py` docstring), and no personalized ranking
  exists. When real ranking ships (gated on the adjudicator writer, see
  Context), rank language may return tied to `ordering.personalized`.
  Numeric rank/score display is explicitly out of scope until that lands.
- Riding fix: `.jc-index` (`jc.css:180-186`) is 15px/600 on the 3:1
  `--lj-gray` tier — below the large-text threshold; switch to
  `--lj-gray-text` (rule-4 self-consistency).
- The verified-fresh `jc-stamp--green` stamp remains unused for now —
  deliberately out of scope to keep green ≤1 per row.

## 3. Expandable card

- **Mechanism**: whole card clickable, expanding in place via htmx —
  `hx-get` to a new `GET /postings/<id>/detail` fragment route, swapped
  into a persistent empty slot INSIDE the row (`<div data-posting-detail>`
  within `[data-posting-row]`), never replacing the row itself. The card's
  existing DOM — Save/Dismiss/Apply controls, saved/applied state, chips —
  is untouched by expansion, which is what lets the detail route stay
  genuinely stateless (a row-`outerHTML` swap from a stateless route would
  strip the authed card's user state). Collapse is local removal of the
  panel (no second fetch); the expand button toggles. htmx beats
  `<details>` because the content worth expanding for (`jd_full`,
  `comp_data_json`, provenance) is deliberately excluded from the list
  projection; the detail route does its own `SELECT * FROM postings WHERE
  id = %s`, mirroring `db/_jd_full.py`'s established single-posting read
  pattern. Accepted interaction: a Save/Dismiss/Undo action re-renders the
  row via `_posting_row.html`, which carries an empty detail slot — acting
  on an expanded row therefore collapses it.
- **Click-target guards**: a delegate on the card root ignores clicks
  landing on `[data-posting-actions]`, `[data-action-apply]`, links,
  buttons, and forms, and checks `window.getSelection().toString()` is
  empty (drag-select must not toggle). Apply's plain-`fetch()` mechanism
  (documented in `_posting_row.html`) is untouched. Keyboard/AT access: a
  real expand `<button>` in the card is the primary control; the whole-card
  click is an enhancement layered on it.
- **Detail contents** (all zero-pipeline-work): `jd_full`, falling back to
  `description`, falling back to an honest "full description not yet
  available" note; comp context from `comp_data_json` via the existing
  `engine/scoring_types.py::build_comp_context`; `locations_structured` as a
  real multi-location list; "seen on" provenance from `sightings`
  (source, first/last seen); posted / last-seen dates using the same
  `posted_date_precision` anchor logic as the freshness chips (never claim
  an origination date we don't have); the four structural axes plus
  `structural_scored_at` ("signals as of…"). NULL renders as
  "Not specified" — never a false negative ("Not remote"). `direct_url` is
  a decoy column (unconditionally NULL) and must not be read.
- **Route exposure**: serves posting content only, no user state — public
  like `/demo`, no event logging. Note the exposure delta this ratifies:
  `/demo` shows postings only through the canned profile's filter, while
  `GET /postings/<id>/detail` makes any posting's full `jd_full` fetchable
  by id enumeration with no auth. Acceptable because the content is
  scraped-public in origin, but it is a wider window than any existing
  public surface. Cards expand on all three surfaces (feed, demo,
  preview). Reveal is a plain swap — no transform animation
  (identity rule 3).

## 4. Navigation & identity

- **Identity-aware wordmark link**: `Job Cannon` → `/` when
  `visitor_is_authed`, `/demo` when not (respecting the
  `visitor_is_authed`-not-`g.clerk_user` PUBLIC_PATHS subtlety documented in
  `base.html`). Sequencing: the wordmark becomes a link BEFORE any nav
  trimming — it becomes the only route back to the feed from `/postings`.
- **Final nav** — authed: wordmark + "My postings" ("Feed" and "Demo" links
  removed). Signed out: wordmark (→/demo) + "Build your feed" (→/start) +
  the existing Sign in / Sign up cluster (the separate "Demo" link is
  redundant once the wordmark covers it).
- **Sort select removed** while `sort_tokens|length == 1` — the masthead's
  "Sorted by recency" line already answers it, and a second sort token
  requires keyset-pagination work first (`_feed.py:410-415` hard-rejects
  non-default tokens with a cursor). When a second sort ships, options get
  human labels with the wire value preserved. The workplace-type select
  stays (real choices).
- **Favicon**: `favicon.svg` — simplified cannon glyph (barrel + firing
  arc; the cannon-firing-stick-figure concept does not survive 16×16),
  with an embedded `prefers-color-scheme` style block for dark mode,
  referenced via `url_for('static', …)` in `base.html` (never trips the
  third-party-host test). The full stick-figure illustration is reserved
  for a 180px touch icon. **Rule-5 amendment required**: SVG icon assets
  cannot resolve CSS custom properties, so `living-journal.md` rule 5 gains
  a documented icon-asset exemption sanctioning the ink/paper hex literals
  (parallel to the existing `legal_page.html` inline-style exception). No
  test changes — the design tests scan only `.html`/CSS.

## 5. Safety patch: /start prefill

`GET /start` prefills from the stored profile row via `get_profile` (today
it prefills only from session/query-string echo). This defuses the live
footgun where a revisit + unchecked resubmit silently wipes saved picks
(`_profiles.py` submits literally, by design), and makes the new
"Build your feed" link safe. The full profile editor is Spec 2.

## 6. Error handling & testing

- Detail route: 200 fragment on success, 404 for unknown ids, field-by-field
  NULL degradation as above.
- Unit tests: salary formatter (sentinel cases both spellings), dedup
  helper, chip cap/priority, detail-fragment NULL handling, nav gating by
  auth state, /start prefill.
- Design tests unaffected: new classes land in `jc.css` (closure
  self-updates); green usage only shrinks; token pipeline untouched.
- Implementation-phase test writing is delegated to subagents per the
  standing workflow rules.

## Out of scope

- Profile page / editor (Spec 2).
- Numeric rank or score display; any `feed_state` writer.
- Second sort option and its pagination work.
- Verified-fresh green stamp.
- Page-width changes.
- Upstream location normalization (stripping workplace tokens from scraped
  `location` at ingest) — flagged for a future pipeline pass; the UI dedup
  helper covers the display problem meanwhile.
