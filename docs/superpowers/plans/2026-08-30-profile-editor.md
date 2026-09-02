# Profile Editor (Spec 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Implement Spec 2 (`docs/superpowers/specs/2026-08-30-profile-editor-design.md`): an authed `/profile` edit form over every editable `profiles` column (the first real writer for `experience_summary` / `target_locations`), a three-count pipeline stats strip, an authed-only "Profile" nav link, and the #262 fix that redirects signed-in visitors off `/start`.

**Architecture:** One wave of five file-disjoint parallel tasks lands the primitives (a plain-overwrite `replace_profile` DAL writer, two COUNT primitives, a pure form parse/format module, the `/start` redirect, the nav link); a single Wave-2 task composes them into the `profile` blueprint + template; one gate agent runs the full suite. All on one shared checkout/branch. The only human gate is the final PR review.

**Tech Stack:** Flask + Jinja2, Flask-WTF `CSRFProtect` (app-wide), psycopg3/Postgres (`Jsonb` adapter), pytest, ruff, hand-authored `jc.css` over generated `lj-tokens.css`.

## Deviations from the approved spec (owner: review these first)

1. **A new `replace_profile()` DAL writer instead of routing the editor through `upsert_profile`.** The spec (§2, §6) says "no DAL write changes … complete snapshot through the existing `upsert_profile`". That contract cannot express the editor's semantics: `upsert_profile` is `COALESCE(EXCLUDED.col, profiles.col)` per column, so a **scalar** field the visitor blanks out (`years_of_experience`, `comp_floor_usd`, `seniority_level`, `experience_summary`) arrives as `None` and is *preserved*, not cleared — the visitor sees their edit silently revert on the next GET. Lists can be cleared (`Jsonb([])`), scalars cannot. `replace_profile` is a plain-overwrite `INSERT … ON CONFLICT DO UPDATE SET col = EXCLUDED.col` for every column, every kwarg keyword-only with **no defaults** (the `workplace_type` required-kwarg contract generalized), so a complete snapshot is the only thing it can accept. `upsert_profile`, `clear_profile_targets`, and `get_profile` are byte-untouched; the module docstring's "exactly one of the two functions" becomes three. The `profiles` table stays single-writer-by-module.
2. **`_profile_prefill` and its `GET /start` call site are removed.** Under decision 2 (authed visitors 303 off `/start`), the prefill is unreachable in production: it only ever returns non-empty for a clerk identity, and every such request now redirects before reaching it. Dead code with eight tests pinning it would be a standing lie about what `/start` does. `_WORKPLACE_DB_TO_FORM` stays (the editor's DB→form mapping reuses it). The prefill tests in `tests/host/test_start_prefill.py` are deleted; the one unrelated test there (`test_preview_entries_come_from_build_entry`) is kept.

Everything else follows the spec verbatim. No spec edits (Spec 1 precedent: deviations are listed here, not back-ported).

## Global Constraints

Every task's requirements implicitly include all of these.

- **Living Journal identity rules are BINDING** (`docs/design/living-journal.md`): no new green elements; no color literals in templates; `lj-*` classes are a closed vocabulary — never invent one; every `jc-*` class used in a template must already be defined in `jc.css` (this plan adds NO new classes — reuse `jc-title`, `jc-lede`, `jc-error-note`, `jc-stamp`/`jc-stamp-dot`, `jc-stack`, `jc-cluster`, `jc-field`, `lj-label`, `jc-input`, `jc-btn jc-btn--primary`, `jc-meta`/`jc-meta-num`/`jc-meta-lab`, `jc-link`, `jc-note`); themes via `prefers-color-scheme` only.
- **Never edit `jobcannon/web/static/lj-tokens.css`, `fonts.css`, or `jc.css`** — the first two are generated and drift-guarded; the third is out of scope for this spec.
- **Every interactive tag in a template carries `{{ touch_target() }}`** (`tests/host/test_touch_targets.py` scans every template: `<a>`, `<button>`, `<input>`, `<select>`, `<textarea>`, `<label>` wrapping a control). Checkbox inputs use `{{ touch_target('checkbox') }}` exactly as `onboarding_picker.html` does.
- **Test invocation, exactly:** `uv run --no-sync --active pytest -q --tb=short <paths>` (bare `pytest` gets hijacked by Windows AppInstaller stubs; `--no-sync` keeps parallel agents from fighting over the venv). `tests/host` DB-backed tests skip without `POSTGRES_ADMIN_DSN`; pure tests there run regardless. `tests/host/conftest.py` creates unique-per-run throwaway DBs, so parallel agents running pytest cannot collide.
- **Lint:** `uv run --no-sync ruff check .` and `uv run --no-sync ruff format --check .`; line length 100.
- **Commits:** Conventional Commits (`feat:`/`fix:`/`docs:`/`test:`/`refactor:`/`chore:`), subject ≤72 chars — `hooks/validate-commit.sh` rejects otherwise. No attribution footers. **Pathspec-limited, always:** `git add <own paths>` then `git commit -m "..." -- <own paths>` so a parallel sibling's staged files never ride along. On `index.lock` contention retry up to 5× with a 2s sleep. Never `git stash`. Never push (the orchestrator handles push/PR after the owner gate).
- **Branch:** all work happens on `feat/profile-editor` in `C:/Users/senki/repos/jobcannon` (cut from the `docs/profile-editor-spec` tip — main does not carry the spec or this plan). Verify `git rev-parse --abbrev-ref HEAD` before every commit; if wrong, stop and report — never create branches or worktrees.
- **File ownership:** each task edits ONLY its listed files (see the map below). A failure in another task's files goes in your report for the gate agent — do not fix it inline.
- **Mid-wave green rule:** sibling tasks are half-landed while you work, so "green" for a wave task means *your own test files* plus `tests/test_design_templates.py tests/host/test_touch_targets.py` pass. The full suite is the gate's job.
- **Retry idempotency:** if an edit's target text is missing, check whether your change is already applied (you may be a stall-retry of yourself) before treating it as an error.
- **Repo guard tests are part of every gate run** (Track A lesson, PR #268): `tests/test_ported_paths_manifest.py` fails if `ported-paths.json` is stale after editing a ported file. None of this plan's files are ported paths, but the gate runs the FULL suite, never a subset, precisely so this class of guard can't be skipped.
- **DAL contracts untouched:** `upsert_profile` signature, `clear_profile_targets`, `get_profile` column list (#105 export pin — no new columns, no export change). No schema migration.
- **`/start` anonymous flow byte-identical**; `handoff.py` promotion untouched; no htmx additions.
- **Form contract (the `start_submit` shape):** validation errors re-render **200** echoing every submitted value — never 4xx, never redirect. Success is PRG **303**. `CSRFProtect` is app-wide: the form includes `<input type="hidden" name="csrf_token" value="{{ csrf_token() }}">`; under `TESTING` CSRF is off unless the test app sets `WTF_CSRF_ENABLED: True`.
- **Identity on `/profile`:** the route is NOT in `PUBLIC_PATHS`, so `before_request` 401s anonymous requests and `g.clerk_user.user_id` IS `profiles.user_id` — direct key, no lookup.
- **No hardcoded lists** where the data can derive from stored state: the stats strip iterates `postings_history._VIEWS`; pipeline count keys come from `_user_actions._PIPELINE_STATUSES`; skills options come from `onboarding.SKILLS_OPTIONS`.

## File ownership map

| Task | Wave | Creates | Modifies |
|---|---|---|---|
| 1 | 1 | `tests/host/test_replace_profile.py` | `jobcannon/db/_profiles.py` |
| 2 | 1 | `tests/host/test_user_action_counts.py` | `jobcannon/db/_user_actions.py` |
| 3 | 1 | `jobcannon/web/profile_form.py`, `tests/host/test_profile_form.py` | — |
| 5 | 1 | `tests/host/test_start_authed_redirect.py` | `jobcannon/web/onboarding.py`, `tests/host/test_start_prefill.py`, `tests/host/test_csrf.py` (ONLY `test_post_start_with_token_mints_anon_user`) |
| 6 | 1 | — | `jobcannon/web/templates/base.html`, `tests/host/test_auth_nav.py` |
| 4 | 2 | `jobcannon/web/profile.py`, `jobcannon/web/templates/profile.html`, `tests/host/test_profile_route.py` | `jobcannon/web/__init__.py`, `tests/host/test_csrf.py` (ONLY appending two new `/profile` tests) |
| 7 | gate | — | full-suite fallout only (see Task 7) |

Within Wave 1, no file appears in two tasks. `tests/host/test_csrf.py` appears in Task 5 (Wave 1) and Task 4 (Wave 2) — sequential, never concurrent, and the two edits touch disjoint regions.

---

### Task 0: Pre-warm (orchestrator, before any agent dispatch)

Never put this inside a retried agent prompt (stall-retries would replay it). Run once from the main session:

- [x] **Step 1: Cut the feature branch from the spec branch tip** (the plan + spec must exist in the shared checkout):

```powershell
git -C C:\Users\senki\repos\jobcannon rev-parse --abbrev-ref HEAD   # expect docs/profile-editor-spec
git -C C:\Users\senki\repos\jobcannon checkout -b feat/profile-editor
```

- [x] **Step 2: Record the baseline.** The post-Track-A baseline against main `9567515` is **3344 passed / 14 skipped / 0 failed** (verified 2026-08-31, `POSTGRES_ADMIN_DSN` set). Re-record it on this checkout so the gate compares like-with-like:

```powershell
$env:POSTGRES_ADMIN_DSN -ne $null   # record the boolean
uv run --no-sync --active pytest -q --tb=short 2>&1 | Tee-Object baseline-pytest.log | Select-Object -Last 5
uv run --no-sync ruff check .
```

Expected: `3344 passed, 14 skipped`, ruff clean. `baseline-pytest.log` stays untracked (do not commit it).

---

## Wave 1 — five parallel tasks (1, 2, 3, 5, 6)

### Task 1: `replace_profile` — plain-overwrite snapshot writer (Deviation 1)

**Files:**
- Modify: `jobcannon/db/_profiles.py` (append after `upsert_profile`, before `clear_profile_targets`; update the module docstring's first paragraph)
- Create: `tests/host/test_replace_profile.py`

**Interfaces:**
- Consumes: nothing new (`Jsonb`, `commit_unless_nested` already imported in the module).
- Produces: `replace_profile(conn, user_id: str, *, skills: list, experience_summary: str | None, target_titles: list, target_locations: list, seniority_level: str | None, years_of_experience: float | None, comp_floor_usd: int | None, target_companies: list, workplace_type: str | None) -> None`. Every kwarg is keyword-only with NO default — omitting any one is a `TypeError` at the call site. Lists are always written as `Jsonb(list)` (an empty list is a stored `[]`, never NULL); scalars are written literally (None → NULL). Task 4's `submit()` is the only production caller; Task 3's `parse_profile_form` returns a dict whose keys are exactly these nine kwarg names.

- [x] **Step 1: Write the failing tests**

Create `tests/host/test_replace_profile.py`:

```python
"""replace_profile (jobcannon/db/_profiles.py) — the profile editor's
plain-overwrite snapshot writer (Spec 2, plan Deviation 1). Rollback-isolated
`db_conn` fixture, same shape as tests/host/test_profiles_dal.py."""

from __future__ import annotations

from decimal import Decimal

import psycopg.errors
import pytest


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def _full_snapshot(**overrides):
    snapshot = {
        "skills": ["python", "sql"],
        "experience_summary": "Twelve years of backend work.",
        "target_titles": ["Staff Engineer"],
        "target_locations": ["Seattle, WA", "Remote"],
        "seniority_level": "staff",
        "years_of_experience": 12.5,
        "comp_floor_usd": 180000,
        "target_companies": ["Acme"],
        "workplace_type": "REMOTE",
    }
    snapshot.update(overrides)
    return snapshot


def test_replace_profile_roundtrips_every_column(db_conn):
    from jobcannon.db._profiles import get_profile, replace_profile

    _seed_user(db_conn, "rp-roundtrip")
    replace_profile(db_conn, "rp-roundtrip", **_full_snapshot())

    row = get_profile(db_conn, "rp-roundtrip")
    assert row["skills"] == ["python", "sql"]
    assert row["experience_summary"] == "Twelve years of backend work."
    assert row["target_titles"] == ["Staff Engineer"]
    assert row["target_locations"] == ["Seattle, WA", "Remote"]
    assert row["seniority_level"] == "staff"
    assert row["years_of_experience"] == Decimal("12.5")
    assert row["comp_floor_usd"] == 180000
    assert row["target_companies"] == ["Acme"]
    assert row["workplace_type"] == "REMOTE"
    assert row["updated_at"] is not None


def test_replace_profile_clears_scalars_and_lists_upsert_would_preserve(db_conn):
    """The reason this function exists: upsert_profile's COALESCE keeps a
    previously-stored scalar when the caller passes None. A complete-snapshot
    editor must be able to blank every field, so replace_profile writes
    NULL for None and [] for an empty list, overwriting whatever was there."""
    from jobcannon.db._profiles import get_profile, replace_profile, upsert_profile

    _seed_user(db_conn, "rp-clear")
    upsert_profile(
        db_conn,
        "rp-clear",
        skills=["python"],
        experience_summary="old summary",
        target_titles=["Old Title"],
        target_locations=["Old Town"],
        seniority_level="senior",
        years_of_experience=9,
        comp_floor_usd=150000,
        target_companies=["OldCo"],
        workplace_type="HYBRID",
    )

    replace_profile(
        db_conn,
        "rp-clear",
        skills=[],
        experience_summary=None,
        target_titles=[],
        target_locations=[],
        seniority_level=None,
        years_of_experience=None,
        comp_floor_usd=None,
        target_companies=[],
        workplace_type=None,
    )

    row = get_profile(db_conn, "rp-clear")
    assert row["skills"] == []
    assert row["experience_summary"] is None
    assert row["target_titles"] == []
    assert row["target_locations"] == []
    assert row["seniority_level"] is None
    assert row["years_of_experience"] is None
    assert row["comp_floor_usd"] is None
    assert row["target_companies"] == []
    assert row["workplace_type"] is None


def test_replace_profile_second_call_overwrites_not_merges(db_conn):
    from jobcannon.db._profiles import get_profile, replace_profile

    _seed_user(db_conn, "rp-twice")
    replace_profile(db_conn, "rp-twice", **_full_snapshot())
    replace_profile(
        db_conn,
        "rp-twice",
        **_full_snapshot(target_titles=["Principal Engineer"], years_of_experience=13),
    )

    row = get_profile(db_conn, "rp-twice")
    assert row["target_titles"] == ["Principal Engineer"]
    assert row["years_of_experience"] == Decimal("13")
    # Untouched keys in the second snapshot still arrived as the same
    # literal values, so they read back unchanged — one row, PK user_id.
    count = db_conn.execute(
        "SELECT COUNT(*) AS n FROM profiles WHERE user_id = %s", ("rp-twice",)
    ).fetchone()["n"]
    assert count == 1


def test_replace_profile_rejects_an_incomplete_snapshot():
    """Every kwarg is required: an omitted field is a TypeError at the call
    site, before any SQL runs — the required-kwarg contract upsert_profile
    applies only to workplace_type, generalized to the whole row. Pure
    Python, no database needed (conn is never touched)."""
    from jobcannon.db._profiles import replace_profile

    snapshot = _full_snapshot()
    del snapshot["comp_floor_usd"]
    with pytest.raises(TypeError):
        replace_profile(object(), "rp-incomplete", **snapshot)


def test_replace_profile_requires_an_existing_user(db_conn):
    """profiles.user_id REFERENCES users(id): no users row, no profile.
    Savepoint-scoped so the aborted statement doesn't poison the fixture's
    outer transaction."""
    from jobcannon.db._profiles import replace_profile

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with db_conn.transaction():
            replace_profile(db_conn, "rp-no-such-user", **_full_snapshot())
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_replace_profile.py`
Expected: every test FAILS with `ImportError: cannot import name 'replace_profile'` (the pure test too — it imports before asserting). With `POSTGRES_ADMIN_DSN` unset, the four `db_conn` tests skip and only the pure one fails — still a failing baseline.

- [x] **Step 3: Add `replace_profile` to `jobcannon/db/_profiles.py`**

Insert immediately after `upsert_profile`'s closing `commit_unless_nested(raw)` line and before `def clear_profile_targets`:

```python
def replace_profile(
    conn: Any,
    user_id: str,
    *,
    skills: list,
    experience_summary: str | None,
    target_titles: list,
    target_locations: list,
    seniority_level: str | None,
    years_of_experience: float | None,
    comp_floor_usd: int | None,
    target_companies: list,
    workplace_type: str | None,
) -> None:
    """Spec 2 (profile editor): write a COMPLETE profile snapshot, overwriting
    every column with the caller's literal value — None becomes NULL, an
    empty list becomes a stored `[]`. Contrast `upsert_profile` above, whose
    per-column COALESCE deliberately preserves a stored value when the caller
    omits a field: that is the right contract for the onboarding picker
    (which only collects a subset), and the wrong one for an editor whose
    visitor has just blanked out their years-of-experience box and expects
    it to stay blank. `workplace_type`'s required-keyword contract is
    generalized here to every column — no defaults, so an incomplete
    snapshot is a TypeError at the call site rather than a silent partial
    write. The sole production caller is `jobcannon/web/profile.py`'s POST
    handler; `jobcannon/web/profile_form.py`'s `parse_profile_form` returns
    a dict keyed exactly by these kwarg names."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    raw.execute(
        "INSERT INTO profiles (user_id, skills, experience_summary, target_titles, "
        "target_locations, seniority_level, years_of_experience, comp_floor_usd, "
        "target_companies, workplace_type, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "skills = EXCLUDED.skills, "
        "experience_summary = EXCLUDED.experience_summary, "
        "target_titles = EXCLUDED.target_titles, "
        "target_locations = EXCLUDED.target_locations, "
        "seniority_level = EXCLUDED.seniority_level, "
        "years_of_experience = EXCLUDED.years_of_experience, "
        "comp_floor_usd = EXCLUDED.comp_floor_usd, "
        "target_companies = EXCLUDED.target_companies, "
        "workplace_type = EXCLUDED.workplace_type, "
        "updated_at = now()",
        (
            user_id,
            Jsonb(skills),
            experience_summary,
            Jsonb(target_titles),
            Jsonb(target_locations),
            seniority_level,
            years_of_experience,
            comp_floor_usd,
            Jsonb(target_companies),
            workplace_type,
        ),
    )
    commit_unless_nested(raw)
```

- [x] **Step 4: Update the module docstring's single-writer sentence**

In the first paragraph of the `jobcannon/db/_profiles.py` module docstring, replace:

```
every write to
this table still goes through exactly one of the two functions defined here,
both in this module, neither anywhere else.
```

with:

```
every write to
this table still goes through exactly one of the three functions defined here
(`upsert_profile`, `replace_profile` — Spec 2's complete-snapshot editor
writer, plain overwrite instead of COALESCE — and `clear_profile_targets`),
all in this module, none anywhere else.
```

- [x] **Step 5: Run the tests to verify they pass**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_replace_profile.py tests/host/test_profiles_dal.py`
Expected: all PASS (`test_profiles_dal.py` proves `upsert_profile` / `get_profile` / `clear_profile_targets` are unaffected).

- [x] **Step 6: Lint and commit**

```bash
uv run --no-sync ruff check jobcannon/db/_profiles.py tests/host/test_replace_profile.py
uv run --no-sync ruff format jobcannon/db/_profiles.py tests/host/test_replace_profile.py
git add jobcannon/db/_profiles.py tests/host/test_replace_profile.py
git commit -m "feat(db): add replace_profile plain-overwrite snapshot writer" -- jobcannon/db/_profiles.py tests/host/test_replace_profile.py
```

---

### Task 2: COUNT primitives for the stats strip

**Files:**
- Modify: `jobcannon/db/_user_actions.py` (append after `list_pipeline_status_entries`)
- Create: `tests/host/test_user_action_counts.py`

**Interfaces:**
- Consumes: the module's existing `_PIPELINE_STATUSES = frozenset({"dismissed", "applied"})` and `raw = conn.raw if hasattr(conn, "raw") else conn` convention. Rows come back through the connection's `dict_row` factory (every existing reader in the module indexes by column name).
- Produces: `count_saved_postings(conn, user_id: str) -> int` and `count_pipeline_statuses(conn, user_id: str) -> dict[str, int]` (keys are exactly `_PIPELINE_STATUSES`, every key present, 0 for absence). Task 4's `edit()` reads both.

- [x] **Step 1: Write the failing tests**

Create `tests/host/test_user_action_counts.py`:

```python
"""count_saved_postings / count_pipeline_statuses (jobcannon/db/_user_actions.py)
— Spec 2's stats-strip COUNT primitives. Rollback-isolated `db_conn`, seed
helpers copied from tests/host/test_user_actions.py (same table shapes)."""

from __future__ import annotations


def _seed_user(conn, user_id):
    conn.execute("INSERT INTO users (id, plan_tier) VALUES (%s, 'free')", (user_id,))


def _seed_company(conn, name):
    return conn.execute(
        "INSERT INTO companies (name, name_raw, ats_platform, ats_slug, ats_probe_status) "
        "VALUES (%s, %s, 'jobvite', %s, 'hit') RETURNING id",
        (name, name, name.lower().replace(" ", "-")),
    ).fetchone()["id"]


def _seed_posting(conn, dedup_key, company_id, *, title="Engineer", company="Acme"):
    return conn.execute(
        "INSERT INTO postings (dedup_key, company_id, title, company) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (dedup_key, company_id, title, company),
    ).fetchone()["id"]


def test_counts_are_zero_for_a_user_with_no_rows(db_conn):
    """Absence is the neutral state (spec: 'row-absence as the neutral
    state'): no watchlists row, no pipeline_status row -> every count is 0
    and every status key is still present."""
    from jobcannon.db._user_actions import count_pipeline_statuses, count_saved_postings

    _seed_user(db_conn, "cnt-empty")

    assert count_saved_postings(db_conn, "cnt-empty") == 0
    assert count_pipeline_statuses(db_conn, "cnt-empty") == {"dismissed": 0, "applied": 0}


def test_counts_follow_save_dismiss_apply_and_unsave(db_conn):
    """seed -> count -> act -> recount, through the module's own writers so
    the counts are pinned to what those writers actually store."""
    from jobcannon.db._user_actions import (
        count_pipeline_statuses,
        count_saved_postings,
        dismiss_posting,
        mark_applied,
        save_posting,
        unsave_posting,
    )

    _seed_user(db_conn, "cnt-flow")
    company_id = _seed_company(db_conn, "Count Co")
    p1 = _seed_posting(db_conn, "cnt-1", company_id)
    p2 = _seed_posting(db_conn, "cnt-2", company_id)
    p3 = _seed_posting(db_conn, "cnt-3", company_id)

    save_posting(db_conn, "cnt-flow", p1)
    save_posting(db_conn, "cnt-flow", p2)
    save_posting(db_conn, "cnt-flow", p2)  # idempotent double-save: still one row
    assert count_saved_postings(db_conn, "cnt-flow") == 2

    dismiss_posting(db_conn, "cnt-flow", p1)
    dismiss_posting(db_conn, "cnt-flow", p2)
    mark_applied(db_conn, "cnt-flow", p3)
    assert count_pipeline_statuses(db_conn, "cnt-flow") == {"dismissed": 2, "applied": 1}

    # dismiss -> apply shares the (user_id, posting_id) row: the status
    # moves between buckets, the total stays 3.
    mark_applied(db_conn, "cnt-flow", p1)
    assert count_pipeline_statuses(db_conn, "cnt-flow") == {"dismissed": 1, "applied": 2}

    unsave_posting(db_conn, "cnt-flow", p1)
    assert count_saved_postings(db_conn, "cnt-flow") == 1


def test_counts_are_scoped_to_the_requested_user(db_conn):
    from jobcannon.db._user_actions import (
        count_pipeline_statuses,
        count_saved_postings,
        dismiss_posting,
        save_posting,
    )

    _seed_user(db_conn, "cnt-a")
    _seed_user(db_conn, "cnt-b")
    company_id = _seed_company(db_conn, "Scope Co")
    posting_id = _seed_posting(db_conn, "cnt-scope-1", company_id)
    save_posting(db_conn, "cnt-a", posting_id)
    dismiss_posting(db_conn, "cnt-a", posting_id)

    assert count_saved_postings(db_conn, "cnt-b") == 0
    assert count_pipeline_statuses(db_conn, "cnt-b") == {"dismissed": 0, "applied": 0}
    assert count_saved_postings(db_conn, "cnt-a") == 1
    assert count_pipeline_statuses(db_conn, "cnt-a") == {"dismissed": 1, "applied": 0}


def test_company_watches_do_not_count_as_saved_postings(db_conn):
    """watchlists holds EITHER a posting_id OR a company_id (m0001 CHECK).
    Only posting saves are 'Saved' on the profile strip."""
    from jobcannon.db._user_actions import count_saved_postings

    _seed_user(db_conn, "cnt-company")
    company_id = _seed_company(db_conn, "Watched Co")
    db_conn.execute(
        "INSERT INTO watchlists (user_id, company_id) VALUES (%s, %s)",
        ("cnt-company", company_id),
    )

    assert count_saved_postings(db_conn, "cnt-company") == 0
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_user_action_counts.py`
Expected: FAIL with `ImportError: cannot import name 'count_saved_postings'` (or SKIP without `POSTGRES_ADMIN_DSN` — if so, note it in your report; the gate has the DSN).

- [x] **Step 3: Add the two primitives to `jobcannon/db/_user_actions.py`**

Append at the end of the module, after `list_pipeline_status_entries`:

```python
def count_saved_postings(conn: Any, user_id: str) -> int:
    """Spec 2 stats strip: how many postings this user has saved. `watchlists`
    holds either a posting watch or a company watch per row (m0001's CHECK
    constraint), so the `posting_id IS NOT NULL` filter is what makes this a
    count of SAVED POSTINGS rather than of watchlist rows. Single SELECT
    COUNT; the first aggregate query over either user-action table."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    row = raw.execute(
        "SELECT COUNT(*) AS n FROM watchlists WHERE user_id = %s AND posting_id IS NOT NULL",
        (user_id,),
    ).fetchone()
    return int(row["n"])


def count_pipeline_statuses(conn: Any, user_id: str) -> dict[str, int]:
    """Spec 2 stats strip: per-status row counts for one user, keyed by every
    member of `_PIPELINE_STATUSES` — a status with no rows is present as 0,
    never missing (row-absence is the neutral state, and the template
    renders "0" rather than hiding the cell). One GROUP BY query; the
    result is built fresh, never mutated in place."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    rows = raw.execute(
        "SELECT status, COUNT(*) AS n FROM pipeline_status WHERE user_id = %s GROUP BY status",
        (user_id,),
    ).fetchall()
    zeroes = {status: 0 for status in _PIPELINE_STATUSES}
    return {**zeroes, **{row["status"]: int(row["n"]) for row in rows}}
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_user_action_counts.py tests/host/test_user_actions.py`
Expected: all PASS.

- [x] **Step 5: Lint and commit**

```bash
uv run --no-sync ruff check jobcannon/db/_user_actions.py tests/host/test_user_action_counts.py
uv run --no-sync ruff format jobcannon/db/_user_actions.py tests/host/test_user_action_counts.py
git add jobcannon/db/_user_actions.py tests/host/test_user_action_counts.py
git commit -m "feat(db): add saved/pipeline COUNT primitives for the profile strip" -- jobcannon/db/_user_actions.py tests/host/test_user_action_counts.py
```

---

### Task 3: `profile_form.py` — pure parse / prefill / echo layer

**Files:**
- Create: `jobcannon/web/profile_form.py`
- Create: `tests/host/test_profile_form.py`

**Interfaces:**
- Consumes (imported from `jobcannon/web/onboarding.py`, which already exports these names; importing an underscore-prefixed helper across `web/` modules has precedent in `web/__init__.py`): `SENIORITY_LEVELS`, `WORKPLACE_TYPES`, `SKILLS_OPTIONS`, `MAX_YEARS_OF_EXPERIENCE`, `MAX_COMP_FLOOR_USD`, `MAX_TITLES_PER_SELECTION`, `MAX_TITLE_LENGTH`, `MAX_COMPANIES_PER_SELECTION`, `MAX_COMPANY_LENGTH`, `_WORKPLACE_FILTERS`, `_WORKPLACE_DB_TO_FORM`, `_has_control_char(value: str) -> bool`, `_too_many_selected_message(kind: str, count: int, limit: int) -> str | None`.
- Produces (Task 4 is the only consumer):
  - `MAX_LOCATIONS_PER_PROFILE = 10`, `MAX_LOCATION_LENGTH = 80`, `MAX_EXPERIENCE_SUMMARY_LENGTH = 2000`
  - `WORKPLACE_FORM_OPTIONS: tuple[str, ...]` — the form values with a non-NULL DB mapping (`("remote", "hybrid", "onsite")`), derived from `_WORKPLACE_FILTERS`, for the template's select (a separate blank option means "no preference" → NULL).
  - `parse_profile_form(form) -> tuple[dict | None, str | None]` — exactly one of the pair is non-None. The dict's keys are exactly `replace_profile`'s nine keyword arguments: `skills: list[str]`, `experience_summary: str | None`, `target_titles: list[str]`, `target_locations: list[str]`, `seniority_level: str | None`, `years_of_experience: float | None`, `comp_floor_usd: int | None`, `target_companies: list[str]`, `workplace_type: str | None` (DB value: `"REMOTE"`/`"HYBRID"`/`"ONSITE"`/`None`).
  - `profile_form_values(row) -> dict[str, Any]` — DB row (or `None`) → template values. Keys: `target_titles`, `target_companies`, `target_locations`, `experience_summary` (all `str`, lists joined by `"\n"`), `checked_skills: list[str]`, `seniority_level`, `years_of_experience`, `comp_floor_usd`, `workplace_type` (all `str`, `""` for NULL).
  - `echo_form_values(form) -> dict[str, Any]` — same key set, raw strings straight from a rejected submission.

- [x] **Step 1: Write the failing tests**

Create `tests/host/test_profile_form.py`:

```python
"""jobcannon/web/profile_form.py — pure parse / prefill / echo layer for the
/profile editor (Spec 2 §2). No Flask app, no database: forms are
werkzeug MultiDicts, rows are plain dicts."""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from werkzeug.datastructures import MultiDict


def _form(**fields):
    """MultiDict from kwargs; a list value becomes repeated keys (checkbox
    groups), everything else a single value."""
    items = []
    for key, value in fields.items():
        if isinstance(value, list):
            items.extend((key, v) for v in value)
        else:
            items.append((key, value))
    return MultiDict(items)


def _valid_form(**overrides):
    fields = {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme\r\nGlobex",
        "target_locations": "Seattle, WA\n\n  Remote  \n",
        "experience_summary": "Twelve years.\r\nMostly backend.",
        "skills": ["python", "sql", "not-a-known-skill"],
        "seniority_level": "staff",
        "years_of_experience": "12.5",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }
    fields.update(overrides)
    return _form(**fields)


# --- parse_profile_form -------------------------------------------------


def test_parse_keys_are_exactly_replace_profile_kwargs():
    """Type-consistency pin between the two halves of the write path: the
    dict parse_profile_form returns is splatted straight into
    replace_profile, whose kwargs are all required. A key drift on either
    side is a TypeError in production; this catches it in a unit test."""
    from jobcannon.db._profiles import replace_profile
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form())
    assert error is None
    kwargs = {
        name
        for name, param in inspect.signature(replace_profile).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert set(parsed) == kwargs


def test_parse_valid_form_produces_a_complete_snapshot():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form())
    assert error is None
    assert parsed == {
        "skills": ["python", "sql"],  # unknown skill filtered, order kept
        "experience_summary": "Twelve years.\nMostly backend.",  # CRLF -> LF
        "target_titles": ["Staff Engineer", "Principal Engineer"],
        "target_locations": ["Seattle, WA", "Remote"],  # blanks dropped, edges stripped
        "seniority_level": "staff",
        "years_of_experience": 12.5,
        "comp_floor_usd": 180000,
        "target_companies": ["Acme", "Globex"],  # CRLF split
        "workplace_type": "REMOTE",
    }


def test_parse_empty_form_is_a_valid_all_blank_snapshot():
    """Blank everything is a legitimate submission: empty lists (a stored
    [], the deliberate-clear semantics) and NULL scalars. There is no
    'pick at least one' rule here — that rule belongs to the picker, whose
    empty submission would otherwise show an unfiltered preview."""
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_form())
    assert error is None
    assert parsed == {
        "skills": [],
        "experience_summary": None,
        "target_titles": [],
        "target_locations": [],
        "seniority_level": None,
        "years_of_experience": None,
        "comp_floor_usd": None,
        "target_companies": [],
        "workplace_type": None,
    }


def test_parse_whitespace_only_summary_is_none():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(experience_summary="  \r\n \n "))
    assert error is None
    assert parsed["experience_summary"] is None


def test_parse_blank_workplace_means_no_preference():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(workplace_type=""))
    assert error is None
    assert parsed["workplace_type"] is None


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("seniority_level", "wizard", "unrecognized seniority level"),
        ("workplace_type", "moon", "unrecognized workplace type"),
        ("years_of_experience", "abc", "years of experience must be a number"),
        ("years_of_experience", "61", "years of experience must be between 0 and 60"),
        ("years_of_experience", "-1", "years of experience must be between 0 and 60"),
        ("comp_floor_usd", "120000.50", "compensation floor must be a whole number"),
        ("comp_floor_usd", "-5", "compensation floor must be between 0 and"),
        ("comp_floor_usd", "2147483648", "compensation floor must be between 0 and"),
    ],
)
def test_parse_scalar_validation_mirrors_the_picker(field, value, fragment):
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(**{field: value}))
    assert parsed is None
    assert fragment in error


def test_parse_too_many_locations_is_rejected():
    from jobcannon.web.profile_form import MAX_LOCATIONS_PER_PROFILE, parse_profile_form

    lines = "\n".join(f"City {i}" for i in range(MAX_LOCATIONS_PER_PROFILE + 1))
    parsed, error = parse_profile_form(_valid_form(target_locations=lines))
    assert parsed is None
    assert error == f"too many locations selected (max {MAX_LOCATIONS_PER_PROFILE})"

    at_cap = "\n".join(f"City {i}" for i in range(MAX_LOCATIONS_PER_PROFILE))
    parsed, error = parse_profile_form(_valid_form(target_locations=at_cap))
    assert error is None
    assert len(parsed["target_locations"]) == MAX_LOCATIONS_PER_PROFILE


def test_parse_overlong_location_is_rejected():
    from jobcannon.web.profile_form import MAX_LOCATION_LENGTH, parse_profile_form

    parsed, error = parse_profile_form(_valid_form(target_locations="x" * (MAX_LOCATION_LENGTH + 1)))
    assert parsed is None
    assert error == f"location exceeds the {MAX_LOCATION_LENGTH}-character limit"


def test_parse_control_char_in_a_list_item_is_rejected():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(target_locations="Seattle\x00WA"))
    assert parsed is None
    assert error == "location contains invalid (control) characters"

    parsed, error = parse_profile_form(_valid_form(target_titles="Staff\x07Engineer"))
    assert parsed is None
    assert error == "title contains invalid (control) characters"


def test_parse_title_and_company_caps_reuse_the_picker_bounds():
    from jobcannon.web.onboarding import (
        MAX_COMPANIES_PER_SELECTION,
        MAX_TITLE_LENGTH,
        MAX_TITLES_PER_SELECTION,
    )
    from jobcannon.web.profile_form import parse_profile_form

    too_many_titles = "\n".join(f"Title {i}" for i in range(MAX_TITLES_PER_SELECTION + 1))
    parsed, error = parse_profile_form(_valid_form(target_titles=too_many_titles))
    assert parsed is None
    assert error == f"too many titles selected (max {MAX_TITLES_PER_SELECTION})"

    too_many_companies = "\n".join(f"Co {i}" for i in range(MAX_COMPANIES_PER_SELECTION + 1))
    parsed, error = parse_profile_form(_valid_form(target_companies=too_many_companies))
    assert parsed is None
    assert error == f"too many companies selected (max {MAX_COMPANIES_PER_SELECTION})"

    parsed, error = parse_profile_form(_valid_form(target_titles="t" * (MAX_TITLE_LENGTH + 1)))
    assert parsed is None
    assert error == f"title exceeds the {MAX_TITLE_LENGTH}-character limit"


def test_parse_summary_allows_newlines_but_no_other_control_chars():
    from jobcannon.web.profile_form import parse_profile_form

    parsed, error = parse_profile_form(_valid_form(experience_summary="line one\n\nline three"))
    assert error is None
    assert parsed["experience_summary"] == "line one\n\nline three"

    parsed, error = parse_profile_form(_valid_form(experience_summary="tab\there"))
    assert parsed is None
    assert error == "experience summary contains invalid (control) characters"


def test_parse_summary_length_cap():
    from jobcannon.web.profile_form import MAX_EXPERIENCE_SUMMARY_LENGTH, parse_profile_form

    parsed, error = parse_profile_form(
        _valid_form(experience_summary="s" * (MAX_EXPERIENCE_SUMMARY_LENGTH + 1))
    )
    assert parsed is None
    assert error == (
        f"experience summary exceeds the {MAX_EXPERIENCE_SUMMARY_LENGTH:,}-character limit"
    )

    parsed, error = parse_profile_form(
        _valid_form(experience_summary="s" * MAX_EXPERIENCE_SUMMARY_LENGTH)
    )
    assert error is None


# --- profile_form_values / echo_form_values -----------------------------


def _row(**overrides):
    row = {
        "user_id": "user_123",
        "skills": ["python", "retired-skill"],
        "experience_summary": "Twelve years.",
        "target_titles": ["Staff Engineer", "Principal Engineer"],
        "target_locations": ["Seattle, WA"],
        "seniority_level": "staff",
        "years_of_experience": Decimal("12.5"),
        "comp_floor_usd": 180000,
        "target_companies": ["Acme"],
        "workplace_type": "REMOTE",
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_profile_form_values_maps_a_row_to_form_strings():
    from jobcannon.web.profile_form import profile_form_values

    assert profile_form_values(_row()) == {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme",
        "target_locations": "Seattle, WA",
        "experience_summary": "Twelve years.",
        "checked_skills": ["python"],  # retired option filtered out
        "seniority_level": "staff",
        "years_of_experience": "12.5",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }


def test_profile_form_values_whole_number_years_renders_without_decimal():
    from jobcannon.web.profile_form import profile_form_values

    assert profile_form_values(_row(years_of_experience=Decimal("12")))["years_of_experience"] == "12"


def test_profile_form_values_null_row_and_null_fields_are_blank():
    from jobcannon.web.profile_form import profile_form_values

    blank = {
        "target_titles": "",
        "target_companies": "",
        "target_locations": "",
        "experience_summary": "",
        "checked_skills": [],
        "seniority_level": "",
        "years_of_experience": "",
        "comp_floor_usd": "",
        "workplace_type": "",
    }
    assert profile_form_values(None) == blank
    assert (
        profile_form_values(
            _row(
                skills=None,
                experience_summary=None,
                target_titles=None,
                target_locations=None,
                seniority_level=None,
                years_of_experience=None,
                comp_floor_usd=None,
                target_companies=None,
                workplace_type=None,
            )
        )
        == blank
    )


def test_profile_form_values_returns_a_fresh_dict_each_call():
    """Immutability guard: mutating one caller's blank dict must not leak
    into the next caller's."""
    from jobcannon.web.profile_form import profile_form_values

    first = profile_form_values(None)
    first["checked_skills"].append("python")
    assert profile_form_values(None)["checked_skills"] == []


def test_echo_form_values_returns_raw_submission_strings():
    from jobcannon.web.profile_form import echo_form_values

    assert echo_form_values(_valid_form(years_of_experience="abc")) == {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme\r\nGlobex",
        "target_locations": "Seattle, WA\n\n  Remote  \n",
        "experience_summary": "Twelve years.\r\nMostly backend.",
        "checked_skills": ["python", "sql", "not-a-known-skill"],
        "seniority_level": "staff",
        "years_of_experience": "abc",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }
    assert echo_form_values(_form()) == {
        "target_titles": "",
        "target_companies": "",
        "target_locations": "",
        "experience_summary": "",
        "checked_skills": [],
        "seniority_level": "",
        "years_of_experience": "",
        "comp_floor_usd": "",
        "workplace_type": "",
    }


def test_workplace_form_options_derive_from_the_forward_map():
    from jobcannon.web.onboarding import _WORKPLACE_FILTERS
    from jobcannon.web.profile_form import WORKPLACE_FORM_OPTIONS

    assert WORKPLACE_FORM_OPTIONS == tuple(
        form for form, db in _WORKPLACE_FILTERS.items() if db is not None
    )
    assert "any" not in WORKPLACE_FORM_OPTIONS
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_profile_form.py`
Expected: every test FAILS with `ModuleNotFoundError: No module named 'jobcannon.web.profile_form'`. (`test_parse_keys_are_exactly_replace_profile_kwargs` also depends on Task 1's `replace_profile`; if Task 1 hasn't landed yet when you run, that single test fails with an ImportError on `replace_profile` — expected mid-wave, and the gate rechecks it.)

- [x] **Step 3: Create `jobcannon/web/profile_form.py`**

```python
"""Pure parse / prefill / echo layer for the /profile editor (Spec 2 §2).

Three functions, no Flask request access, no database — `jobcannon/web/
profile.py` (the route module) is the only caller and hands in the request
form / the `get_profile` row:

- `parse_profile_form(form)` -> `(snapshot, error)`: the POST validator. The
  returned dict's keys are EXACTLY `jobcannon.db._profiles.replace_profile`'s
  keyword arguments (all required), so the route splats it straight through;
  `tests/host/test_profile_form.py` pins that key set against the writer's
  signature. Scalars mirror `jobcannon/web/onboarding.py`'s `_parse_submission`
  rules verbatim (same bounds, same messages); list fields arrive as ONE
  TEXT CONTROL EACH, one entry per line (the spec's "list input"), and are
  parsed by `_parse_lines` into the same validated list shapes the picker
  produces. Two validators are new here because no writer existed for the
  columns before: `target_locations` (count + per-item length + control
  chars, `_parse_titles`' shape) and `experience_summary` (length cap,
  control chars rejected EXCEPT newline — it is a textarea).

  Line parsing strips each entry. The picker deliberately keeps titles
  verbatim (an option's incidental edge whitespace must keep matching the
  corpus title it came from), but on a free-text surface trailing spaces are
  invisible to the visitor, so a stored title with edge whitespace is
  normalized the first time the visitor saves from here. Deliberate.

  There is NO "pick at least one title or company" rule: that belongs to the
  picker, whose empty submission would otherwise show an unfiltered preview.
  A blank editor submission is a legitimate all-clear snapshot.

- `profile_form_values(row)` -> template values from a `get_profile` row (or
  None for a user with no row yet): lists joined with "\\n", numbers as the
  strings the inputs echo (`format(years, "g")` so a whole-number numeric
  renders "12" not "12.0"), NULL -> "", skills filtered to SKILLS_OPTIONS
  (a retired option must not render an unknown checkbox), workplace via
  onboarding's `_WORKPLACE_DB_TO_FORM`.

- `echo_form_values(form)` -> the same key set straight from a rejected
  submission, so the 200 re-render shows exactly what the visitor typed
  (the `start_submit` echo contract).

Importing onboarding's underscore-prefixed helpers across `web/` modules has
precedent (`jobcannon/web/__init__.py` imports `_current_identity`); the
bounds are imported rather than copied so the two surfaces cannot drift.
"""

from __future__ import annotations

import unicodedata
from typing import Any

from jobcannon.web.onboarding import (
    MAX_COMP_FLOOR_USD,
    MAX_COMPANIES_PER_SELECTION,
    MAX_COMPANY_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_TITLES_PER_SELECTION,
    MAX_YEARS_OF_EXPERIENCE,
    SENIORITY_LEVELS,
    SKILLS_OPTIONS,
    WORKPLACE_TYPES,
    _WORKPLACE_DB_TO_FORM,
    _WORKPLACE_FILTERS,
    _has_control_char,
    _too_many_selected_message,
)

# target_locations has no picker precedent to inherit from. Ten locations is
# generous for a job search (the scoring prompt's location_fit reads the
# whole list); 80 characters covers "City, ST, Country" with room, and both
# keep the jsonb payload far inside the bounds the title/company caps were
# sized against.
MAX_LOCATIONS_PER_PROFILE = 10
MAX_LOCATION_LENGTH = 80

# experience_summary is a text column with no schema bound; 2000 characters
# (~300 words) is the scoring prompt's useful ceiling — candidate_context
# feeds the whole string in, so an unbounded field is an unbounded prompt.
MAX_EXPERIENCE_SUMMARY_LENGTH = 2000

# The select's non-blank options: every form value with a real DB value.
# Derived from the forward map, never a second hand-maintained tuple; the
# blank "No preference" option is rendered separately by the template and
# parses to None (NULL) below.
WORKPLACE_FORM_OPTIONS: tuple[str, ...] = tuple(
    form for form, db in _WORKPLACE_FILTERS.items() if db is not None
)


def _split_lines(raw: str | None) -> list[str]:
    """One entry per line: CRLF-normalize, split, strip each entry, drop
    blanks. A textarea submits CRLF per the HTML spec, so the normalization
    is load-bearing, not cosmetic."""
    text = (raw or "").replace("\r\n", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _parse_lines(
    raw: str | None, *, kind: str, singular: str, max_items: int, max_len: int
) -> tuple[list[str] | None, str | None]:
    """Shape-validate a one-per-line list field into the list shape
    upsert_profile/replace_profile store: count cap (via the picker's shared
    message helper, so the wording can't drift), per-item length cap, and
    control-character rejection — `_parse_titles`' three checks."""
    items = _split_lines(raw)
    message = _too_many_selected_message(kind, len(items), max_items)
    if message is not None:
        return None, message
    for item in items:
        if len(item) > max_len:
            return None, f"{singular} exceeds the {max_len}-character limit"
        if _has_control_char(item):
            return None, f"{singular} contains invalid (control) characters"
    return items, None


def _parse_summary(raw: str | None) -> tuple[str | None, str | None]:
    """experience_summary: CRLF-normalized, edge-stripped, blank -> None
    (NULL), length-capped, and control characters rejected EXCEPT "\\n" —
    the one control character a textarea legitimately produces. Tabs are
    rejected with the rest of category Cc: nothing downstream renders them."""
    text = (raw or "").replace("\r\n", "\n").strip()
    if not text:
        return None, None
    if len(text) > MAX_EXPERIENCE_SUMMARY_LENGTH:
        return None, (
            f"experience summary exceeds the {MAX_EXPERIENCE_SUMMARY_LENGTH:,}-character limit"
        )
    if any(ch != "\n" and unicodedata.category(ch) == "Cc" for ch in text):
        return None, "experience summary contains invalid (control) characters"
    return text, None


def parse_profile_form(form: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a POST /profile body into a complete replace_profile snapshot.
    Returns (snapshot, error) — exactly one of the two is non-None."""
    seniority_level = form.get("seniority_level") or None
    if seniority_level is not None and seniority_level not in SENIORITY_LEVELS:
        return None, f"unrecognized seniority level: {seniority_level!r}"

    workplace_type = form.get("workplace_type") or "any"
    if workplace_type not in WORKPLACE_TYPES:
        return None, f"unrecognized workplace type: {workplace_type!r}"

    years_raw = (form.get("years_of_experience") or "").strip()
    years_of_experience: float | None = None
    if years_raw:
        try:
            years_of_experience = float(years_raw)
        except ValueError:
            return None, "years of experience must be a number"
        if not (0 <= years_of_experience <= MAX_YEARS_OF_EXPERIENCE):
            return None, f"years of experience must be between 0 and {MAX_YEARS_OF_EXPERIENCE}"

    comp_floor_raw = (form.get("comp_floor_usd") or "").strip()
    comp_floor_usd: int | None = None
    if comp_floor_raw:
        try:
            comp_floor_usd = int(comp_floor_raw)
        except ValueError:
            return None, "compensation floor must be a whole number"
        if not (0 <= comp_floor_usd <= MAX_COMP_FLOOR_USD):
            return None, f"compensation floor must be between 0 and {MAX_COMP_FLOOR_USD:,}"

    titles, error = _parse_lines(
        form.get("target_titles"),
        kind="titles",
        singular="title",
        max_items=MAX_TITLES_PER_SELECTION,
        max_len=MAX_TITLE_LENGTH,
    )
    if error is not None:
        return None, error

    companies, error = _parse_lines(
        form.get("target_companies"),
        kind="companies",
        singular="company",
        max_items=MAX_COMPANIES_PER_SELECTION,
        max_len=MAX_COMPANY_LENGTH,
    )
    if error is not None:
        return None, error

    locations, error = _parse_lines(
        form.get("target_locations"),
        kind="locations",
        singular="location",
        max_items=MAX_LOCATIONS_PER_PROFILE,
        max_len=MAX_LOCATION_LENGTH,
    )
    if error is not None:
        return None, error

    experience_summary, error = _parse_summary(form.get("experience_summary"))
    if error is not None:
        return None, error

    snapshot = {
        "skills": [s for s in form.getlist("skills") if s and s in SKILLS_OPTIONS],
        "experience_summary": experience_summary,
        "target_titles": titles,
        "target_locations": locations,
        "seniority_level": seniority_level,
        "years_of_experience": years_of_experience,
        "comp_floor_usd": comp_floor_usd,
        "target_companies": companies,
        "workplace_type": _WORKPLACE_FILTERS[workplace_type],
    }
    return snapshot, None


def _blank_form_values() -> dict[str, Any]:
    """A fresh dict every call (never a shared module constant) so a caller
    mutating its copy cannot leak into the next render."""
    return {
        "target_titles": "",
        "target_companies": "",
        "target_locations": "",
        "experience_summary": "",
        "checked_skills": [],
        "seniority_level": "",
        "years_of_experience": "",
        "comp_floor_usd": "",
        "workplace_type": "",
    }


def profile_form_values(row: Any) -> dict[str, Any]:
    """get_profile row (or None: no profiles row yet) -> template values."""
    if row is None:
        return _blank_form_values()
    years = row["years_of_experience"]
    comp_floor = row["comp_floor_usd"]
    return {
        "target_titles": "\n".join(row["target_titles"] or []),
        "target_companies": "\n".join(row["target_companies"] or []),
        "target_locations": "\n".join(row["target_locations"] or []),
        "experience_summary": row["experience_summary"] or "",
        "checked_skills": [s for s in (row["skills"] or []) if s in SKILLS_OPTIONS],
        "seniority_level": row["seniority_level"] or "",
        "years_of_experience": format(years, "g") if years is not None else "",
        "comp_floor_usd": str(comp_floor) if comp_floor is not None else "",
        "workplace_type": _WORKPLACE_DB_TO_FORM.get(row["workplace_type"], ""),
    }


def echo_form_values(form: Any) -> dict[str, Any]:
    """Rejected submission -> template values, verbatim. Skills are echoed
    unfiltered so an unknown value re-renders nothing (the template only
    iterates SKILLS_OPTIONS) rather than being silently dropped from the
    echo — what the visitor checked is what stays checked."""
    return {
        "target_titles": form.get("target_titles") or "",
        "target_companies": form.get("target_companies") or "",
        "target_locations": form.get("target_locations") or "",
        "experience_summary": form.get("experience_summary") or "",
        "checked_skills": list(form.getlist("skills")),
        "seniority_level": form.get("seniority_level") or "",
        "years_of_experience": form.get("years_of_experience") or "",
        "comp_floor_usd": form.get("comp_floor_usd") or "",
        "workplace_type": form.get("workplace_type") or "",
    }
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_profile_form.py`
Expected: all PASS (`test_parse_keys_are_exactly_replace_profile_kwargs` passes once Task 1's `replace_profile` exists — if it is the only failure, report it as expected cross-task timing; it is on the gate's list).

- [x] **Step 5: Lint and commit**

```bash
uv run --no-sync ruff check --fix jobcannon/web/profile_form.py tests/host/test_profile_form.py   # --fix: import ordering only
uv run --no-sync ruff format jobcannon/web/profile_form.py tests/host/test_profile_form.py
git add jobcannon/web/profile_form.py tests/host/test_profile_form.py
git commit -m "feat(web): add profile editor form parse/prefill/echo layer" -- jobcannon/web/profile_form.py tests/host/test_profile_form.py
```

---

### Task 5: `/start` redirects authed visitors; prefill removed (#262, Deviation 2)

**Files:**
- Modify: `jobcannon/web/onboarding.py` (`start()` ≈ lines 578-639, `start_submit()` ≈ 642-707, delete `_profile_prefill` ≈ 539-577, the `get_profile` import at line 100, the `_WORKPLACE_DB_TO_FORM` comment ≈ 244-250)
- Modify: `tests/host/test_start_prefill.py` (delete the prefill tests; keep `test_preview_entries_come_from_build_entry`)
- Modify: `tests/host/test_csrf.py` — ONLY the body of `test_post_start_with_token_mints_anon_user` (≈ line 198)
- Create: `tests/host/test_start_authed_redirect.py`

**Interfaces:**
- Consumes: the module's own `_current_identity() -> ClerkIdentity | None` (fail-open seam, defined at the bottom of the module — runtime resolution, so calling it from `start()` above is fine).
- Produces: `GET|POST /start` → `303 Location: /profile` whenever `_current_identity()` is not None, checked FIRST in both views (before the HX-Request and pending-picker branches). The redirect target is the **literal string** `"/profile"`, not `url_for("profile.edit")`: the `profile` blueprint is Task 4's Wave-2 deliverable and does not exist while this task's tests run; Task 4 pins `url_for("profile.edit") == "/profile"` so the literal cannot drift silently.

- [x] **Step 1: Write the failing redirect tests**

Create `tests/host/test_start_authed_redirect.py`:

```python
"""/start is a purely anonymous surface (Spec 2 decision 2, issue #262): a
visitor whose Clerk identity resolves is 303'd to /profile on GET and POST
alike, before any other branch runs. Monkeypatched-module-attribute pattern
(tests/host/test_pages.py style), no Postgres needed."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.onboarding as onboarding_module

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def _app(verify=lambda req: None):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def _identity():
    return ClerkIdentity(user_id="user_123", claims={"sub": "user_123"})


def _authed_app():
    return _app(verify=lambda req: _identity())


def _forbid_anon_writes(monkeypatch):
    """The anon/pending domain must gain no writes from an authed request:
    a call into either writer is the failure."""

    def _boom(*args, **kwargs):
        raise AssertionError("anon-domain writer called for an authed visitor")

    monkeypatch.setattr(onboarding_module, "mint_anon_user", _boom)
    monkeypatch.setattr(onboarding_module, "upsert_profile", _boom)
    monkeypatch.setattr(onboarding_module, "connection_factory", _boom)


def test_authed_get_start_redirects_to_profile():
    resp = _authed_app().test_client().get("/start")

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_get_start_with_query_still_redirects():
    """The search form's own GET fallback (?q=) is a /start GET too."""
    resp = _authed_app().test_client().get("/start?q=staff&titles=Engineer")

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_hx_get_start_redirects_before_the_fragment_branch():
    """The identity check runs FIRST: an HX-Request from a signed-in visitor
    gets the same 303, never a #picker-options fragment. Unreachable from the
    picker in practice (an authed visitor never renders it), pinned so the
    invariant is 'authed never touches /start', not 'usually'."""
    resp = _authed_app().test_client().get("/start?q=x", headers={"HX-Request": "true"})

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_post_start_redirects_and_writes_nothing(monkeypatch):
    _forbid_anon_writes(monkeypatch)

    resp = _authed_app().test_client().post(
        "/start", data={"titles": ["Engineer"], "seniority_level": ""}
    )

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile")


def test_authed_redirect_carries_the_public_path_cache_headers():
    """Spec §1 cache safety: /start is PUBLIC_PATHS and now returns an
    identity-dependent response, so the shared-cache guard the after_request
    hook applies to every PUBLIC_PATHS response must be on the 303 too."""
    resp = _authed_app().test_client().get("/start")

    assert resp.status_code == 303
    assert "Cookie" in resp.headers.get("Vary", "")
    assert "private" in resp.headers.get("Cache-Control", "")


def test_anonymous_get_start_renders_the_picker(monkeypatch):
    """Byte-identical anonymous flow: no redirect, the picker form renders."""
    monkeypatch.setattr(
        onboarding_module, "_read_picker_options", lambda q="": {"titles": [], "companies": []}
    )
    resp = _app().test_client().get("/start")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'action="/start"' in html
    assert "Location" not in resp.headers


def test_anonymous_invalid_post_start_still_rerenders_200():
    resp = _app().test_client().post("/start", data={"seniority_level": ""})

    assert resp.status_code == 200
    assert "pick at least one title or company" in resp.get_data(as_text=True)


def test_anonymous_valid_post_start_still_redirects_to_preview(monkeypatch):
    """The anon happy path is unchanged: mint, upsert, 302 to /preview.
    start_submit opens `conn.raw.transaction()` around the two writes, so
    the connection double needs a `.raw` with a no-op transaction()."""
    calls = []
    conn = SimpleNamespace(raw=SimpleNamespace(transaction=lambda: contextlib.nullcontext()))
    monkeypatch.setattr(onboarding_module, "connection_factory", lambda: contextlib.nullcontext(conn))
    monkeypatch.setattr(
        onboarding_module, "mint_anon_user", lambda conn: calls.append("mint") or "anon_abc"
    )
    monkeypatch.setattr(
        onboarding_module, "upsert_profile", lambda conn, user_id, **kw: calls.append("upsert")
    )

    resp = _app().test_client().post("/start", data={"titles": ["Engineer"], "seniority_level": ""})

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/preview")
    assert calls == ["mint", "upsert"]


def test_verifier_failure_is_treated_as_anonymous(monkeypatch):
    """_current_identity fails OPEN: a throwing verifier means 'anonymous',
    i.e. today's exact behavior — a form, not a 500 and not a redirect."""
    monkeypatch.setattr(
        onboarding_module, "_read_picker_options", lambda q="": {"titles": [], "companies": []}
    )

    def _boom(req):
        raise RuntimeError("clerk unreachable")

    resp = _app(verify=_boom).test_client().get("/start")

    assert resp.status_code == 200
    assert "Location" not in resp.headers
```

(`_read_picker_options(q: str = "") -> dict[str, list[str]]` is the module-level helper `_picker_context` calls for the title/company option lists; it already fails open to empty options without a DB, so the monkeypatch only keeps the two anonymous GET tests from logging a warning — it is not load-bearing.)

- [x] **Step 2: Run the new tests to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_start_authed_redirect.py`
Expected: the five authed tests FAIL (200 or 302 instead of 303; `test_authed_post_start_redirects_and_writes_nothing` fails with the AssertionError from `_boom`); the anonymous/verifier tests PASS already.

- [x] **Step 3: Add the identity check to both views in `jobcannon/web/onboarding.py`**

In `start()`, insert as the FIRST statements of the body (directly after the docstring, before `pending = get_pending_picker()`):

```python
    # Spec 2 decision 2 / issue #262: /start is a purely anonymous surface.
    # A signed-in visitor is sent to the profile editor before ANY other
    # branch — HX fragment, pending picker, prefill — so the clerk profile
    # domain has exactly one writer (jobcannon/web/profile.py) and this
    # route never has to branch by identity. Same fail-open seam /preview
    # uses: a throwing verifier means "anonymous" and today's exact form.
    # Literal path, not url_for("profile.edit"): the redirect must not
    # depend on blueprint registration order; tests/host/test_profile_route.py
    # pins url_for("profile.edit") == "/profile" so the two can't drift.
    if _current_identity() is not None:
        return redirect("/profile", code=303)
```

In `start_submit()`, insert the same as the FIRST statements of the body (before `is_hx = request.headers.get("HX-Request") == "true"`):

```python
    # #262: identical gate to `start` above — an authed POST is never a
    # picker submission, so no anon user is minted and nothing is written.
    if _current_identity() is not None:
        return redirect("/profile", code=303)
```

Append one sentence to the END of each view's docstring:

- `start()`: `Spec 2 (#262): a resolved Clerk identity short-circuits every branch above with a 303 to /profile — see the first statement of the body.`
- `start_submit()`: `Spec 2 (#262): a resolved Clerk identity 303s to /profile before parsing — the anon domain gains no writes from a signed-in visitor.`

- [x] **Step 4: Run the redirect tests to verify they pass**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_start_authed_redirect.py`
Expected: all PASS.

- [x] **Step 5: Remove `_profile_prefill` and its call site**

In `start()`, replace the prefill block:

```python
    # Spec §5 prefill: a full-page GET with no carried-forward selections
    # seeds the form from the stored profile row. HX fragment renders
    # never prefill — the search box's hx-include carries the visitor's
    # LIVE checked set, so an empty set there is a deliberate uncheck-all,
    # not an absent submission; re-checking saved picks under the
    # visitor's cursor would undo their edit.
    profile_defaults: dict[str, Any] = {}
    if not is_hx and not raw_titles and not raw_companies:
        profile_defaults = _profile_prefill()
        checked_titles = profile_defaults.pop("checked_titles", checked_titles)
        checked_companies = profile_defaults.pop("checked_companies", checked_companies)
    context = _picker_context(
        notice=notice,
        q=q,
        checked_titles=checked_titles,
        checked_companies=checked_companies,
        **profile_defaults,
    )
```

with:

```python
    context = _picker_context(
        notice=notice,
        q=q,
        checked_titles=checked_titles,
        checked_companies=checked_companies,
    )
```

Delete the entire `def _profile_prefill() -> dict[str, Any]:` function (its docstring through the closing `}` of its return dict — ≈ lines 539-577, the function directly above `@onboarding_bp.get("/start", strict_slashes=False)`).

Change the import at line 100 from `from jobcannon.db._profiles import get_profile, upsert_profile` to `from jobcannon.db._profiles import upsert_profile` (`get_profile` had exactly one use, inside the deleted function — confirm with `grep -n get_profile jobcannon/web/onboarding.py` → only the import line remains before you edit it).

Rewrite the `_WORKPLACE_DB_TO_FORM` comment (≈ lines 244-250) so it names its surviving consumer:

```python
# Inverse of _WORKPLACE_FILTERS for the DB -> form direction:
# profiles.workplace_type stores the DB-facing value ('REMOTE'/'HYBRID'/
# 'ONSITE' or NULL), the form speaks the lowercase option values. Derived
# from the forward map — never a second hand-maintained table. The None
# ("any") mapping is excluded: a NULL column renders as "" (no selection).
# Consumed by jobcannon/web/profile_form.py's profile_form_values (Spec 2's
# editor prefill); /start itself no longer prefills — a signed-in visitor
# is redirected to /profile before the form renders (#262).
```

Then `grep -n "prefill\|spec §5\|Spec §5" jobcannon/web/onboarding.py` — any remaining hit in the module docstring or a comment that describes `/start` prefilling from the profile must be reworded to say `/start` redirects authed visitors to `/profile` (Spec 2, #262). Leave unrelated hits alone.

- [x] **Step 6: Prune `tests/host/test_start_prefill.py` to its one surviving test**

Delete these eight tests and the helpers only they used: `test_profile_prefill_maps_row_to_form_values`, `test_profile_prefill_anonymous_is_empty`, `test_profile_prefill_no_row_is_empty`, `test_profile_prefill_fails_open_on_db_error`, `test_profile_prefill_null_fields_echo_as_blank`, `test_start_get_prefills_from_profile`, `test_start_get_carry_forward_beats_prefill`, `test_start_hx_fragment_never_prefills`, plus `_identity`, `_profile_row`, `_patch_db`, and the now-unused imports `contextlib`, `Decimal`, `ClerkIdentity`. Keep `_app`, `_WEBHOOK_SECRET`, the `onboarding_module` import, and `test_preview_entries_come_from_build_entry` verbatim. Replace the module docstring with:

```python
"""/preview's switch to build_entry (Spec 1 Task 5). Route test with a
monkeypatched module attribute, same pattern as tests/host/test_pages.py —
no Postgres needed. (This file also held the GET /start profile-prefill
tests until Spec 2 removed the prefill: a signed-in visitor is now 303'd to
/profile before the picker renders — see tests/host/test_start_authed_redirect.py.)"""
```

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_start_prefill.py`
Expected: `1 passed`.

- [x] **Step 7: Fix the one CSRF test the redirect breaks**

`tests/host/test_csrf.py`'s `db_app` fixture verifies every request as an authed user, and `test_post_start_with_token_mints_anon_user` exercises the ANONYMOUS picker path (it expects a 302 to `/preview` and a minted `anon_` user). Under the redirect it now gets a 303. The test's subject is CSRF-token acceptance on `/start`, which is an anonymous route — make it anonymous. Insert as the first statements of its body (before `client = db_app.test_client()`):

```python
    # Spec 2 (#262): /start 303s a resolved Clerk identity to /profile before
    # the form is parsed, so the token-accepted-and-minted path this test
    # pins is only reachable anonymously. db_app is function-scoped; the
    # override does not leak.
    db_app.config["VERIFY_REQUEST"] = lambda req: None
```

Change nothing else in the file (Task 4 appends two `/profile` tests to it in Wave 2).

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_csrf.py`
Expected: all PASS (or SKIP without `POSTGRES_ADMIN_DSN`; the gate has it).

- [x] **Step 8: Run this task's full green bar**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_start_authed_redirect.py tests/host/test_start_prefill.py tests/host/test_onboarding.py tests/host/test_preview.py tests/host/test_csrf.py`
Expected: all PASS. `test_onboarding.py` and `test_preview.py` use anonymous fixtures and must be unaffected — if either fails, the redirect landed in the wrong place (after a branch instead of first); fix, don't skip.

- [x] **Step 9: Lint and commit**

```bash
uv run --no-sync ruff check jobcannon/web/onboarding.py tests/host/test_start_prefill.py tests/host/test_start_authed_redirect.py tests/host/test_csrf.py
uv run --no-sync ruff format jobcannon/web/onboarding.py tests/host/test_start_prefill.py tests/host/test_start_authed_redirect.py tests/host/test_csrf.py
git add jobcannon/web/onboarding.py tests/host/test_start_prefill.py tests/host/test_start_authed_redirect.py tests/host/test_csrf.py
git commit -m "feat(web): redirect signed-in visitors off /start; drop prefill" -- jobcannon/web/onboarding.py tests/host/test_start_prefill.py tests/host/test_start_authed_redirect.py tests/host/test_csrf.py
```

---

### Task 6: Authed-only "Profile" nav link

**Files:**
- Modify: `jobcannon/web/templates/base.html` (the `{% if visitor_is_authed %}` nav block, ≈ lines 108-120)
- Modify: `tests/host/test_auth_nav.py` (append two tests)

**Interfaces:**
- Consumes: the `visitor_is_authed` template global and `touch_target()`.
- Produces: `<a href="/profile" class="jc-nav-link …" data-profile-nav-link>Profile</a>` inside the authed block. Literal `href="/profile"` for the same Wave-1 reason as Task 5 (Task 4 pins `url_for("profile.edit") == "/profile"`).

- [x] **Step 1: Write the failing tests**

Append to `tests/host/test_auth_nav.py`:

```python
@pytest.mark.parametrize("path", ["/privacy", "/terms"])
def test_signed_in_visitor_sees_the_profile_nav_link(path):
    """Spec 2 §4: the profile editor link sits beside My postings, gated on
    the same visitor_is_authed global — so it survives PUBLIC_PATHS renders
    where g.clerk_user is force-None (#205), exactly like its neighbour."""
    app = _app(
        _host_config(clerk_sign_up_url=_SIGN_UP_URL, clerk_sign_in_url=_SIGN_IN_URL),
        verify=lambda req: ClerkIdentity(
            user_id="user_authed_profile", claims={"sub": "user_authed_profile"}
        ),
    )
    html = app.test_client().get(path).get_data(as_text=True)

    assert "data-profile-nav-link" in html
    assert 'href="/profile"' in html
    assert ">Profile<" in html


@pytest.mark.parametrize("path", ["/privacy", "/terms", "/start"])
def test_anonymous_visitor_does_not_see_the_profile_nav_link(path):
    app = _app(
        _host_config(clerk_sign_up_url=_SIGN_UP_URL, clerk_sign_in_url=_SIGN_IN_URL),
        verify=lambda req: None,
    )
    html = app.test_client().get(path).get_data(as_text=True)

    assert "data-profile-nav-link" not in html
    assert ">Profile<" not in html
```

(`pytest`, `ClerkIdentity`, `_app`, `_host_config`, `_SIGN_UP_URL`, `_SIGN_IN_URL` are already defined/imported in this module.) If `/start` needs a DB read to render anonymously in this module's app, drop `"/start"` from the second parametrize list rather than adding fixtures.

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_auth_nav.py -k profile_nav_link`
Expected: the signed-in cases FAIL (`data-profile-nav-link` absent); the anonymous cases PASS already.

- [x] **Step 3: Add the link to `jobcannon/web/templates/base.html`**

Inside the existing `{% if visitor_is_authed %}` block, directly after the My postings anchor (`data-postings-history-nav-link>My postings</a>`) and before that block's `{% endif %}`, insert:

```html
      {# Spec 2: the profile editor (jobcannon/web/profile.py). Same
         visitor_is_authed gate as My postings above, for the same #205
         reason. Literal href rather than url_for: the link must render on
         every page regardless of blueprint registration order, and
         tests/host/test_profile_route.py pins url_for('profile.edit') to
         exactly "/profile" so this literal cannot drift. #}
      <a href="/profile" class="jc-nav-link {{ touch_target() }}" data-profile-nav-link>Profile</a>
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_auth_nav.py tests/host/test_touch_targets.py tests/test_design_templates.py`
Expected: all PASS (touch-target scan sees the class on the new anchor; design closure sees only existing classes).

- [x] **Step 5: Commit**

```bash
uv run --no-sync ruff check tests/host/test_auth_nav.py
uv run --no-sync ruff format tests/host/test_auth_nav.py
git add jobcannon/web/templates/base.html tests/host/test_auth_nav.py
git commit -m "feat(web): add authed-only Profile nav link" -- jobcannon/web/templates/base.html tests/host/test_auth_nav.py
```

---

## Wave 2 — one task (4), after every Wave-1 task has reported

### Task 4: `/profile` blueprint, template, registration, route + CSRF tests

**Files:**
- Create: `jobcannon/web/profile.py`
- Create: `jobcannon/web/templates/profile.html`
- Create: `tests/host/test_profile_route.py`
- Modify: `jobcannon/web/__init__.py` (the blueprint registration block ≈ lines 1056-1058: add the new import + `register_blueprint` directly after `posting_detail_bp`)
- Modify: `tests/host/test_csrf.py` (append two tests at the end of the file; touch nothing else)

**Interfaces:**
- Consumes (all landed in Wave 1 — grep each name before starting; if any is missing, return `blocked`):
  - `jobcannon.db._profiles.replace_profile(conn, user_id, *, skills, experience_summary, target_titles, target_locations, seniority_level, years_of_experience, comp_floor_usd, target_companies, workplace_type) -> None` and `get_profile(conn, user_id) -> row | None` (Task 1 / existing).
  - `jobcannon.db._user_actions.count_saved_postings(conn, user_id) -> int`, `count_pipeline_statuses(conn, user_id) -> dict[str, int]` keyed `{"dismissed", "applied"}` (Task 2).
  - `jobcannon.web.profile_form.parse_profile_form(form) -> (snapshot | None, error | None)`, `profile_form_values(row) -> dict`, `echo_form_values(form) -> dict`, `WORKPLACE_FORM_OPTIONS` (Task 3). Template value keys: `target_titles`, `target_companies`, `target_locations`, `experience_summary`, `checked_skills`, `seniority_level`, `years_of_experience`, `comp_floor_usd`, `workplace_type`.
  - `jobcannon.web.postings_history._VIEWS == ("saved", "applied", "dismissed")` and endpoint `postings_history.index` accepting `view=`.
  - `jobcannon.web.onboarding.SKILLS_OPTIONS`, `SENIORITY_LEVELS`.
- Produces: blueprint `profile` with endpoints `profile.edit` (`GET /profile`) and `profile.submit` (`POST /profile`); `url_for("profile.edit") == "/profile"` (pinned — Tasks 5 and 6 rely on the literal).

- [x] **Step 1: Write the failing route tests**

Create `tests/host/test_profile_route.py`:

```python
"""GET/POST /profile (jobcannon/web/profile.py) — Spec 2's editor-first
profile page. Monkeypatched-module-attribute pattern (tests/host/test_pages.py
style): the DAL functions the route module imported are replaced on the
module, so no Postgres is needed; the DB-backed CSRF cases live in
tests/host/test_csrf.py."""

from __future__ import annotations

import contextlib
from decimal import Decimal

from flask import url_for
import pytest

from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.profile as profile_module

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="
USER_ID = "user_profile_123"


def _app(verify=lambda req: ClerkIdentity(user_id=USER_ID, claims={"sub": USER_ID})):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def _row(**overrides):
    row = {
        "user_id": USER_ID,
        "skills": ["python", "retired-skill"],
        "experience_summary": "Twelve years.\nMostly backend.",
        "target_titles": ["Staff Engineer", "Principal Engineer"],
        "target_locations": ["Seattle, WA"],
        "seniority_level": "staff",
        "years_of_experience": Decimal("12.5"),
        "comp_floor_usd": 180000,
        "target_companies": ["Acme"],
        "workplace_type": "REMOTE",
        "updated_at": None,
    }
    row.update(overrides)
    return row


@pytest.fixture()
def db(monkeypatch):
    """Stub every DAL call the route module makes. Returns a dict the test
    can inspect: `writes` collects replace_profile kwargs."""
    state = {"row": _row(), "saved": 2, "pipeline": {"applied": 1, "dismissed": 3}, "writes": []}
    monkeypatch.setattr(
        profile_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(profile_module, "get_profile", lambda conn, user_id: state["row"])
    monkeypatch.setattr(profile_module, "count_saved_postings", lambda conn, user_id: state["saved"])
    monkeypatch.setattr(
        profile_module, "count_pipeline_statuses", lambda conn, user_id: dict(state["pipeline"])
    )
    monkeypatch.setattr(
        profile_module,
        "replace_profile",
        lambda conn, user_id, **kw: state["writes"].append((user_id, kw)),
    )
    return state


def _valid_body(**overrides):
    body = {
        "target_titles": "Staff Engineer\nPrincipal Engineer",
        "target_companies": "Acme",
        "target_locations": "Seattle, WA\nRemote",
        "experience_summary": "Twelve years.",
        "skills": ["python", "sql"],
        "seniority_level": "staff",
        "years_of_experience": "12.5",
        "comp_floor_usd": "180000",
        "workplace_type": "remote",
    }
    body.update(overrides)
    return body


# --- routing / auth -------------------------------------------------------


def test_unauthenticated_get_and_post_are_401(db):
    client = _app(verify=lambda req: None).test_client()

    assert client.get("/profile").status_code == 401
    assert client.post("/profile", data=_valid_body()).status_code == 401
    assert db["writes"] == []


def test_url_for_profile_edit_is_exactly_slash_profile():
    """Tasks 5 and 6 redirect/link to the literal "/profile" (they land in
    Wave 1, before this blueprint exists); this is the pin that keeps the
    literal honest."""
    app = _app()
    with app.test_request_context("/"):
        assert url_for("profile.edit") == "/profile"
        assert url_for("profile.submit") == "/profile"


# --- GET ----------------------------------------------------------------


def test_get_prefills_every_field_from_the_row(db):
    html = _app().test_client().get("/profile").get_data(as_text=True)

    assert "Staff Engineer\nPrincipal Engineer</textarea>" in html
    assert ">Acme</textarea>" in html
    assert ">Seattle, WA</textarea>" in html
    assert "Twelve years.\nMostly backend.</textarea>" in html
    assert 'value="python" checked' in html
    assert 'value="sql"' in html and 'value="sql" checked' not in html
    assert "retired-skill" not in html  # filtered by SKILLS_OPTIONS
    assert '<option value="staff" selected>' in html
    assert 'name="years_of_experience"' in html and 'value="12.5"' in html
    assert 'name="comp_floor_usd"' in html and 'value="180000"' in html
    assert '<option value="remote" selected>' in html
    assert 'name="csrf_token"' in html
    assert 'action="/profile"' in html
    assert 'method="post"' in html


def test_get_with_no_row_renders_a_blank_form(db):
    """Spec §2 no-row edge case: a user who signed up without onboarding has
    no profiles row; the form renders empty and the first POST creates it."""
    db["row"] = None
    resp = _app().test_client().get("/profile")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert 'action="/profile"' in html
    assert "checked" not in html
    assert '<option value="" selected>' in html  # blank seniority + workplace
    assert "data-profile-unavailable" not in html


def test_get_renders_the_three_stat_links_in_history_order(db):
    html = _app().test_client().get("/profile").get_data(as_text=True)

    saved = html.index('data-profile-stat="saved"')
    applied = html.index('data-profile-stat="applied"')
    dismissed = html.index('data-profile-stat="dismissed"')
    assert saved < applied < dismissed
    assert 'href="/postings?view=saved"' in html
    assert 'href="/postings?view=applied"' in html
    assert 'href="/postings?view=dismissed"' in html
    assert ">2</span>" in html  # saved
    assert ">1</span>" in html  # applied
    assert ">3</span>" in html  # dismissed


def test_get_renders_zero_counts_rather_than_hiding_cells(db):
    db["saved"] = 0
    db["pipeline"] = {"applied": 0, "dismissed": 0}
    html = _app().test_client().get("/profile").get_data(as_text=True)

    assert html.count(">0</span>") == 3
    assert html.count("data-profile-stat=") == 3


def test_get_saved_flag_renders_the_confirmation(db):
    html = _app().test_client().get("/profile?saved=1").get_data(as_text=True)
    assert "data-profile-saved" in html
    assert "Profile saved." in html

    html = _app().test_client().get("/profile").get_data(as_text=True)
    assert "data-profile-saved" not in html


def test_get_fails_closed_when_the_read_fails(db, monkeypatch):
    """A blank form on a failed read would invite the visitor to save an
    empty snapshot over a profile that exists — destructive. The read
    failure renders an unavailable notice and NO form, at 200 (the page
    itself is fine; the data is not)."""

    def _boom(conn, user_id):
        raise RuntimeError("db down")

    monkeypatch.setattr(profile_module, "get_profile", _boom)
    resp = _app().test_client().get("/profile")
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "data-profile-unavailable" in html
    assert 'action="/profile"' not in html
    assert "data-profile-stat=" not in html


# --- POST ---------------------------------------------------------------


def test_post_valid_snapshot_writes_and_redirects(db):
    resp = _app().test_client().post("/profile", data=_valid_body())

    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile?saved=1")
    assert db["writes"] == [
        (
            USER_ID,
            {
                "skills": ["python", "sql"],
                "experience_summary": "Twelve years.",
                "target_titles": ["Staff Engineer", "Principal Engineer"],
                "target_locations": ["Seattle, WA", "Remote"],
                "seniority_level": "staff",
                "years_of_experience": 12.5,
                "comp_floor_usd": 180000,
                "target_companies": ["Acme"],
                "workplace_type": "REMOTE",
            },
        )
    ]


def test_post_blank_form_clears_everything(db):
    """Empty list = deliberate clear; blank scalar = NULL. The whole point of
    replace_profile over upsert_profile (plan Deviation 1)."""
    resp = _app().test_client().post("/profile", data={"workplace_type": ""})

    assert resp.status_code == 303
    _, kw = db["writes"][0]
    assert kw == {
        "skills": [],
        "experience_summary": None,
        "target_titles": [],
        "target_locations": [],
        "seniority_level": None,
        "years_of_experience": None,
        "comp_floor_usd": None,
        "target_companies": [],
        "workplace_type": None,
    }


def test_post_validation_error_rerenders_200_echoing_every_field(db):
    body = _valid_body(years_of_experience="lots", target_locations="Paris\nBerlin")
    resp = _app().test_client().post("/profile", data=body)
    html = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Location" not in resp.headers
    assert db["writes"] == []
    assert "years of experience must be a number" in html
    assert 'value="lots"' in html  # the bad value echoes, not the stored one
    assert "Paris\nBerlin</textarea>" in html
    assert "Staff Engineer\nPrincipal Engineer</textarea>" in html
    assert 'value="python" checked' in html
    assert 'value="sql" checked' in html
    assert '<option value="remote" selected>' in html
    assert "data-profile-stat=" in html  # stats strip still present on the error page


def test_post_write_failure_is_a_500_not_a_silent_success(db, monkeypatch):
    def _boom(conn, user_id, **kw):
        raise RuntimeError("write failed")

    monkeypatch.setattr(profile_module, "replace_profile", _boom)
    app = _app()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = app.test_client().post("/profile", data=_valid_body())

    assert resp.status_code == 500
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_profile_route.py`
Expected: collection FAILS with `ModuleNotFoundError: No module named 'jobcannon.web.profile'`.

- [x] **Step 3: Create `jobcannon/web/profile.py`**

```python
"""GET/POST /profile — the profile editor (Spec 2, resolving #262's "how does
the user see their profile?").

Editor-first (decision 1): the page IS the edit form, with a compact stats
strip (Saved / Applied / Dismissed, decision 4) above it. NOT in PUBLIC_PATHS,
so jobcannon/web/__init__.py's before_request gate guarantees g.clerk_user and
`g.clerk_user.user_id` IS `profiles.user_id` — direct key, no lookup (the
postings_history.py / consent.py precedent).

Write path (decision 5, plan Deviation 1): the complete snapshot goes through
`replace_profile`, a plain overwrite, so a blanked field stays blank. This
module is the clerk profile domain's ONLY writer — /start 303s a signed-in
visitor here before it can write (decision 2).

Form contract (the `start_submit` shape): a validation failure re-renders at
200 with every submitted value echoed back, never a 4xx or a redirect; success
is PRG — 303 back to GET /profile?saved=1, which renders the confirmation
(the /consent pattern). Plain form POST, no htmx. CSRFProtect is app-wide;
the template carries csrf_token().

Reads fail CLOSED (pages.py's `_read_page_data` posture, for a stronger
reason here): a blank form rendered over a failed read would invite the
visitor to "save" an empty snapshot on top of a profile that exists. So a
read failure renders an unavailable notice and no form at all. Writes are
NOT caught — a failed write must surface as the 500 error page, never as a
redirect that looks like success.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, g, redirect, render_template, request, url_for

from jobcannon.db._profiles import get_profile, replace_profile
from jobcannon.db._user_actions import count_pipeline_statuses, count_saved_postings
from jobcannon.db.pool import connection_factory
from jobcannon.web.onboarding import SENIORITY_LEVELS, SKILLS_OPTIONS
from jobcannon.web.postings_history import _VIEWS
from jobcannon.web.profile_form import (
    WORKPLACE_FORM_OPTIONS,
    echo_form_values,
    parse_profile_form,
    profile_form_values,
)

logger = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__)


def _read_page_data(user_id: str) -> tuple[Any, dict[str, int], bool]:
    """(row, counts, ok). `counts` is keyed by the postings-history view
    tokens: "saved" from watchlists plus every pipeline status. On any
    failure `ok` is False and the caller renders the unavailable branch."""
    try:
        with connection_factory() as conn:
            row = get_profile(conn, user_id)
            counts = {
                "saved": count_saved_postings(conn, user_id),
                **count_pipeline_statuses(conn, user_id),
            }
            return row, counts, True
    except Exception:
        logger.warning(
            "profile page read failed for user %s (rendering unavailable)",
            user_id,
            exc_info=True,
        )
        return None, {}, False


def _stats(counts: dict[str, int]) -> list[dict[str, Any]] | None:
    """Strip cells in postings-history tab order, each linking to that view.
    Iterates `postings_history._VIEWS` so a new view token there shows up
    here without a second hand-maintained list; a count of 0 renders "0"
    (spec §3), never hides the cell. None when there are no counts at all
    (a failed read) — the template hides the strip rather than lying with
    zeros."""
    if not counts:
        return None
    return [
        {
            "view": view,
            "count": counts.get(view, 0),
            "href": url_for("postings_history.index", view=view),
        }
        for view in _VIEWS
    ]


def _render(
    *,
    values: dict[str, Any],
    counts: dict[str, int],
    error: str | None = None,
    saved: bool = False,
    unavailable: bool = False,
) -> str:
    return render_template(
        "profile.html",
        values=values,
        stats=_stats(counts),
        error=error,
        saved=saved,
        unavailable=unavailable,
        skills=SKILLS_OPTIONS,
        seniority_levels=SENIORITY_LEVELS,
        workplace_options=WORKPLACE_FORM_OPTIONS,
    )


@profile_bp.get("/profile", strict_slashes=False)
def edit():
    user_id = g.clerk_user.user_id
    row, counts, ok = _read_page_data(user_id)
    if not ok:
        return _render(values=profile_form_values(None), counts=counts, unavailable=True)
    return _render(
        values=profile_form_values(row),
        counts=counts,
        saved=request.args.get("saved") == "1",
    )


@profile_bp.post("/profile", strict_slashes=False)
def submit():
    user_id = g.clerk_user.user_id
    snapshot, error = parse_profile_form(request.form)
    if error is not None:
        # Echo the submission, not the stored row (the visitor's typing is
        # what they need to fix); the strip still reads live counts so the
        # error page is the same page, minus nothing.
        _, counts, _ok = _read_page_data(user_id)
        return _render(values=echo_form_values(request.form), counts=counts, error=error)
    with connection_factory() as conn:
        replace_profile(conn, user_id, **snapshot)
    return redirect(url_for("profile.edit", saved=1), code=303)
```

- [x] **Step 4: Create `jobcannon/web/templates/profile.html`**

Every class below already exists in `jc.css`; every interactive tag carries `touch_target()`.

```html
{% extends "base.html" %}
{% block title %}Your profile — Job Cannon{% endblock %}
{% block content %}
<h1 class="jc-title jc-title--roomy">Your profile</h1>

{% if unavailable %}
  {# Fail-closed read (see jobcannon/web/profile.py's module docstring): no
     form, so nothing can be saved over a profile we couldn't read. #}
  <p class="jc-error-note" data-profile-unavailable>
    Your profile couldn't be loaded right now. Please try again in a moment.
  </p>
{% else %}

  {% if stats %}
    {# Spec 2 §3: three cells in postings-history tab order, each linking to
       that filtered view. Zero renders "0" — never a hidden cell. #}
    <nav class="jc-cluster" aria-label="Your postings" data-profile-stats>
      {% for stat in stats %}
        <a href="{{ stat.href }}" class="jc-meta jc-link {{ touch_target() }}"
           data-profile-stat="{{ stat.view }}">
          <span class="jc-meta-num">{{ stat.count }}</span>
          <span class="jc-meta-lab">{{ stat.view | capitalize }}</span>
        </a>
      {% endfor %}
    </nav>
  {% endif %}

  {% if saved %}
    <p class="jc-stamp" data-profile-saved>
      <span class="jc-stamp-dot"></span>
      Profile saved.
    </p>
  {% endif %}
  {% if error %}
    <p class="jc-error-note">{{ error }}</p>
  {% endif %}

  <p class="jc-lede">
    Edits show in your feed on the next page load; scoring picks them up on
    its next run.
  </p>

  <form method="post" action="/profile" class="jc-stack">
    <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Target titles (one per line)</span>
      <textarea name="target_titles" rows="4" class="jc-input {{ touch_target() }}">{{ values.target_titles }}</textarea>
    </label>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Target companies (one per line)</span>
      <textarea name="target_companies" rows="3" class="jc-input {{ touch_target() }}">{{ values.target_companies }}</textarea>
    </label>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Target locations (one per line)</span>
      <textarea name="target_locations" rows="3" class="jc-input {{ touch_target() }}">{{ values.target_locations }}</textarea>
    </label>

    <fieldset class="jc-field">
      <legend class="lj-label">Skills</legend>
      {% for skill in skills %}
        <label class="jc-cluster {{ touch_target() }}">
          <input type="checkbox" name="skills" value="{{ skill }}" {% if skill in values.checked_skills %}checked{% endif %} class="{{ touch_target('checkbox') }}"> {{ skill }}
        </label>
      {% endfor %}
    </fieldset>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Seniority level</span>
      <select name="seniority_level" class="jc-input {{ touch_target() }}">
        <option value="" {% if not values.seniority_level %}selected{% endif %}>Prefer not to say</option>
        {% for level in seniority_levels %}
          <option value="{{ level }}" {% if level == values.seniority_level %}selected{% endif %}>{{ level }}</option>
        {% endfor %}
      </select>
    </label>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Years of experience</span>
      <input type="number" name="years_of_experience" min="0" max="60" step="0.5"
             value="{{ values.years_of_experience }}"
             class="jc-input {{ touch_target() }}">
    </label>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Compensation floor (USD/year, optional)</span>
      <input type="number" name="comp_floor_usd" min="0" step="1"
             value="{{ values.comp_floor_usd }}"
             class="jc-input {{ touch_target() }}">
    </label>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Workplace type</span>
      <select name="workplace_type" class="jc-input {{ touch_target() }}">
        <option value="" {% if not values.workplace_type %}selected{% endif %}>No preference</option>
        {% for wt in workplace_options %}
          <option value="{{ wt }}" {% if wt == values.workplace_type %}selected{% endif %}>{{ wt }}</option>
        {% endfor %}
      </select>
    </label>

    <label class="jc-field {{ touch_target() }}">
      <span class="lj-label">Experience summary</span>
      <textarea name="experience_summary" rows="6" class="jc-input {{ touch_target() }}">{{ values.experience_summary }}</textarea>
    </label>

    <button type="submit" class="jc-btn jc-btn--primary {{ touch_target() }}">Save profile</button>
  </form>
{% endif %}
{% endblock %}
```

The `<option value="…" {% if … %}selected{% endif %}>` spacing matters: the route tests assert the literal `<option value="staff" selected>` / `<option value="" selected>`, so keep exactly one space and no trailing attributes.

- [x] **Step 5: Register the blueprint in `jobcannon/web/__init__.py`**

Directly after the existing lines

```python
    from jobcannon.web.posting_detail import posting_detail_bp

    app.register_blueprint(posting_detail_bp)
```

add:

```python
    from jobcannon.web.profile import profile_bp

    app.register_blueprint(profile_bp)
```

`/profile` is deliberately NOT added to `PUBLIC_PATHS`.

- [x] **Step 6: Run the route tests and the design/touch guards**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_profile_route.py tests/test_design_templates.py tests/host/test_touch_targets.py tests/host/test_auth_nav.py`
Expected: all PASS. If `test_design_templates.py` reports an undefined class, you used a class not in `jc.css` — swap for one listed in Global Constraints; never add to `jc.css`.

- [x] **Step 7: Append the two CSRF cases to `tests/host/test_csrf.py`**

At the very end of the file:

```python
def test_post_profile_without_token_is_400():
    """Spec 2 §2: /profile is a plain form POST under the app-wide
    CSRFProtect — a missing token is the 400 CSRF error page, same as every
    other form route pinned above. _stateless_app verifies as USER_ID, so
    the 401 gate is not what rejects it."""
    client = _stateless_app().test_client()
    resp = client.post("/profile", data={"workplace_type": ""})
    assert resp.status_code == 400


@requires_postgres
def test_post_profile_with_token_saves_and_redirects(db_app):
    """Round trip through the real DB: the rendered form carries a valid
    token; posting it back with the token is accepted (303 PRG) and the
    snapshot lands in profiles. Seeds the users row (profiles.user_id FK)
    and marks the handoff done, exactly as the /consent case above does."""
    dsn = db_app.config["_TEST_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("INSERT INTO users (id) VALUES (%s)", (USER_ID,))
        conn.commit()

    client = db_app.test_client()
    with client.session_transaction() as sess:
        sess[_HANDOFF_DONE_KEY] = True
    get_resp = client.get("/profile")
    assert get_resp.status_code == 200
    token = _token_from(get_resp.data)

    resp = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "target_titles": "Staff Engineer",
            "target_locations": "Seattle, WA",
            "experience_summary": "Twelve years.",
            "skills": ["python"],
            "seniority_level": "staff",
            "years_of_experience": "12",
            "comp_floor_usd": "",
            "workplace_type": "remote",
        },
    )
    assert resp.status_code == 303
    assert resp.headers["Location"].endswith("/profile?saved=1")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        row = get_profile(conn, USER_ID)
    assert row is not None
    assert row["target_titles"] == ["Staff Engineer"]
    assert row["target_locations"] == ["Seattle, WA"]
    assert row["experience_summary"] == "Twelve years."
    assert row["workplace_type"] == "REMOTE"
    assert row["comp_floor_usd"] is None
```

Every name used above is already defined/imported at the top of `tests/host/test_csrf.py`: `_stateless_app` (CSRF on, verifies as `USER_ID`, no pool), `_token_from`, `db_app`, `USER_ID`, `requires_postgres`, `psycopg`, `dict_row`, `get_profile`, `_HANDOFF_DONE_KEY`. Add no imports.

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_csrf.py`
Expected: all PASS (DB cases SKIP without `POSTGRES_ADMIN_DSN`).

- [x] **Step 8: Lint and commit**

```bash
uv run --no-sync ruff check --fix jobcannon/web/profile.py tests/host/test_profile_route.py tests/host/test_csrf.py
uv run --no-sync ruff format jobcannon/web/profile.py tests/host/test_profile_route.py tests/host/test_csrf.py
git add jobcannon/web/profile.py jobcannon/web/templates/profile.html jobcannon/web/__init__.py tests/host/test_profile_route.py tests/host/test_csrf.py
git commit -m "feat(web): add /profile editor page with pipeline stats strip" -- jobcannon/web/profile.py jobcannon/web/templates/profile.html jobcannon/web/__init__.py tests/host/test_profile_route.py tests/host/test_csrf.py
```

---

## Gate

### Task 7: Integration gate (single agent, after Task 4 reports)

**Files:**
- Modify: nothing by default. Fallout fixes are limited to test files whose failures are caused by this branch's changes; a production-code fix is only allowed when the failing test proves a defect in a file this plan created or modified, and must be reported explicitly.

**Interfaces:**
- Consumes: every Wave-1/Wave-2 commit on `feat/profile-editor`; `baseline-pytest.log` from Task 0 (baseline **3344 passed / 14 skipped**).
- Produces: a green full suite, clean ruff, and a report listing the delta in test counts.

- [x] **Step 1: Confirm branch and the expected commit set**

```bash
git -C C:/Users/senki/repos/jobcannon rev-parse --abbrev-ref HEAD    # feat/profile-editor
git -C C:/Users/senki/repos/jobcannon log --oneline docs/profile-editor-spec..HEAD
```

Expected: six `feat(...)` commits (Tasks 1, 2, 3, 5, 6, 4) plus any `fix:`/`test:` commits from verifiers. If a task's commit is missing, STOP and report `blocked` — do not implement it yourself.

- [x] **Step 2: Full suite in the background, tee'd, exit code echoed**

```bash
cd C:/Users/senki/repos/jobcannon
uv run --no-sync --active pytest -q --tb=short > gate-pytest.log 2>&1; echo "PYTEST_EXIT=$?" >> gate-pytest.log
```

Run that as a background command (it takes ~8 minutes), then poll `tail -n 5 gate-pytest.log` until `PYTEST_EXIT=` appears. Never sit silent waiting on it — run Step 3 meanwhile.

- [x] **Step 3: Lint while the suite runs**

```bash
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

Expected: both clean. A formatting diff in a file this plan owns: run `uv run --no-sync ruff format <file>` and pathspec-commit it as `style: format <file>`. A diff in any other file is pre-existing — report, do not touch.

- [x] **Step 4: Triage the suite result against the baseline**

Read the tail of `gate-pytest.log` and `baseline-pytest.log`. Expected shape: `PYTEST_EXIT=0`, passed count = 3344 − 8 (deleted prefill tests) + the new tests (Task 1: 5, Task 2: 4, Task 3: ~21, Task 4: 14 + 2 CSRF, Task 5: 9, Task 6: 5 parametrized cases) — roughly **3396 passed**, skipped unchanged at 14 (with `POSTGRES_ADMIN_DSN` set; without it, the DB-backed tests in Tasks 1, 2 and the two CSRF/`db_app` cases skip instead — compare like-with-like against what Task 0 recorded).

For each FAILED test:
1. Is it in `baseline-pytest.log` as failing too? → pre-existing; list it in the report, leave it.
2. Does it exercise `/start`, prefill, `base.html` nav, `_profiles.py`, `_user_actions.py`, or CSRF? → caused by this branch. Read the failure; the fix is almost always a test whose fixture verifies as authed and hits `/start` (expected 200/302, now 303) — apply the same fix as Task 5 Step 7 (`app.config["VERIFY_REQUEST"] = lambda req: None` at the top of that test, with a one-line `# Spec 2 (#262)` comment), or a template guard (`tests/host/test_touch_targets.py`, `tests/test_design_templates.py`) naming a tag or class in `profile.html` — fix the template, never the guard.
3. Otherwise → report it as unexplained; do not "fix" a test you cannot explain.

Re-run only the files you edited, then re-run the FULL suite once more (background + tee, same as Step 2) — the gate's deliverable is a full green run, not a green subset.

- [x] **Step 5: Verify the repo guards explicitly**

```bash
uv run --no-sync --active pytest -q --tb=short tests/test_ported_paths_manifest.py tests/test_design_templates.py tests/test_design_css.py tests/test_design_tokens.py tests/host/test_touch_targets.py tests/host/test_account_export.py
```

Expected: all PASS. (`test_account_export.py` pins the exported `profiles` key set — `get_profile`'s column list must be untouched; `test_ported_paths_manifest.py` is the PR #268 lesson.)

- [x] **Step 6: Commit any fallout and report**

```bash
git add <only the files you edited>
git commit -m "test: reconcile suite with /start redirect and profile editor" -- <same files>
```

Report: final passed/skipped/failed counts vs baseline, the list of fallout files edited (with one line each on why), pre-existing failures left standing, ruff status. `gate-pytest.log` stays untracked (do not commit it).

---

## Execution Strategy (orchestrator playbook)

This plan is built for a Workflow run: Wave 1's five tasks are file-disjoint by the ownership map; Wave 2 is one task that composes them; each task carries its own test cycle; the only serialization points are the Wave-1→Wave-2 boundary and the gate. **13 agents total** (6 implement + 6 verify + 1 gate) — inside the medium size guideline. There is deliberately NO mid-wave gate: Wave 1 leaves the app fully working (every change is additive or self-contained with its own tests), so Task 4 can start the moment the five Wave-1 verifiers report.

**Before dispatch (orchestrator, NOT inside the workflow — stall-retries must never repeat setup):**

1. Execute **Task 0** in this session: `git checkout -b feat/profile-editor` from the `docs/profile-editor-spec` tip, baseline full-suite run tee'd to `baseline-pytest.log` (expect 3344 / 14), `uv run --no-sync ruff check .`, record whether `POSTGRES_ADMIN_DSN` is set. Do not commit the log.
2. Heed the workflow lint hook's FLEET-CONTENTION warning if it fires — dispatch between fleet waves rather than into a hot org-rate window.
3. On stall-kills, run `python ~/.claude/scripts/classify-workflow-stall.py <run-dir>` BEFORE relaunching; API_SILENCE means throttling (switch to plain Agent-tool subagents or wait), MID_COMMAND means fix the prompt. Resume with `resumeFromRunId` — completed agent() calls replay from cache.

**The workflow script:**

```javascript
export const meta = {
  name: 'profile-editor',
  description: 'Two-wave implementation of the Spec 2 profile editor plan: five file-disjoint implementer/verifier pairs, one composing task, one full-suite gate',
  phases: [
    { title: 'Wave 1', detail: 'Tasks 1,2,3,5,6: replace_profile, COUNT primitives, form layer, /start redirect, nav link' },
    { title: 'Wave 2', detail: 'Task 4: /profile blueprint + template + route/CSRF tests' },
    { title: 'Gate', detail: 'Task 7: full suite + ruff + fallout triage vs baseline' },
  ],
}

const REPO = 'C:/Users/senki/repos/jobcannon'
const PLAN = 'docs/superpowers/plans/2026-08-30-profile-editor.md'
const BRANCH = 'feat/profile-editor'

const RESULT_SCHEMA = {
  type: 'object',
  properties: {
    task: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    tests: { type: 'string' },
    notes: { type: 'string' },
    blocked: { type: 'string' },
  },
  // No `required`: a step-0 halt returns bare {"blocked": ...}, which must
  // validate — a required field here would eat exactly the clean-stop path.
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'fixed', 'blocked'] },
    fixes: { type: 'array', items: { type: 'string' } },
    notes: { type: 'string' },
    blocked: { type: 'string' },
  },
  // No `required`, same reason as RESULT_SCHEMA.
}

const COMMON = `Repo: ${REPO}, branch ${BRANCH}. Plan: ${PLAN} — read ONLY its
Global Constraints, the file-ownership map, and the one task section named
below; never other tasks' sections.

Step 0 (verify-only, never fix): run git -C ${REPO} rev-parse --abbrev-ref HEAD.
If it is not "${BRANCH}", STOP and return {"blocked": "wrong branch: <seen>"}.
Never clone, checkout, reset --hard, or stash — the workspace is pre-warmed
(plan Task 0) and shared with sibling agents.

Rules:
- Touch ONLY files your task's ownership row lists. Defects in sibling-owned
  files go in "notes", never into edits.
- Retry-idempotency: before each edit, check whether it is already applied (a
  stall-retry may rerun you); if so, verify it and continue.
- Tests: uv run --no-sync --active pytest -q --tb=short <paths>. Anything that
  may run over 2 minutes: run in the background with output redirected to a
  log file plus an echoed exit code, then read the file — never sit silent.
- Commits: pathspec-limited exactly as the plan's commit steps show; no
  attribution footers. On an index.lock error wait 2 seconds and retry, at
  most 5 times.
- Return JSON only; every string field < 1500 chars — if schema validation
  fails, SHORTEN, never pad.`

const WAVE1 = [
  { label: 'task-1', phase: 'Wave 1', heading: 'Task 1', title: 'replace_profile plain-overwrite writer', tests: 'tests/host/test_replace_profile.py tests/host/test_profiles_dal.py' },
  { label: 'task-2', phase: 'Wave 1', heading: 'Task 2', title: 'COUNT primitives for the stats strip', tests: 'tests/host/test_user_action_counts.py tests/host/test_user_actions.py' },
  { label: 'task-3', phase: 'Wave 1', heading: 'Task 3', title: 'profile_form parse/prefill/echo layer', tests: 'tests/host/test_profile_form.py' },
  { label: 'task-5', phase: 'Wave 1', heading: 'Task 5', title: '/start authed redirect + prefill removal', tests: 'tests/host/test_start_authed_redirect.py tests/host/test_start_prefill.py tests/host/test_onboarding.py tests/host/test_preview.py tests/host/test_csrf.py' },
  { label: 'task-6', phase: 'Wave 1', heading: 'Task 6', title: 'authed-only Profile nav link', tests: 'tests/host/test_auth_nav.py tests/host/test_touch_targets.py' },
]

const WAVE2 = [
  { label: 'task-4', phase: 'Wave 2', heading: 'Task 4', title: '/profile blueprint, template, route + CSRF tests', tests: 'tests/host/test_profile_route.py tests/host/test_csrf.py tests/host/test_auth_nav.py tests/host/test_touch_targets.py' },
]

const implement = (t) => agent(
  `${COMMON}

Implement "${t.heading}: ${t.title}" — the plan section headed "${t.heading}".
Follow its steps in order, TDD included. Your mid-wave green bar is ONLY:
${t.tests} plus tests/test_design_templates.py — the FULL suite belongs to
the gate agent, and expected cross-task fallout is prescribed in the plan's
Task 7, NOT yours to fix.`,
  { label: t.label, phase: t.phase, model: 'sonnet', schema: RESULT_SCHEMA },
)

const verify = (t, r) => agent(
  `${COMMON}

Verify the completed "${t.heading}: ${t.title}" against its plan section on
the L1-L4 ladder, emphasizing L3 (WIRED: every new function/route/template is
imported AND called/registered by the real consumer the plan names — grep for
the call site; an import alone is not wiring) and L4 (run ${t.tests} plus
tests/test_design_templates.py YOURSELF — never trust the implementer's
claim). Implementer report: ${JSON.stringify(r)}. Confirm its commits exist
in git log. Small in-scope defects in THIS task's files: fix and
pathspec-commit with a fix:/test: message. Anything larger, or in
sibling-owned files: record in "notes" only.`,
  { label: `verify-${t.label}`, phase: t.phase, model: 'sonnet', schema: VERDICT_SCHEMA },
)

const runWave = (wave) => pipeline(
  wave,
  (t) => implement(t).then((r) => ({ t, r })),
  ({ t, r }) => (r && r.blocked
    ? { task: t.label, report: r, verdict: { verdict: 'blocked', notes: r.blocked } }
    : verify(t, r).then((v) => ({ task: t.label, report: r, verdict: v }))),
)

const summarize = (wave) => wave.map((x) => ({
  task: x.task,
  verdict: x.verdict && x.verdict.verdict,
  commits: (x.report && x.report.commits) || [],
  notes: [x.report && x.report.notes, x.verdict && x.verdict.notes]
    .filter(Boolean).join(' | ').slice(0, 800),
}))

const gate = (heading, phase, waveSummaries) => agent(
  `${COMMON}

You are the integration gate: execute the plan section "${heading}" in full
(background full-suite run tee'd to a log, ruff, triage against
baseline-pytest.log, the explicit repo-guard run, scoped fallout commits).
Wave reports: ${JSON.stringify(waveSummaries)}. Act on any "notes" naming
real cross-task defects; leave pre-existing baseline failures standing but
report them. The deliverable is a FULL green run, never a subset.`,
  { label: heading.toLowerCase().replace(/[^a-z0-9]+/g, '-'), phase, model: 'sonnet', schema: RESULT_SCHEMA },
)

const wave1 = await runWave(WAVE1)
const w1 = summarize(wave1)
if (w1.some((x) => x.verdict === 'blocked')) {
  return { halted: 'wave 1 blocked', wave1: w1 }
}

const wave2 = await runWave(WAVE2)
const w2 = summarize(wave2)
if (w2.some((x) => x.verdict === 'blocked')) {
  return { halted: 'wave 2 blocked', wave1: w1, wave2: w2 }
}

const gateResult = await gate('Task 7', 'Gate', { wave1: w1, wave2: w2 })
return { wave1: w1, wave2: w2, gate: gateResult }
```

**After the workflow (Task 8 — orchestrator, outside the workflow):**

1. **Final review** — dispatch an `opus48` Agent-tool subagent (owner-preferred verdict tier) to review the full branch diff (`git diff docs/profile-editor-spec...feat/profile-editor`) against the spec (`docs/superpowers/specs/2026-08-30-profile-editor-design.md`) with the two plan deviations as declared exceptions, and against the Living Journal identity rules, with explicit attention to: `replace_profile` being the editor's only write path and `upsert_profile` byte-untouched; the `/start` identity check being the FIRST statement of both views; the anonymous `/start` flow unchanged; every `profile.html` interactive tag carrying `touch_target()`; no new `jc-*` classes; the fail-closed GET rendering no form; stale comments referencing prefill. Fix its findings (inline for small ones; a `sonnet5` subagent for mechanical batches), re-running affected tests.
2. **Verify the suite one last time** (background + tee) and `uv run --no-sync ruff check .`.
3. **STOP — owner gate.** Pushing `feat/profile-editor` and opening a PR are outward-facing actions (global rule 8), and push approval for the `docs/profile-editor-spec` branch itself was never granted. Present the branch summary (commits, test counts vs baseline 3344/14, the two deviations as exercised) and wait for explicit owner approval before any `git push` / `gh pr create`. Note for the PR stage: jobcannon PRs carry the CodeQL default-setup gate with no local equivalent — query alerts via `ref=refs/pull/<N>/head` after opening; the PR closes #262.
