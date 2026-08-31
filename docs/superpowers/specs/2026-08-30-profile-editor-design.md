# Profile Editor (Spec 2) — Design

> Ratified 2026-08-30 in the post-Spec-1 session. Answers the original feedback
> item "how is the user supposed to see their profile?" and resolves #262.
> Scope inherited verbatim from Spec 1's Decisions §2: "Profile surface: full
> edit form — deferred to Spec 2, including new write paths for
> `experience_summary` and `target_locations` (no writer exists today) and
> per-user pipeline stats."

## Decisions (owner-ratified)

1. **Editor-first page shape.** `/profile` IS the edit form; pipeline stats are
   a compact summary strip on it, not a dashboard in front of it.
2. **#262 resolution: redirect authed visitors off `/start`.** `/start` stays a
   purely anonymous surface; the clerk profile domain gains exactly one writer
   (the editor). The alternative — branching `POST /start` by identity into two
   write domains — was rejected as two write paths where one suffices.
3. **Scoring staleness: natural refresh.** No re-score trigger, no staleness
   UI. Overlap chips read the profile at render time (next page load reflects
   edits); batch scoring picks up `experience_summary` / `target_locations` on
   its next run. Documented behavior, zero new machinery.
4. **Stats strip: three counts.** Saved / Applied / Dismissed, each linking to
   the matching postings-history filter view. No recency metadata (YAGNI).
5. **Write path: full-snapshot form (Approach A).** Complete-snapshot submit
   through the existing `upsert_profile`; sectioned htmx partial saves
   (Approach B) rejected — they fight the `workplace_type` required-kwarg
   contract and the empty-vs-omitted COALESCE convention, and multiply write
   paths into a deliberately single-writer table.

## Load-bearing facts (verified against main @ 57525d9)

- `profiles` (m0001 + m0008 + m0012) already has every needed column. There is
  **no schema migration in this spec.**
- `experience_summary` and `target_locations` are NULL for every real user:
  `upsert_profile` accepts both (`db/_profiles.py:110,112`) but the only
  callers passing them are the guest-demo seed script and `handoff.py`'s
  domain-promotion copy (which only copies the anon row's NULLs). They DO have
  live readers: `host/candidate_context.py:128-129` feeds both into the
  scoring prompt, whose `location_fit` scores against `target_locations`.
  **This editor is therefore the first real writer for scoring-relevant
  fields — populating them intentionally changes scoring behavior.**
- `upsert_profile` is per-column `COALESCE(EXCLUDED.col, profiles.col)`
  (`_profiles.py:125-137`): omitted/None preserves, empty list clears.
  `workplace_type` is a plain overwrite with a **required** keyword-only arg
  (NULL is its legitimate "no preference" value). The picker "submits
  literally": a complete snapshot every time, `_profile_prefill` making
  revisit-and-resubmit safe. The editor mirrors this exactly.
- Domain model (#262): both profile domains are rows in the same `profiles`
  table — `anon_<uuid4hex>` ids minted by `POST /start` vs raw Clerk subjects.
  Promotion (`web/handoff.py:117-175`) is destructive copy-then-cascade-delete
  on first authed request; the domains never coexist after signup.
- `g.clerk_user.user_id` IS `profiles.user_id` on authed routes — direct key,
  no lookup (`postings_history.py:193`, `consent.py:143`).
- Pipeline state: saves live in `watchlists`; `pipeline_status` holds exactly
  `{"dismissed", "applied"}` (`db/_user_actions.py:47`) with row-absence as the
  neutral state. **No COUNT/aggregate query exists anywhere yet.**
- `get_profile` uses an explicit column list (#105) and the account export
  pins its exported key set — no new columns means **no export change**.

## 1. Routes & auth

- `GET /profile` + `POST /profile` in a new `jobcannon/web/profile.py`
  blueprint. NOT in `PUBLIC_PATHS`, so the `before_request` gate guarantees
  `g.clerk_user`; the row key is `g.clerk_user.user_id` directly.
- `/start` #262 fix, in `web/onboarding.py`: when `_current_identity()`
  resolves a clerk identity, both `GET /start` and `POST /start` return
  `redirect(url_for("profile.edit"), code=303)`. `_current_identity()` is the
  same fail-open seam `_profile_prefill` already uses — a visitor whose
  verification throws is treated as anonymous and gets today's exact behavior.
  The anonymous flow is byte-identical; the anon/pending domain keeps exactly
  its current writers.

## 2. The form

`GET /profile` renders every editable column prefilled from
`get_profile(conn, user_id)`:

| Field | Input | Notes |
|---|---|---|
| `target_titles` | list input | picker-convention validators reused/mirrored |
| `target_companies` | list input | same |
| `workplace_type` | select | explicit "no preference" option → NULL; always submitted (required-kwarg contract) |
| `comp_floor_usd` | number | nonneg (schema CHECK m0008 mirrored in validation) |
| `seniority_level` | existing vocabulary | as `/start` collects it |
| `years_of_experience` | number | numeric, bounded |
| `skills` | list input | picker conventions |
| `experience_summary` | textarea | **new validator**: length cap, control-char rejection (newlines allowed) |
| `target_locations` | list input | **new validator**: count cap + per-item length cap + control-char rejection, mirroring `_parse_titles`' shape |

("List input" throughout = a plain text control, one entry per line, parsed
server-side into the same validated list shapes the picker produces. The
`/start` search-picker widget is deliberately NOT replicated on `/profile`.)

- `POST /profile` submits the **complete snapshot** through the existing
  `upsert_profile` — no DAL write changes. Empty list = deliberate clear
  (reaches COALESCE as non-NULL `Jsonb([])`, the #169 semantics).
- Validation errors: re-render **200** (never 4xx/redirect) with every
  submitted value echoed back — the `start_submit` pattern.
- Success: PRG — `redirect` 303 back to `GET /profile` with a rendered
  "Profile saved" confirmation note (the `/consent` pattern).
- Plain form POST; no htmx in v1 (the picker's own submit is a plain form).
- **No-row edge case**: an authed user who signed up without onboarding has no
  `profiles` row. `get_profile` returns None → form renders empty; first POST
  creates the row via the upsert's INSERT arm. No special-casing beyond
  "prefill from None renders blanks."

## 3. Stats strip

- New COUNT primitives in `db/_user_actions.py` (the module already owns all
  reads/writes for both tables): a watchlist count and a per-status
  `pipeline_status` count for one user. Single-query SELECT COUNTs.
- Rendered as a compact three-cell strip at the top of `/profile`:
  Saved / Applied / Dismissed. Each cell links to the postings-history view
  filtered to that status if such a URL exists (the plan verifies
  `postings_history.py`'s actual filter parameters); otherwise all three link
  to the plain history view — never invent a filter URL.
- Counts are read in the `GET /profile` view alongside the profile row; a
  count of zero renders "0", never hides the cell.

## 4. Nav

- Authed-only "Profile" link in `base.html`, gated by the existing
  `visitor_is_authed` template global, alongside "My postings", with the
  standard touch-target class. No anon-facing change.

## 5. Scoring propagation (documented behavior, nothing built)

- Overlap chips: profile is read at feed render time → edits visible on the
  next page load, automatically.
- Batch scoring: `candidate_context` consumes `experience_summary` /
  `target_locations` at scoring time → new values take effect on the next
  scoring run. No trigger, no staleness indicator.
- The spec records explicitly: shipping this editor is the moment those two
  columns stop being always-NULL, which changes scoring inputs for every
  subsequently scored posting. That is the point, not a side effect.

## 6. Non-changes (guard rails for the implementation plan)

- No schema migration; no new columns.
- No account-export change; the pinned export key set stays valid.
- `db/_profiles.py` contracts untouched (`upsert_profile` signature,
  `clear_profile_targets`, `get_profile` column list).
- `/start`'s anonymous flow byte-identical; `handoff.py` promotion untouched.
- No htmx additions; no new green elements (Living Journal rules bind: any new
  `jc-*` classes land in `jc.css` with the closure tests, themes via
  `prefers-color-scheme` only).

## 7. Testing

- Route tests on the local-`_app` + monkeypatched-module-attribute pattern
  (`test_start_prefill.py` / `test_pages.py` style): prefill renders stored
  values; None-profile renders empty form; full-snapshot submit persists;
  empty-list clears; validation errors echo all fields at 200; PRG redirect
  on success; unauthed request 401s.
- Authed `/start` redirect: GET and POST both 303 to `/profile`; anonymous
  GET/POST behavior pinned unchanged; fail-open (verifier throws) pinned to
  anonymous behavior.
- Validator units for the two new parsers (caps, control chars, empty vs
  omitted).
- `requires_postgres` tests for the COUNT primitives (seed → count → act →
  recount; absence-is-neutral pinned).
- Nav assertions in `test_auth_nav.py` (authed sees Profile, anon doesn't);
  design-template closure tests cover any new classes.
