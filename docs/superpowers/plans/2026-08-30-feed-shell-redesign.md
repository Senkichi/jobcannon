# Feed & Shell Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec 1 (`docs/superpowers/specs/2026-08-30-feed-shell-redesign-design.md`): three-tier posting cards with formatted salary, green-chip discipline, an expandable stateless detail view, identity-aware navigation with favicon, sort-select removal, and the /start profile prefill safety patch.

**Architecture:** Two waves of file-disjoint parallel tasks on one shared checkout/branch, each wave followed by an automated full-suite gate agent; data-layer changes land additively in Wave 1 (the old templates keep rendering), templates consume them in Wave 2, and a cleanup pass deletes the one compat shim. The only human gate is the final PR review.

**Tech Stack:** Flask + Jinja2, htmx 2.0.4 (self-hosted), psycopg3/Postgres, pytest, ruff, hand-authored `jc.css` over generated `lj-tokens.css`, Pillow (one-off script only, never a dependency).

## Deviations from the approved spec (owner: review these first)

1. **Salary renders via a precomputed `entry.salary_display` field, not a Jinja template filter.** The spec says "a new Python template filter"; filter registration lives in `web/__init__.py`, which Wave 1's detail-route task must also edit — two parallel tasks on one file. A pure `format_salary()` in a new module, called once in `build_entry`, gives identical rendered output, avoids the collision, and is directly unit-testable.
2. **The sort select is removed outright, not conditioned on `sort_tokens|length == 1`.** The spec's condition would silently auto-restore a raw-wire-token select the moment a second sort token ships — the spec itself says that future select needs human labels, so it should be built deliberately then. Backend `?sort=` parsing stays untouched (still validated, still graceful).
3. **The expand button re-fetches when the panel is already open; closing is via card-click toggle or the panel's Collapse button.** A true single-button toggle needs htmx trigger-filter/handler-ordering tricks that are fragile across htmx versions. Same accepted-quirk family as the spec's own "acting on an expanded row collapses it".

## Global Constraints

Every task's requirements implicitly include all of these.

- **Living Journal identity rules are BINDING** (`docs/design/living-journal.md`): green is an honesty signal only, ≤1 green element per posting row (`jc-chip--why` on at most the first chip); no transform animation (reveal = plain swap); `--lj-gray` is 3:1 large-text-only, body-size gray must be `--lj-gray-text`; no color literals outside generated `lj-tokens.css` (Task 4 adds a documented icon-asset exemption); `lj-*` classes are a closed vocabulary — never invent one; every `jc-*` class used in a template must be defined in `jc.css`; themes via `prefers-color-scheme` only.
- **Never edit `jobcannon/web/static/lj-tokens.css` or `fonts.css`** — generated, drift-guarded by `tests/test_design_tokens.py`.
- **Test invocation, exactly:** `uv run --no-sync --active pytest -q --tb=short <paths>` (bare `pytest` gets hijacked by Windows AppInstaller stubs; `--no-sync` keeps parallel agents from fighting over the venv). `tests/host` DB-backed tests skip without `POSTGRES_ADMIN_DSN`; the pure tests there run regardless.
- **Lint:** `uv run --no-sync ruff check .` and `uv run --no-sync ruff format --check .`; line length 100.
- **Commits:** Conventional Commits (`feat:`/`fix:`/`docs:`/`test:`/`refactor:`/`chore:`…), subject ≤72 chars — `hooks/validate-commit.sh` rejects the whole chained command otherwise. **Pathspec-limited, always:** `git add <own paths>` then `git commit -m "..." -- <own paths>` so a parallel sibling's staged files never ride along. On `index.lock` contention retry up to 5× with a 2s sleep. Never `git stash`. Never push (the orchestrator handles push/PR after the owner gate).
- **Branch:** all work happens on `feat/feed-shell-redesign` in `C:/Users/senki/repos/jobcannon` (cut from the `docs/feed-shell-redesign-spec` tip — main does not carry the spec or this plan). Verify `git rev-parse --abbrev-ref HEAD` before every commit; if wrong, stop and report — never create branches or worktrees.
- **File ownership:** each wave task edits ONLY its listed files (see the map below). A failure in another task's files goes in your report for the gate agent — do not fix it inline.
- **Mid-wave green rule:** sibling tasks are half-landed while you work, so "green" for a wave task means *your own test files* plus the three design tests (`tests/test_design_templates.py tests/test_design_css.py tests/test_design_tokens.py`) pass. The full suite is the gate's job.
- **Retry idempotency:** if an edit's target text is missing, check whether your change is already applied (you may be a stall-retry of yourself) before treating it as an error.
- **Untouchable mechanism:** the Apply control's plain-`fetch()` block in `_posting_row.html` (the `hx-on:click` handler and its long comment) is carried VERBATIM through the Task 8 rewrite — do not simplify, reflow, or "modernize" it.
- **Sentinel spellings are case-sensitive by schema** (`m0001_initial_schema.py`): `salary_currency` uses uppercase `'UNKNOWN'`; `salary_period` uses lowercase `'unknown'`.
- **`postings.direct_url` is a decoy column** (permanently NULL) — never read it.
- **Copy rules:** chip header is `Highlights`; the `signals still computing for this posting` marker text is retained verbatim; NULL detail fields render `Not specified` (never a fabricated negative); missing JD renders `Full description not yet available for this posting.`
- **No hardcoded lists** where the data can derive from stored state (e.g. the detail panel iterates `structural_axes` keys dynamically rather than naming the four axes).

## File ownership map

| Task | Wave | Creates | Modifies |
|---|---|---|---|
| 1 | 1 | `jobcannon/web/salary_fmt.py`, `tests/host/test_salary_fmt.py`, `tests/host/test_feed_entries.py` | `jobcannon/web/why.py`, `jobcannon/web/feed_entries.py`, `tests/host/test_why.py` |
| 2 | 1 | `jobcannon/db/_posting_detail.py`, `jobcannon/web/posting_detail.py`, `jobcannon/web/templates/_posting_detail.html`, `tests/host/test_posting_detail.py` | `jobcannon/web/__init__.py` (sole Wave-1 owner) |
| 3 | 1 | — | `jobcannon/web/static/jc.css` (sole owner, both waves) |
| 4 | 1 | `jobcannon/web/static/favicon.svg`, `jobcannon/web/static/apple-touch-icon.png`, `scripts/gen_touch_icon.py` | `docs/design/living-journal.md` |
| 5 | 1 | `tests/host/test_start_prefill.py` | `jobcannon/web/onboarding.py` |
| 6 | gate | — | prescribed test-fallout files only (see Task 6) |
| 7 | 2 | — | `jobcannon/web/templates/base.html`, `tests/host/test_auth_nav.py`, `tests/host/test_touch_targets.py` |
| 8 | 2 | — | `jobcannon/web/templates/_posting_row.html`, `jobcannon/web/feed_entries.py`, `tests/host/test_feed_entries.py`, `tests/host/test_feed_page.py` |
| 9 | 2 | `tests/host/test_feed_list_template.py` | `jobcannon/web/templates/_feed_list.html`, `jobcannon/web/templates/feed.html`, `jobcannon/web/templates/demo.html`, `jobcannon/web/pages.py` |
| 10 | gate | — | `jobcannon/web/why.py`, `tests/host/test_why.py`, comment-only edits in `jobcannon/web/actions.py` + `jobcannon/web/pages.py`, plus full-suite fallout |

Within a wave, no file appears in two tasks. `feed_entries.py`/`test_feed_entries.py`/`test_why.py`/`why.py` appear across waves — sequential, never concurrent.

---

### Task 0: Pre-warm (orchestrator, before any agent dispatch)

Never put this inside a retried agent prompt (stall-retries would replay it). Run once from the main session:

- [ ] **Step 1: Cut the feature branch from the spec branch tip** (the plan + spec must exist in the shared checkout):

```powershell
git -C C:\Users\senki\repos\jobcannon rev-parse --abbrev-ref HEAD   # expect docs/feed-shell-redesign-spec
git -C C:\Users\senki\repos\jobcannon checkout -b feat/feed-shell-redesign
```

- [ ] **Step 2: Record the baseline.** Note whether `POSTGRES_ADMIN_DSN` is set (gates must compare like-with-like on host-test skips):

```powershell
$env:POSTGRES_ADMIN_DSN -ne $null   # record the boolean
uv run --no-sync --active pytest -q --tb=short 2>&1 | Tee-Object baseline-pytest.log | Select-Object -Last 5
uv run --no-sync ruff check .
```

Expected: suite green (or a recorded pre-existing failure list), ruff clean. `baseline-pytest.log` stays untracked (do not commit it).

---

## Wave 1 — five parallel tasks (1–5)

### Task 1: Salary formatting + chip-kind data layer (additive)

Everything here is additive or behavior-preserving for the current templates: `build_entry`'s `chips` stays a flat `list[str]` (Wave 2's Task 8 swaps it to dicts together with its only consumer). The one visible change: the `"salary listed"` chip is deleted (spec §1 — redundant once the number is prominent).

**Files:**
- Create: `jobcannon/web/salary_fmt.py`
- Create: `tests/host/test_salary_fmt.py`
- Create: `tests/host/test_feed_entries.py`
- Modify: `jobcannon/web/why.py`
- Modify: `jobcannon/web/feed_entries.py`
- Modify: `tests/host/test_why.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces (Wave-2 Task 8 and Task 2 rely on these exact names):
  - `jobcannon.web.salary_fmt.format_salary(row) -> str | None`
  - `jobcannon.web.why.chip_kinds(row, selections_or_profile) -> dict[str, str | None]` with keys exactly `("overlap", "freshness", "seniority", "jd_quality")`
  - `jobcannon.web.feed_entries.dedupe_location(location, workplace_type) -> tuple[str | None, bool]`
  - `build_entry(...)` gains keys `salary_display: str | None`, `display_location: str | None`, `show_workplace_badge: bool` (existing keys unchanged; `chips` still `list[str]` until Task 8)
  - `why_chips` survives as a compat wrapper (flat strings, order freshness → seniority → jd_quality → overlap, no salary chip) until Task 10 deletes it.

- [ ] **Step 1: Write the failing formatter tests** — create `tests/host/test_salary_fmt.py`:

```python
"""format_salary (jobcannon/web/salary_fmt.py) — pure, DB-free compact salary
rendering for the card's primary tier (spec §1). Sentinels are case-sensitive
by schema (m0001): currency uppercase 'UNKNOWN', period lowercase 'unknown'.
No Postgres needed."""

from decimal import Decimal

import pytest

from jobcannon.web.salary_fmt import format_salary


def _row(**overrides):
    row = {
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "USD",
        "salary_period": "annual",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"salary_min": 150000, "salary_max": 200000}, "$150k\u2013200k/yr"),
        ({"salary_min": 150000}, "from $150k/yr"),
        ({"salary_max": 200000}, "up to $200k/yr"),
        ({"salary_min": 150000, "salary_max": 150000}, "$150k/yr"),
        (
            {"salary_min": 80000, "salary_max": 100000, "salary_currency": "EUR"},
            "EUR 80k\u2013100k/yr",
        ),
        (
            {"salary_min": 150000, "salary_max": 200000, "salary_currency": "UNKNOWN"},
            "150k\u2013200k/yr",
        ),
        ({"salary_min": 150000, "salary_period": "unknown"}, "from $150k"),
        ({"salary_min": 60, "salary_max": 80, "salary_period": "hourly"}, "$60\u201380/hr"),
        (
            {"salary_min": Decimal("52.5"), "salary_period": "hourly"},
            "from $52.5/hr",
        ),
        ({"salary_min": 8000, "salary_max": 9500, "salary_period": "monthly"}, "$8k\u20139.5k/mo"),
        ({"salary_min": 147500}, "from $147.5k/yr"),
        ({"salary_min": 147550}, "from $147,550/yr"),
        (
            {"salary_min": Decimal("150000"), "salary_max": Decimal("200000")},
            "$150k\u2013200k/yr",
        ),
    ],
)
def test_format_salary_cases(overrides, expected):
    assert format_salary(_row(**overrides)) == expected


def test_no_salary_data_returns_none():
    assert format_salary(_row()) is None


def test_lowercase_unknown_currency_is_not_the_sentinel():
    # The schema CHECK list makes this unrepresentable in the DB, but the
    # function must not treat the wrong-case spelling as the sentinel.
    assert (
        format_salary(_row(salary_min=150000, salary_currency="unknown"))
        == "unknown 150k/yr"
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_salary_fmt.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobcannon.web.salary_fmt'`

- [ ] **Step 3: Create `jobcannon/web/salary_fmt.py`:**

```python
"""format_salary — compact, honest salary line for the feed card's primary
tier (spec §1). Pure and DB-free, beside jobcannon/web/why.py and
apply_url.py; rendered as the precomputed `entry.salary_display` value
built once in jobcannon.web.feed_entries.build_entry (NOT a registered
Jinja filter — see the plan's deviation note 1).

Sentinel spellings are schema-derived and case-sensitive
(jobcannon/db/migrations/m0001_initial_schema.py): `salary_currency` is
NOT NULL with uppercase 'UNKNOWN' in its CHECK list; `salary_period` is
NOT NULL with lowercase 'unknown'. Currency renders '$' for USD, the bare
ISO code as prefix for any other known currency (no symbol table to
hand-maintain), and nothing for 'UNKNOWN'. psycopg returns the `numeric`
salary columns as Decimal — everything goes through Decimal so no float
artifacts can surface.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

_PERIOD_SUFFIX = {"annual": "/yr", "hourly": "/hr", "monthly": "/mo"}


def _compact_amount(value: Any) -> str:
    number = Decimal(str(value))
    if number >= 1000 and number % 100 == 0:
        # A multiple of 100 has at most one decimal digit in k-form.
        thousands = number / 1000
        if thousands == thousands.to_integral_value():
            return f"{int(thousands)}k"
        return f"{thousands:.1f}k"
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return format(number.normalize(), "f")


def format_salary(row: Any) -> str | None:
    """Compact salary line for one posting row, or None when there is no
    salary data at all (the card then renders no salary line — never a
    placeholder). Requires `salary_min`, `salary_max`, `salary_currency`,
    and `salary_period` by string key — present in every postings
    projection this app renders (jobcannon/db/_feed.py's _SELECT_COLUMNS
    and the detail route's SELECT *)."""
    salary_min = row["salary_min"]
    salary_max = row["salary_max"]
    if salary_min is None and salary_max is None:
        return None

    currency = row["salary_currency"]
    if currency == "USD":
        prefix = "$"
    elif currency and currency != "UNKNOWN":
        prefix = f"{currency} "
    else:
        prefix = ""

    suffix = _PERIOD_SUFFIX.get(row["salary_period"], "")

    if salary_min is not None and salary_max is not None:
        if Decimal(str(salary_min)) == Decimal(str(salary_max)):
            core = _compact_amount(salary_min)
        else:
            core = f"{_compact_amount(salary_min)}\u2013{_compact_amount(salary_max)}"
        return f"{prefix}{core}{suffix}"
    if salary_min is not None:
        return f"from {prefix}{_compact_amount(salary_min)}{suffix}"
    return f"up to {prefix}{_compact_amount(salary_max)}{suffix}"
```

- [ ] **Step 4: Run the formatter tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_salary_fmt.py`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add jobcannon/web/salary_fmt.py tests/host/test_salary_fmt.py
git commit -m "feat: add compact salary formatter for feed cards" -- jobcannon/web/salary_fmt.py tests/host/test_salary_fmt.py
```

- [ ] **Step 6: Refactor `jobcannon/web/why.py`.** Three changes; everything else (band tables, `_get`, `_tokenize`, the four private chip helpers other than `_salary_chip`, `_selection_tokens`, `_overlap_chip`) stays byte-identical:

1. Delete `_salary_chip` (currently lines 131–134) entirely.
2. Replace the whole `why_chips` function (currently lines 158–200) with:

```python
def chip_kinds(
    row: Any, selections_or_profile: Mapping[str, Any] | None
) -> dict[str, str | None]:
    """Chip label per kind, unprioritized and uncapped — the single source
    jobcannon.web.feed_entries.select_chips prioritizes and caps from
    (spec §1 tier 3). Keys are always exactly ("overlap", "freshness",
    "seniority", "jd_quality"); a kind with nothing honest to say maps to
    None.

    Same row contract why_chips documented: `structural_axes`,
    `posted_date_precision`, and `title` by string key — the exact shape
    `jobcannon.db._feed.list_feed_postings` returns. A None or malformed
    `structural_axes` degrades to None for the three axis-derived kinds
    only; overlap reads the row/selections directly and still resolves
    (a posting the axes batch hasn't reached yet still gets chips, just
    fewer).
    """
    selections_or_profile = selections_or_profile or {}
    kinds: dict[str, str | None] = {
        "overlap": _overlap_chip(row, selections_or_profile),
        "freshness": None,
        "seniority": None,
        "jd_quality": None,
    }
    axes = _get(row, "structural_axes")
    if isinstance(axes, Mapping):
        posted_date_precision = _get(row, "posted_date_precision")
        kinds["freshness"] = _freshness_chip(axes, posted_date_precision)
        kinds["seniority"] = _seniority_chip(axes)
        kinds["jd_quality"] = _jd_quality_chip(axes)
    return kinds


def why_chips(row: Any, selections_or_profile: Mapping[str, Any] | None) -> list[str]:
    """COMPAT WRAPPER — deleted by this plan's Task 10. Flat chip strings
    in the legacy render order (freshness, seniority, jd_quality, overlap),
    kept only because feed_entries.build_entry renders flat strings until
    the Wave-2 template rewrite (Task 8) swaps it to
    select_chips(chip_kinds(...)). The "salary listed" chip is gone for
    good (spec §1: redundant once the salary number is prominent in the
    card's primary tier)."""
    kinds = chip_kinds(row, selections_or_profile)
    ordered = (kinds["freshness"], kinds["seniority"], kinds["jd_quality"], kinds["overlap"])
    return [chip for chip in ordered if chip is not None]
```

3. In the module docstring (line 4), change `an age band, "salary listed", a title/skill token` to `an age band, a title/skill token` — the module no longer emits a salary chip.

- [ ] **Step 7: Update `tests/host/test_why.py`.** Two literal swaps plus new tests:

1. Both occurrences of `assert "salary listed" in chips` (currently lines 50 and 70) become `assert "salary listed" not in chips` — pinning the deletion.
2. If either of those tests also asserts an exact chips list or length that counted the salary chip, adjust the expected value by removing `"salary listed"` from it (read the surrounding test bodies; the other expected chips are unchanged).
3. Change the import line `from jobcannon.web.why import why_chips` to `from jobcannon.web.why import chip_kinds, why_chips`.
4. Append at the end of the file:

```python
def test_chip_kinds_keys_stable_and_none_when_nothing_to_say():
    row = {"structural_axes": None, "posted_date_precision": None, "title": "Engineer"}
    assert chip_kinds(row, {}) == {
        "overlap": None,
        "freshness": None,
        "seniority": None,
        "jd_quality": None,
    }


def test_chip_kinds_overlap_resolves_without_axes():
    row = {"structural_axes": None, "posted_date_precision": None, "title": "Staff Engineer"}
    kinds = chip_kinds(row, {"titles": ["Staff Engineer"]})
    assert kinds["overlap"] == "title matches your selections: engineer, staff"
    assert kinds["freshness"] is None


def test_salary_never_produces_a_chip_kind():
    row = {
        "structural_axes": None,
        "posted_date_precision": None,
        "title": "Engineer",
        "salary_min": 150000,
        "salary_max": 200000,
    }
    assert why_chips(row, {}) == []
    assert "salary" not in " ".join(k for k in chip_kinds(row, {}))


def test_why_chips_wrapper_preserves_legacy_order():
    axes = {
        "freshness": {"value": 1.0},
        "seniority_clarity": {"value": True},
        "jd_quality": {"value": 0.9},
    }
    row = {"structural_axes": axes, "posted_date_precision": "exact", "title": "Staff Engineer"}
    assert why_chips(row, {"titles": ["Staff Engineer"]}) == [
        "posted within the last week",
        "level stated in title",
        "JD looks complete",
        "title matches your selections: engineer, staff",
    ]
```

- [ ] **Step 8: Run the why tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_why.py`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add jobcannon/web/why.py tests/host/test_why.py
git commit -m "refactor: split chip_kinds from why_chips, drop salary chip" -- jobcannon/web/why.py tests/host/test_why.py
```

- [ ] **Step 10: Write the failing feed_entries tests** — create `tests/host/test_feed_entries.py`:

```python
"""build_entry / dedupe_location (jobcannon/web/feed_entries.py) — pure,
DB-free. Task 8 extends this file with select_chips tests. No Postgres
needed."""

import pytest

from jobcannon.web.feed_entries import build_entry, dedupe_location


@pytest.mark.parametrize(
    ("location", "workplace_type", "expected"),
    [
        # Location only restates the workplace type -> drop it, keep badge.
        ("Remote", "REMOTE", (None, True)),
        ("remote", "REMOTE", (None, True)),
        # Location says MORE than the workplace type -> badge is redundant.
        ("Remote (US)", "REMOTE", ("Remote (US)", False)),
        # Independent facts -> show both.
        ("San Francisco, CA", "HYBRID", ("San Francisco, CA", True)),
        # No workplace type -> location as-is, no badge.
        ("Austin, TX", None, ("Austin, TX", False)),
        (None, None, (None, False)),
        # No location -> badge carries the fact alone.
        (None, "ONSITE", (None, True)),
        ("", "ONSITE", (None, True)),
    ],
)
def test_dedupe_location(location, workplace_type, expected):
    assert dedupe_location(location, workplace_type) == expected


def _row(**overrides):
    row = {
        "id": 7,
        "title": "Staff Engineer",
        "company": "Acme",
        "location": "Remote",
        "workplace_type": "REMOTE",
        "salary_min": 150000,
        "salary_max": 200000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "structural_axes": None,
        "posted_date_precision": None,
        "saved": None,
        "applied": None,
        "source_urls": ["https://jobs.example/7"],
        "sightings": [],
    }
    row.update(overrides)
    return row


def test_build_entry_carries_display_fields():
    entry = build_entry(_row(), {})
    assert entry["salary_display"] == "$150k\u2013200k/yr"
    assert entry["display_location"] is None
    assert entry["show_workplace_badge"] is True
    assert entry["saved"] is False
    assert entry["applied"] is False
    assert entry["apply_url"] == "https://jobs.example/7"


def test_build_entry_no_salary_renders_none():
    entry = build_entry(_row(salary_min=None, salary_max=None), {})
    assert entry["salary_display"] is None
```

- [ ] **Step 11: Run to verify failure**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_entries.py`
Expected: FAIL — `ImportError: cannot import name 'dedupe_location'`

- [ ] **Step 12: Rewrite `jobcannon/web/feed_entries.py`** (full new content — the docstring is updated, `build_entry`'s existing keys are unchanged):

```python
"""build_entry — composes one `list_feed_postings` / `list_postings_by_ids` row
into the dict shape `_posting_row.html` renders: `row` (the raw DB row),
`chips` (jobcannon.web.why.why_chips(...), possibly empty — the pending
marker in `_posting_row.html` is keyed on `structural_axes` being NULL, not
on an empty chip list), `saved` (bool, from the `saved` column
jobcannon/db/_feed.py now selects), `applied` (bool, from the `applied`
column — `pipeline_status.status = 'applied'`, #177), `apply_url` (the
first usable outbound link, jobcannon.web.apply_url.pick_apply_url, or None
when the posting has none — the row partial renders a disabled control in
that case), `salary_display` (jobcannon.web.salary_fmt.format_salary, or
None for no salary line), and `display_location` / `show_workplace_badge`
(dedupe_location below — spec §1's secondary tier: suppress whichever of
location / workplace-type badge merely restates the other).

Shared by jobcannon/web/pages.py (the authenticated feed's initial render),
jobcannon/web/actions.py (the save/dismiss/apply fragment re-render), and
jobcannon/web/onboarding.py's /preview, so every consumer of
`_posting_row.html` builds the identical entry shape from one place instead
of drifting — the same reasoning jobcannon/db/_feed.py's `_build_filters`
gives for staying a single WHERE-clause builder rather than duplicating
filter logic per caller.
"""

from __future__ import annotations

import re
from typing import Any

from jobcannon.web.apply_url import pick_apply_url
from jobcannon.web.salary_fmt import format_salary
from jobcannon.web.why import why_chips

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: Any) -> set[str]:
    # Same 3-line tokenizer as jobcannon.web.why._tokenize — duplicated
    # rather than importing a private name across module boundaries.
    if not text:
        return set()
    return set(_WORD_RE.findall(str(text).lower()))


def dedupe_location(
    location: str | None, workplace_type: str | None
) -> tuple[str | None, bool]:
    """(display_location, show_workplace_badge) — spec §1 secondary tier.

    Token-based and case-insensitive, so scraped variants ("Remote",
    "remote", "REMOTE - remote") all dedupe against workplace_type
    ('REMOTE'/'HYBRID'/'ONSITE' or None, m0001):

    - location tokens ⊆ workplace tokens (location only restates the
      type, e.g. "Remote" vs REMOTE) → drop the location, keep the badge.
    - workplace tokens ⊆ location tokens (location says more, e.g.
      "Remote (US)") → keep the location, the badge is redundant.
    - disjoint / partial → both carry information, show both.
    """
    loc_tokens = _tokenize(location)
    wt_tokens = _tokenize(workplace_type)
    if not wt_tokens:
        return (location or None), False
    if not loc_tokens:
        return None, True
    if loc_tokens <= wt_tokens:
        return None, True
    if wt_tokens <= loc_tokens:
        return location, False
    return location, True


# An empty why_chips() return renders an empty chip list on purpose — no
# placeholder chip is injected here. The "signals still computing" marker in
# _posting_row.html covers the one state worth flagging (structural_axes
# still NULL), keyed on that column directly; a chips-empty fallback would
# duplicate and contradict it whenever the two conditions diverge.
def build_entry(row: Any, profile_or_selections: Any) -> dict[str, Any]:
    saved = row["saved"]
    applied = row["applied"]
    display_location, show_workplace_badge = dedupe_location(
        row["location"], row["workplace_type"]
    )
    return {
        "row": row,
        "chips": why_chips(row, profile_or_selections),
        "saved": bool(saved) if saved is not None else False,
        "applied": bool(applied) if applied is not None else False,
        "apply_url": pick_apply_url(row),
        "salary_display": format_salary(row),
        "display_location": display_location,
        "show_workplace_badge": show_workplace_badge,
    }
```

- [ ] **Step 13: Run the Task-1 test files + design tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_entries.py tests/host/test_salary_fmt.py tests/host/test_why.py tests/test_design_templates.py tests/test_design_css.py tests/test_design_tokens.py`
Expected: PASS

- [ ] **Step 14: Commit**

```bash
git add jobcannon/web/feed_entries.py tests/host/test_feed_entries.py
git commit -m "feat: add salary_display and location dedup to build_entry" -- jobcannon/web/feed_entries.py tests/host/test_feed_entries.py
```

Known, deliberate cross-task fallout (do NOT fix here — Task 6's table): `assert "salary listed" in html` in test_demo_feed/test_day1_stranger_e2e/test_empty_states/test_preview now fails, because the chip no longer exists.

---

### Task 2: Stateless posting-detail route + fragment

**Files:**
- Create: `jobcannon/db/_posting_detail.py`
- Create: `jobcannon/web/posting_detail.py`
- Create: `jobcannon/web/templates/_posting_detail.html`
- Create: `tests/host/test_posting_detail.py`
- Modify: `jobcannon/web/__init__.py` (this task is the SOLE Wave-1 owner of this file)

**Interfaces:**
- Consumes: `jobcannon.web.public_get` (existing, `web/__init__.py:122` — per-view GET/HEAD/OPTIONS auth opt-out), `jobcannon.engine.scoring_types.build_comp_context(job_row: dict) -> str | None` (existing; uses `.get()`, so it must receive a plain dict, never a HybridRow), `jobcannon.db.pool.connection_factory`.
- Produces: endpoint `posting_detail.detail` at `GET /postings/<int:posting_id>/detail` (Task 8's expand button calls `url_for('posting_detail.detail', posting_id=entry.row.id)`); template `_posting_detail.html` whose root is `<div class="jc-panel" data-posting-detail-panel>`.

**Route-exposure decision (locked):** public via the existing `public_get` per-view marker, NOT a `PUBLIC_PATHS` entry and NEVER a path-prefix check. `PUBLIC_PATHS` is an exact-normalized-path frozenset consumed by three coupled surfaces (auth gate, clerk-js loader gate, the issue-#193 Cache-Control hook) and cannot hold a dynamic path; a `/postings/` prefix exemption would also open `GET /postings` (authed history) and `POST /postings/<id>/save`. With `public_get`: anonymous GET renders; POST of any kind still 401s; an authed visitor flows through the normal identity path (harmless — the fragment renders no user state). The route is NOT in `PUBLIC_PATHS`, so the Cache-Control:private hook correctly skips it (the fragment is identity-independent, cacheable content). No event logging in the view. The spec §3 note ratifies the id-enumeration exposure.

- [ ] **Step 1: Write the failing route tests** — create `tests/host/test_posting_detail.py`:

```python
"""GET /postings/<id>/detail (jobcannon/web/posting_detail.py) — the
expandable card's stateless public fragment (spec §3). Route tests use the
same local-_app + monkeypatched-module-attribute pattern as
tests/host/test_pages.py; no Postgres needed except the final round-trip
test, which carries the requires_postgres marker."""

import contextlib
import datetime

from jobcannon.web import create_app
from jobcannon.web.auth import ClerkIdentity
import jobcannon.web.posting_detail as posting_detail_module

_WEBHOOK_SECRET = "whsec_dGVzdHRlc3R0ZXN0dGVzdHRlc3Q="


def _app(verify=lambda req: None):
    return create_app(
        config={
            "TESTING": True,
            "VERIFY_REQUEST": verify,
            "WEBHOOK_SECRET": _WEBHOOK_SECRET,
        }
    )


def _patch(monkeypatch, row):
    monkeypatch.setattr(
        posting_detail_module,
        "connection_factory",
        lambda: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(
        posting_detail_module, "get_posting_detail", lambda conn, posting_id: row
    )


def _row(**overrides):
    row = {
        "id": 7,
        "title": "Staff Engineer",
        "company": "Acme",
        "location": "Remote",
        "workplace_type": "REMOTE",
        "jd_full": "First paragraph.\n\nSecond paragraph.",
        "description": "Short description.",
        "comp_data_json": None,
        "locations_structured": None,
        "sightings": [],
        "source_urls": [],
        "posted_date": None,
        "posted_date_precision": None,
        "last_seen": None,
        "structural_axes": None,
        "structural_scored_at": None,
    }
    row.update(overrides)
    return row


def test_anonymous_get_renders_jd_full(monkeypatch):
    _patch(monkeypatch, _row())
    resp = _app().test_client().get("/postings/7/detail")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "data-posting-detail-panel" in html
    assert "First paragraph." in html
    assert "Second paragraph." in html
    assert "data-action-collapse" in html


def test_authed_get_renders_identically(monkeypatch):
    _patch(monkeypatch, _row())
    identity = ClerkIdentity(user_id="user_123", claims={"sub": "user_123"})
    resp = _app(verify=lambda req: identity).test_client().get("/postings/7/detail")
    assert resp.status_code == 200
    assert "data-posting-detail-panel" in resp.get_data(as_text=True)


def test_description_fallback_then_honest_note(monkeypatch):
    _patch(monkeypatch, _row(jd_full=None))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "Short description." in html

    _patch(monkeypatch, _row(jd_full=None, description=None))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "Full description not yet available for this posting." in html


def test_unknown_id_is_404(monkeypatch):
    _patch(monkeypatch, None)
    assert _app().test_client().get("/postings/999/detail").status_code == 404


def test_post_is_405_not_401(monkeypatch):
    # public_get opens GET/HEAD/OPTIONS only; an unregistered method on the
    # matched rule must surface as 405 via the routing_exception re-raise.
    _patch(monkeypatch, _row())
    assert _app().test_client().post("/postings/7/detail").status_code == 405


def test_null_axes_render_pending_marker_and_null_fields_say_not_specified(monkeypatch):
    _patch(monkeypatch, _row(location=None))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "signals still computing for this posting" in html
    assert "Not specified" in html
    assert "No confirmed post date" in html


def test_axes_render_dynamically_with_scored_at(monkeypatch):
    _patch(
        monkeypatch,
        _row(
            structural_axes={
                "freshness": {"value": 0.7},
                "seniority_clarity": {"value": True},
            },
            structural_scored_at=datetime.datetime(
                2026, 8, 15, 12, 30, tzinfo=datetime.timezone.utc
            ),
        ),
    )
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "freshness" in html
    assert "seniority clarity" in html
    assert "2026-08-15 12:30" in html
    assert "signals still computing" not in html


def test_comp_context_via_plain_dict(monkeypatch):
    # build_comp_context reads via .get(); the route must hand it a plain
    # dict, never the HybridRow. Patch it at the route module to observe
    # the payload shape.
    seen = {}

    def _fake_comp(job_row):
        seen.update(job_row)
        return "comp context line"

    _patch(monkeypatch, _row(comp_data_json='{"anything": true}'))
    monkeypatch.setattr(posting_detail_module, "build_comp_context", _fake_comp)
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "comp context line" in html
    assert seen == {"comp_data_json": '{"anything": true}'}


def test_timeline_and_sightings(monkeypatch):
    _patch(
        monkeypatch,
        _row(
            posted_date=datetime.date(2026, 8, 1),
            posted_date_precision="approximate",
            last_seen=datetime.datetime(2026, 8, 20, 9, 0, tzinfo=datetime.timezone.utc),
            sightings=[
                {
                    "source": "lever",
                    "source_url": "https://jobs.lever.co/acme/7",
                    "first_seen": "2026-08-01T00:00:00+00:00",
                    "last_seen": "2026-08-20T00:00:00+00:00",
                }
            ],
        ),
    )
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "Posted 2026-08-01" in html
    assert "(approximate)" in html
    assert "Last seen 2026-08-20 09:00" in html
    assert "lever" in html
    assert "first 2026-08-01" in html


def test_proxy_precision_never_claims_a_post_date(monkeypatch):
    # Same anchor-trust rule as jobcannon/web/why.py's freshness chips:
    # 'proxy' precision must not render as an origination date.
    _patch(monkeypatch, _row(posted_date=datetime.date(2026, 8, 1), posted_date_precision="proxy"))
    html = _app().test_client().get("/postings/7/detail").get_data(as_text=True)
    assert "No confirmed post date" in html
    assert "Posted 2026-08-01" not in html
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_posting_detail.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobcannon.web.posting_detail'`

- [ ] **Step 3: Create `jobcannon/db/_posting_detail.py`:**

```python
"""get_posting_detail — the detail fragment's single-posting read (spec §3).

`SELECT *` on purpose, mirroring how jobcannon/db/_jd_full.py and the
engine's scoring path treat full postings rows (this repo has no
JOBS_ALL_COLUMNS-style projection to reuse — see _jd_full.py's
row-projection note): the detail view exists precisely to reach the heavy
columns (`jd_full`, `description`, `comp_data_json`, `locations_structured`,
`sightings`, the structural axes) that `list_feed_postings`' projection
deliberately excludes. Read-only, no user state, no transaction needed.
"""

from __future__ import annotations

from typing import Any


def get_posting_detail(conn: Any, posting_id: int) -> Any:
    """The full postings row for `posting_id`, or None when no such row
    exists (the route 404s)."""
    raw = conn.raw if hasattr(conn, "raw") else conn
    return raw.execute(
        "SELECT * FROM postings WHERE id = %s", (posting_id,)
    ).fetchone()
```

- [ ] **Step 4: Create `jobcannon/web/posting_detail.py`:**

```python
"""GET /postings/<id>/detail — the expandable card's stateless fragment
(spec §3).

Public via the `public_get` per-view marker (jobcannon/web/__init__.py:122,
issue #171's mechanism), NOT via PUBLIC_PATHS and never a path-prefix
check: PUBLIC_PATHS is an exact-normalized-path frozenset consumed by three
coupled surfaces (clerk_auth's gate, the clerk-js loader gate, and the
issue-#193 Cache-Control hook) and cannot express a dynamic rule, while a
`/postings/` prefix exemption would also open GET /postings (the authed
history page) and the POST action routes. With the marker, POST on this
rule stays 405/401 territory and an authed visitor flows through the
normal identity path — harmless, because this fragment renders posting
content only, no user state, which is exactly what lets a row's
Save/Dismiss/Apply DOM survive expansion untouched.

The spec ratifies the exposure delta: any posting's jd_full becomes
fetchable by id enumeration with no auth (scraped-public content). No
events are logged here — per-row impression logging stays in
jobcannon/web/pages.py's authed feed route.

`comp_data_json` is handed to build_comp_context as a plain one-key dict:
that function reads via `.get()`, which the pooled HybridRow (a Sequence)
does not support.

Module-level names (`connection_factory`, `get_posting_detail`,
`build_comp_context`) are deliberate monkeypatch seams, matching every
other route module in this package.
"""

from __future__ import annotations

from flask import Blueprint, abort, render_template

from jobcannon.db._posting_detail import get_posting_detail
from jobcannon.db.pool import connection_factory
from jobcannon.engine.scoring_types import build_comp_context
from jobcannon.web import public_get

posting_detail_bp = Blueprint("posting_detail", __name__)


@posting_detail_bp.get("/postings/<int:posting_id>/detail")
@public_get
def detail(posting_id: int):
    with connection_factory() as conn:
        row = get_posting_detail(conn, posting_id)
    if row is None:
        abort(404)
    comp_context = None
    if row["comp_data_json"]:
        comp_context = build_comp_context({"comp_data_json": row["comp_data_json"]})
    return render_template("_posting_detail.html", row=row, comp_context=comp_context)
```

- [ ] **Step 5: Create `jobcannon/web/templates/_posting_detail.html`:**

```jinja
{# Detail fragment for one posting (spec §3): swapped by htmx into the
   row's persistent slot (#posting-detail-<id>, hx-swap="innerHTML") by
   the expand button in _posting_row.html — NEVER replacing the row, so
   the card's Save/Dismiss/Apply state is untouched by expansion.
   Stateless by design: no user state is read or rendered (the route is
   public via public_get) and no events are logged. Collapse is a LOCAL
   replaceChildren() on the slot — no second fetch. Reveal is a plain
   swap — no transform animation (identity rule 3).

   Uses ONLY classes that already existed in jc.css before this feature
   (jc-panel / jc-stack / jc-note / lj-label / jc-btn / jc-chips) — a
   deliberate zero-dependency stance toward the Wave-1 CSS task. JD text
   is paragraphized by splitting on blank lines in Jinja rather than a
   new white-space CSS recipe for the same reason.

   NULL renders "Not specified" — never a fabricated negative ("Not
   remote"). posted_date follows the same precision-trust rule as
   jobcannon/web/why.py's freshness chips: only 'exact'/'approximate'
   may be presented as an origination date. `direct_url` is a decoy
   column (permanently NULL — apply_url.py's docstring) and is never
   read. The axes list iterates whatever structural_axes actually stores
   rather than hardcoding axis names. #}
<div class="jc-panel" data-posting-detail-panel>
  <div class="jc-stack">
    <h3 class="lj-label">Full description</h3>
    {% if row.jd_full %}
      {% for para in row.jd_full.split("\n\n") %}{% if para.strip() %}
        <p class="jc-note" data-detail-jd>{{ para }}</p>
      {% endif %}{% endfor %}
    {% elif row.description %}
      {% for para in row.description.split("\n\n") %}{% if para.strip() %}
        <p class="jc-note" data-detail-jd>{{ para }}</p>
      {% endif %}{% endfor %}
    {% else %}
      <p class="jc-note" data-detail-jd-missing>Full description not yet available for this posting.</p>
    {% endif %}

    {% if comp_context %}
      <h3 class="lj-label">Compensation context</h3>
      <p class="jc-note" data-detail-comp>{{ comp_context }}</p>
    {% endif %}

    <h3 class="lj-label">Locations</h3>
    {% if row.locations_structured %}
      <ul class="jc-chips" data-detail-locations>
        {% for loc in row.locations_structured %}
          {# _loc_dict (jobcannon/db/_jobs.py) stores dataclass-shaped
             dicts; render the non-empty string fields in stored order
             rather than hardcoding field names. #}
          <li class="jc-note">{% if loc is mapping %}{{ loc.values() | select("string") | select | join(", ") }}{% else %}{{ loc }}{% endif %}</li>
        {% endfor %}
      </ul>
    {% elif row.location %}
      <p class="jc-note" data-detail-locations>{{ row.location }}</p>
    {% else %}
      <p class="jc-note" data-detail-locations>Not specified</p>
    {% endif %}

    <h3 class="lj-label">Timeline</h3>
    {% if row.posted_date and row.posted_date_precision in ("exact", "approximate") %}
      <p class="jc-note" data-detail-posted>Posted {{ row.posted_date.strftime("%Y-%m-%d") }}{% if row.posted_date_precision == "approximate" %} (approximate){% endif %}</p>
    {% else %}
      <p class="jc-note" data-detail-posted>No confirmed post date</p>
    {% endif %}
    {% if row.last_seen %}
      <p class="jc-note" data-detail-last-seen>Last seen {{ row.last_seen.strftime("%Y-%m-%d %H:%M") }} UTC</p>
    {% endif %}

    {% if row.sightings %}
      <h3 class="lj-label">Seen on</h3>
      <ul class="jc-chips" data-detail-sightings>
        {% for s in row.sightings %}{% if s is mapping %}
          <li class="jc-note">{{ s.source or "unknown source" }}{% if s.first_seen %} &middot; first {{ s.first_seen[:10] }}{% endif %}{% if s.last_seen %} &middot; last {{ s.last_seen[:10] }}{% endif %}</li>
        {% endif %}{% endfor %}
      </ul>
    {% endif %}

    <h3 class="lj-label">Signals{% if row.structural_scored_at %} as of {{ row.structural_scored_at.strftime("%Y-%m-%d %H:%M") }} UTC{% endif %}</h3>
    {% if row.structural_axes is mapping %}
      {% for axis_name, axis in row.structural_axes.items() | sort %}
        <p class="jc-note" data-detail-axis>{{ axis_name | replace("_", " ") }}: {% if axis is mapping and axis.value is defined and axis.value is not none %}{{ axis.value }}{% else %}Not specified{% endif %}</p>
      {% endfor %}
    {% else %}
      <p class="jc-note" data-signals-pending>signals still computing for this posting</p>
    {% endif %}

    <button type="button" class="jc-btn {{ touch_target() }}"
            hx-on:click="this.closest('[data-posting-detail]').replaceChildren()"
            data-action-collapse>
      Collapse
    </button>
  </div>
</div>
```

- [ ] **Step 6: Register the blueprint in `jobcannon/web/__init__.py`.** Inside `create_app`, in the blueprint-registration block (deferred inline imports around lines 944–982), append after the last existing `register_blueprint` pair, following the exact idiom the neighbors use:

```python
    from jobcannon.web.posting_detail import posting_detail_bp

    app.register_blueprint(posting_detail_bp)
```

- [ ] **Step 7: Run the route tests + design tests** (the new template is scanned the moment it exists)

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_posting_detail.py tests/test_design_templates.py tests/test_design_css.py`
Expected: PASS

- [ ] **Step 8: Add the Postgres round-trip test.** Read how `tests/host/test_feed_page.py` seeds a posting (its fixture/helper at the top of that file), then append to `tests/host/test_posting_detail.py` a `requires_postgres`-marked test that seeds one posting the same way and asserts:

```python
    row = get_posting_detail(db_conn, seeded_id)
    assert row is not None
    assert row["title"] is not None
    assert get_posting_detail(db_conn, -1) is None
```

(using the real `from jobcannon.db._posting_detail import get_posting_detail` import; skip-if-unset comes free from the conftest fixtures). If `POSTGRES_ADMIN_DSN` is unset locally, confirm the test reports SKIPPED, not an error.

- [ ] **Step 9: Commit**

```bash
git add jobcannon/db/_posting_detail.py jobcannon/web/posting_detail.py jobcannon/web/templates/_posting_detail.html tests/host/test_posting_detail.py jobcannon/web/__init__.py
git commit -m "feat: add stateless posting detail fragment route" -- jobcannon/db/_posting_detail.py jobcannon/web/posting_detail.py jobcannon/web/templates/_posting_detail.html tests/host/test_posting_detail.py jobcannon/web/__init__.py
```

---

### Task 3: `jc.css` — density, green discipline, new compositions

**Files:**
- Modify: `jobcannon/web/static/jc.css` (this task is the file's sole owner for the whole plan)

**Interfaces:**
- Consumes: token names from `lj-tokens.css` only (`--lj-gray-text`, `--lj-green-text`, `--lj-ink`) — never a color literal.
- Produces (Wave-2 templates rely on these existing): `.jc-masthead` wrapper (Task 9), `a.jc-wordmark` link styling (Task 7), `.jc-chip--why` modifier (Task 8), `.jc-row > .jc-stack` tightening and `[data-posting-detail]` spacing (Task 8). The closure test (`tests/test_design_templates.py`) derives its class allowlist from this file, so classes MUST land here before or with the wave that uses them — that ordering is why this is a Wave-1 task.

The current values cited below were verified against the file at planning time; anchor edits on the quoted declarations, not line numbers.

- [ ] **Step 1: Density changes (spec §1).** Three single-declaration edits:

1. In the `.jc-page` rule (~line 46), change `padding: 64px 24px;` → `padding: 40px 24px 64px;` (top padding 64→40; bottom stays 64).
2. In the `.jc-row` rule (~line 178), change `padding: 16px 0;` → `padding: 12px 0;`.
3. Do NOT touch the global `.jc-stack` rule (panels and forms keep 12px) — the row-scoped tightening comes in Step 3.

- [ ] **Step 2: Green discipline (spec §2).** In the `.jc-chip` rule (~line 240), change `color: var(--lj-green-text);` → `color: var(--lj-gray-text);`, then add immediately after the `.jc-chip` rule:

```css
/* The ≤1-green-per-row honesty accent (spec §2): _posting_row.html applies
   this to at most the FIRST rendered chip, and only when that chip's kind
   is overlap or freshness (feed_entries.select_chips owns the decision). */
.jc-chip--why {
  color: var(--lj-green-text);
}
```

Also: in the `.jc-index` rule (~line 180), change `color: var(--lj-gray);` → `color: var(--lj-gray-text);` — 15px/600 is below the large-text threshold, so the 3:1 `--lj-gray` tier was a rule-4 slip. Finally, update the comment (~line 214) that inventories green consumers ("the ONLY green consumers besides the accent rule") so it accurately names `.jc-chip--why` (and the still-unused `.jc-stamp--green`) as the green consumers — chips are no longer green by default.

- [ ] **Step 3: New compositions.** Append at the end of the file:

```css
/* --- Feed & shell redesign (spec 2026-08-30) ------------------------- */

/* Masthead (spec §1): h1 + accent rule (+ ordering label on the feed)
   grouped with tight internal rhythm. Children keep their own type
   recipes; the wrapper owns all spacing. */
.jc-masthead {
  display: grid;
  gap: 6px;
  margin-bottom: 24px;
}
.jc-masthead > * {
  margin: 0;
}

/* Wordmark-as-link (spec §4): renders identically to the old <span>. */
a.jc-wordmark {
  color: var(--lj-ink);
  text-decoration: none;
}

/* Intra-card rhythm tightens without touching the global stack recipe
   (spec §1: 12px -> 8px inside a posting row only). */
.jc-row > .jc-stack {
  gap: 8px;
}

/* Expanded-detail slot (spec §3): sits OUTSIDE the row's stack (an empty
   grid child would still cost a gap), so it owns its own spacing — and
   only when populated. */
[data-posting-detail]:not(:empty) {
  margin-top: 10px;
}
```

- [ ] **Step 4: Verify green only shrinks.** Run both greps; the count of `--lj-green` consumers in `jc.css` must be ≤ the pre-change count (record both numbers in your report):

```bash
grep -c "lj-green" jobcannon/web/static/jc.css
grep -n "lj-green" jobcannon/web/static/jc.css
```

- [ ] **Step 5: Run the design tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/test_design_css.py tests/test_design_templates.py tests/test_design_tokens.py`
Expected: PASS (no color literals introduced; closure self-updates from `jc.css`; token files untouched)

- [ ] **Step 6: Commit**

```bash
git add jobcannon/web/static/jc.css
git commit -m "feat: tighten feed density, demote default chips to gray" -- jobcannon/web/static/jc.css
```

---

### Task 4: Favicon + touch icon assets

**Files:**
- Create: `jobcannon/web/static/favicon.svg`
- Create: `scripts/gen_touch_icon.py`
- Create: `jobcannon/web/static/apple-touch-icon.png` (generated by the script, committed)
- Modify: `docs/design/living-journal.md`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: the two static filenames above — Task 7 references them from `base.html` via `url_for('static', filename=...)`. Pillow is a ONE-OFF tool invocation, never a project dependency (do not touch `pyproject.toml`).

The design-test suite scans only `.html` and the two hand-authored CSS files, so the hex literals here trip no test — the rule-5 amendment below is documentation-enforced, exactly like the existing `legal_page.html` exception.

- [ ] **Step 1: Amend `docs/design/living-journal.md` rule 5 FIRST** (the exemption must exist before the assets that rely on it). Locate rule 5 (the no-color-literals rule that documents the `legal_page.html` inline-style exception) and append to it:

```markdown
**Icon-asset exemption** (2026-08-30, feed & shell redesign spec §4):
standalone icon assets — `jobcannon/web/static/favicon.svg`,
`jobcannon/web/static/apple-touch-icon.png`, and
`scripts/gen_touch_icon.py`, which generates the PNG — may carry literal
hex values, because an SVG fetched as its own document (or a PNG
rasterized offline) cannot resolve CSS custom properties from
`lj-tokens.css`. Constraints: every literal must mirror a current
`lj-tokens.css` value and name the token it mirrors in an adjacent
comment; dark-mode variants come from the same file's dark block (the SVG
carries its own embedded `prefers-color-scheme` media query). The design
tests scan only `.html`/CSS, so this exemption is documentation-enforced,
like the `legal_page.html` inline-style exception above.
```

- [ ] **Step 2: Create `jobcannon/web/static/favicon.svg`** — the simplified cannon glyph (barrel + wheel in ink, green firing arc; the stick figure is reserved for the 180px icon):

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <!-- Living Journal icon-asset exemption (docs/design/living-journal.md
       rule 5): literal hexes mirror lj-tokens.css — .ink mirrors --lj-ink
       (#1E1611 light / #EDE5D8 dark), .arc mirrors --lj-green-text
       (#1F7A40 light / #3DBD55 dark). -->
  <style>
    .ink { stroke: #1E1611; }
    .arc { stroke: #1F7A40; }
    @media (prefers-color-scheme: dark) {
      .ink { stroke: #EDE5D8; }
      .arc { stroke: #3DBD55; }
    }
  </style>
  <g fill="none" stroke-linecap="round">
    <line class="ink" x1="9" y1="23" x2="21" y2="11" stroke-width="7"/>
    <circle class="ink" cx="10" cy="24" r="4.5" stroke-width="3"/>
    <path class="arc" d="M24.5 3.5 A 9 9 0 0 1 29 8.5" stroke-width="2.5"/>
    <line class="arc" x1="25" y1="11.5" x2="27.5" y2="9" stroke-width="2.5"/>
  </g>
</svg>
```

- [ ] **Step 3: Verify the SVG parses**

Run: `uv run --no-sync python -c "import xml.etree.ElementTree as ET; ET.parse('jobcannon/web/static/favicon.svg'); print('svg ok')"`
Expected: `svg ok`

- [ ] **Step 4: Create `scripts/gen_touch_icon.py`:**

```python
"""Generate jobcannon/web/static/apple-touch-icon.png (180x180) — the
cannon-firing-a-stick-figure illustration (spec §4; the 16px favicon.svg
keeps the simplified glyph, this concept does not survive 16x16).

One-off, run manually; the PNG is committed, so Pillow is NOT a project
dependency:

    uv run --no-sync --with pillow python scripts/gen_touch_icon.py

Colors are literal hexes sanctioned by living-journal.md rule 5's
icon-asset exemption, mirroring lj-tokens.css light values: --lj-page
#FAF6EF, --lj-ink #1E1611, --lj-green-text #1F7A40. (A PNG cannot switch
with the OS theme; iOS composites touch icons on light tiles, so the
light palette is the honest single choice.)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 180
PAPER = "#FAF6EF"  # --lj-page (light)
INK = "#1E1611"  # --lj-ink (light)
GREEN = "#1F7A40"  # --lj-green-text (light)

OUT = Path(__file__).resolve().parents[1] / "jobcannon" / "web" / "static" / "apple-touch-icon.png"


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE))
    d = ImageDraw.Draw(img)
    # iOS masks its own corner radius; fill the full square so no
    # transparent corners show through the mask.
    d.rectangle((0, 0, SIZE, SIZE), fill=PAPER)

    # Cannon, lower left: angled barrel + carriage wheel.
    d.line((36, 138, 92, 82), fill=INK, width=26)
    d.ellipse((22, 128, 62, 168), outline=INK, width=12)

    # Muzzle flash: two short green rays off the muzzle (the one green
    # accent — same ≤1-accent spirit as the page identity).
    d.line((100, 62, 112, 50), fill=GREEN, width=8)
    d.line((110, 78, 126, 74), fill=GREEN, width=8)

    # Stick figure mid-flight, upper right: head, torso, legs, and three
    # rotated arm strokes suggesting the whirl.
    cx, cy = 138, 44  # shoulder anchor
    d.ellipse((cx - 9, cy - 26, cx + 9, cy - 8), outline=INK, width=6)  # head
    d.line((cx, cy - 8, cx + 10, cy + 22), fill=INK, width=6)  # torso
    d.line((cx + 10, cy + 22, cx - 2, cy + 40), fill=INK, width=6)  # leg
    d.line((cx + 10, cy + 22, cx + 26, cy + 36), fill=INK, width=6)  # leg
    for angle in (20, 140, 260):  # whirling arms
        rad = math.radians(angle)
        d.line(
            (cx, cy, cx + 20 * math.cos(rad), cy + 20 * math.sin(rad)),
            fill=INK,
            width=5,
        )

    img.save(OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Generate and verify the PNG**

Run: `uv run --no-sync --with pillow python scripts/gen_touch_icon.py`
Expected: `wrote ...apple-touch-icon.png`

Run: `uv run --no-sync --with pillow python -c "from PIL import Image; im = Image.open('jobcannon/web/static/apple-touch-icon.png'); print(im.size, im.mode)"`
Expected: `(180, 180) RGBA`

- [ ] **Step 6: Run the design tests** (they must be indifferent to the new assets)

Run: `uv run --no-sync --active pytest -q --tb=short tests/test_design_css.py tests/test_design_templates.py` and `uv run --no-sync ruff check scripts/gen_touch_icon.py`
Expected: PASS / clean

- [ ] **Step 7: Commit**

```bash
git add jobcannon/web/static/favicon.svg jobcannon/web/static/apple-touch-icon.png scripts/gen_touch_icon.py docs/design/living-journal.md
git commit -m "feat: add cannon favicon and touch icon assets" -- jobcannon/web/static/favicon.svg jobcannon/web/static/apple-touch-icon.png scripts/gen_touch_icon.py docs/design/living-journal.md
```

---

### Task 5: `/start` profile prefill + `/preview` on `build_entry`

**Files:**
- Create: `tests/host/test_start_prefill.py`
- Modify: `jobcannon/web/onboarding.py`

**Interfaces:**
- Consumes: `jobcannon.db._profiles.get_profile` (existing; returns a string-key HybridRow with columns `user_id, skills, experience_summary, target_titles, target_locations, seniority_level, years_of_experience, comp_floor_usd, target_companies, workplace_type, updated_at`, or None; NO `.get()` on rows), `jobcannon.web.feed_entries.build_entry` (Task 1's extended shape — safe concurrently: Task 5's own tests never assert the new keys, and the anonymous `list_feed_postings` branch already selects `NULL::boolean AS saved, applied`, exactly what `build_entry` requires).
- Produces: `_profile_prefill() -> dict[str, Any]` and `_WORKPLACE_DB_TO_FORM` (module-private; Task 10's cleanup and future Spec-2 work may reference them), and `/preview` entries now carrying the full `build_entry` shape.

- [ ] **Step 1: Write the failing tests** — create `tests/host/test_start_prefill.py`:

```python
"""GET /start profile prefill (spec §5) + /preview's switch to build_entry.
Route/unit tests with monkeypatched module attributes, same pattern as
tests/host/test_pages.py — no Postgres needed."""

import contextlib
from decimal import Decimal

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


def _profile_row(**overrides):
    row = {
        "user_id": "user_123",
        "target_titles": ["Staff Engineer"],
        "target_companies": ["Acme"],
        "skills": ["python", "not-a-known-skill"],
        "seniority_level": "staff",
        "years_of_experience": Decimal("12"),
        "comp_floor_usd": 180000,
        "workplace_type": "REMOTE",
    }
    row.update(overrides)
    return row


def _patch_db(monkeypatch, row):
    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(onboarding_module, "get_profile", lambda conn, user_id: row)


def test_profile_prefill_maps_row_to_form_values(monkeypatch):
    _patch_db(monkeypatch, _profile_row())
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {
            "checked_titles": ["Staff Engineer"],
            "checked_companies": ["Acme"],
            "checked_skills": ["python"],  # unknown skill filtered out
            "seniority_level": "staff",
            "years_of_experience": "12",
            "comp_floor_usd": "180000",
            "workplace_type": "remote",  # DB 'REMOTE' -> form value
        }


def test_profile_prefill_anonymous_is_empty(monkeypatch):
    _patch_db(monkeypatch, _profile_row())
    with _app().test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {}


def test_profile_prefill_no_row_is_empty(monkeypatch):
    _patch_db(monkeypatch, None)
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {}


def test_profile_prefill_fails_open_on_db_error(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(onboarding_module, "connection_factory", _boom)
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {}


def test_profile_prefill_null_fields_echo_as_blank(monkeypatch):
    _patch_db(
        monkeypatch,
        _profile_row(
            target_titles=None,
            target_companies=None,
            skills=None,
            seniority_level=None,
            years_of_experience=None,
            comp_floor_usd=None,
            workplace_type=None,
        ),
    )
    app = _app(verify=lambda req: _identity())
    with app.test_request_context("/start"):
        assert onboarding_module._profile_prefill() == {
            "checked_titles": [],
            "checked_companies": [],
            "checked_skills": [],
            "seniority_level": "",
            "years_of_experience": "",
            "comp_floor_usd": "",
            "workplace_type": "",
        }


def test_start_get_prefills_from_profile(monkeypatch):
    _patch_db(monkeypatch, _profile_row())
    monkeypatch.setattr(
        onboarding_module,
        "_read_picker_options",
        lambda q="": {"titles": ["Backend Engineer"], "companies": ["Other Co"]},
    )
    app = _app(verify=lambda req: _identity())
    html = app.test_client().get("/start").get_data(as_text=True)
    # _merge_checked folds the saved title/company into the rendered
    # options even though the corpus window doesn't list them.
    assert "Staff Engineer" in html
    assert "Acme" in html


def test_start_get_carry_forward_beats_prefill(monkeypatch):
    calls = []

    def _get_profile(conn, user_id):
        calls.append(user_id)
        return _profile_row()

    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(onboarding_module, "get_profile", _get_profile)
    monkeypatch.setattr(
        onboarding_module,
        "_read_picker_options",
        lambda q="": {"titles": ["Backend Engineer"], "companies": []},
    )
    app = _app(verify=lambda req: _identity())
    html = app.test_client().get("/start?titles=Backend+Engineer").get_data(as_text=True)
    assert calls == []  # explicit carry-forward: the DB is never read
    assert "Backend Engineer" in html


def test_start_hx_fragment_never_prefills(monkeypatch):
    calls = []

    def _get_profile(conn, user_id):
        calls.append(user_id)
        return _profile_row()

    monkeypatch.setattr(
        onboarding_module, "connection_factory", lambda: contextlib.nullcontext(object())
    )
    monkeypatch.setattr(onboarding_module, "get_profile", _get_profile)
    monkeypatch.setattr(
        onboarding_module,
        "_read_picker_options",
        lambda q="": {"titles": [], "companies": []},
    )
    app = _app(verify=lambda req: _identity())
    resp = app.test_client().get("/start?q=eng", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert calls == []  # a fragment render must never re-check unchecked boxes


def test_preview_entries_come_from_build_entry(monkeypatch):
    row = {
        "id": 1,
        "title": "Staff Engineer",
        "company": "Acme",
        "location": "Remote",
        "workplace_type": "REMOTE",
        "salary_min": 150000,
        "salary_max": 200000,
        "salary_currency": "USD",
        "salary_period": "annual",
        "structural_axes": None,
        "posted_date": None,
        "posted_date_precision": None,
        "last_seen": None,
        "rank_score": None,
        "saved": None,
        "applied": None,
        "source_urls": ["https://jobs.example/1"],
        "sightings": [],
    }
    monkeypatch.setattr(
        onboarding_module, "_read_preview_postings", lambda **kwargs: [row]
    )
    resp = _app().test_client().get("/preview")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Staff Engineer" in html
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_start_prefill.py`
Expected: FAIL — `AttributeError: module 'jobcannon.web.onboarding' has no attribute '_profile_prefill'` (and `get_profile` missing)

- [ ] **Step 3: Update `jobcannon/web/onboarding.py` imports.**

1. Add `from jobcannon.db._profiles import get_profile` beside the other `jobcannon.db` imports.
2. Confirm `connection_factory` is already imported at module level (it backs `_read_picker_options`); add `from jobcannon.db.pool import connection_factory` only if absent.
3. Replace `from jobcannon.web.why import why_chips` (line ~103) with `from jobcannon.web.feed_entries import build_entry`.
4. In the module docstring's chip mention (~line 72, "jobcannon.web.why.why_chips) are pure literal restatements…"), change the reference to `jobcannon.web.feed_entries.build_entry` composing chips via `jobcannon.web.why`.

- [ ] **Step 4: Add the inverse workplace map** immediately after `_WORKPLACE_FILTERS` (~line 241):

```python
# Inverse of _WORKPLACE_FILTERS for the GET /start prefill (spec §5):
# profiles.workplace_type stores the DB-facing value ('REMOTE'/'HYBRID'/
# 'ONSITE' or NULL), the form speaks the lowercase option values. Derived
# from the forward map — never a second hand-maintained table. The None
# ("any") mapping is excluded: a NULL column prefills as "" (no
# selection), the same rendering as an untouched form.
_WORKPLACE_DB_TO_FORM = {db: form for form, db in _WORKPLACE_FILTERS.items() if db is not None}
```

- [ ] **Step 5: Add `_profile_prefill`** immediately above `start()` (~line 530):

```python
def _profile_prefill() -> dict[str, Any]:
    """Stored-profile defaults for a fresh, full-page GET /start render
    (spec §5): defuses the footgun where a revisit + unchecked resubmit
    silently wipes saved picks (upsert_profile submits literally, by
    design). Uses the same identity re-check /preview uses
    (_current_identity — /start is PUBLIC_PATHS, so g.clerk_user is
    force-None here) and the same fail-OPEN posture: any failure renders
    the ordinary blank picker (a UX miss on a public page), never a 500.

    Values are returned in _picker_context's raw-string echo form (the
    #175 error-re-render contract): numbers become strings, NULLs become
    "" / [], skills are filtered to SKILLS_OPTIONS (a retired option must
    not render an unknown checkbox), and the title/company lists respect
    the same caps as a POST submission.
    """
    identity = _current_identity()
    if identity is None:
        return {}
    try:
        with connection_factory() as conn:
            row = get_profile(conn, user_id=identity.user_id)
    except Exception:
        logger.warning("start prefill read failed (rendering blank picker)", exc_info=True)
        return {}
    if row is None:
        return {}
    years = row["years_of_experience"]
    comp_floor = row["comp_floor_usd"]
    return {
        "checked_titles": list(row["target_titles"] or [])[:MAX_TITLES_PER_SELECTION],
        "checked_companies": list(row["target_companies"] or [])[:MAX_COMPANIES_PER_SELECTION],
        "checked_skills": [s for s in (row["skills"] or []) if s in SKILLS_OPTIONS],
        "seniority_level": row["seniority_level"] or "",
        "years_of_experience": format(years, "g") if years is not None else "",
        "comp_floor_usd": str(comp_floor) if comp_floor is not None else "",
        "workplace_type": _WORKPLACE_DB_TO_FORM.get(row["workplace_type"], ""),
    }
```

- [ ] **Step 6: Wire the prefill into `start()`.** Replace the section of `start()` from `q = (request.args.get("q") or "").strip()` through the `context = _picker_context(...)` call (currently lines ~563–573; the pending-picker/HX-Request lines above and the two render lines below stay unchanged) with:

```python
    q = (request.args.get("q") or "").strip()
    raw_titles = request.args.getlist("titles")
    raw_companies = request.args.getlist("companies")
    notice = _too_many_selected_message(
        "titles", len(raw_titles), MAX_TITLES_PER_SELECTION
    ) or _too_many_selected_message("companies", len(raw_companies), MAX_COMPANIES_PER_SELECTION)
    checked_titles = raw_titles[:MAX_TITLES_PER_SELECTION]
    checked_companies = raw_companies[:MAX_COMPANIES_PER_SELECTION]
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

- [ ] **Step 7: Switch `/preview` to `build_entry`.** In `preview()` (~line 812), replace:

```python
    entries = [{"row": row, "chips": why_chips(row, selections)} for row in rows]
```

with:

```python
    # build_entry, not an inline dict (spec §1 riding fix): /preview now
    # renders the same entry shape as / and /demo, so the chip cap and
    # salary_display reach all three surfaces from one composer. The
    # anonymous list_feed_postings branch selects NULL::boolean AS
    # saved/applied — exactly what build_entry's coercion expects — and
    # preview.html still withholds show_actions, so no controls render.
    entries = [build_entry(row, selections) for row in rows]
```

- [ ] **Step 8: Run the task tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_start_prefill.py tests/host/test_onboarding.py`
Expected: PASS

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_preview.py`
Expected: the ONLY failures (if `POSTGRES_ADMIN_DSN` is set) are the `"salary listed"` assertions in Task 6's fallout table — anything else is yours to fix.

- [ ] **Step 9: Commit**

```bash
git add jobcannon/web/onboarding.py tests/host/test_start_prefill.py
git commit -m "feat: prefill /start from stored profile, unify preview entries" -- jobcannon/web/onboarding.py tests/host/test_start_prefill.py
```

---

### Task 6: Wave-1 integration gate (single agent, runs after Tasks 1–5 all report)

**Files:** the prescribed fallout files below, plus anything the full suite reveals that Wave 1 caused. Never revert sibling work; smallest correct fix wins.

- [ ] **Step 1: Run the FULL suite in the background** (never a silent foreground call):

```bash
uv run --no-sync --active pytest -q --tb=short 2>&1 | tee wave1-pytest.log; echo "exit=$?"
```

Compare against `baseline-pytest.log` (Task 0) — like-with-like on host-test skips.

- [ ] **Step 2: Apply the prescribed fallout table.** Task 1 deleted the `"salary listed"` chip; these assertions (line numbers pre-wave — match by content) flip from presence to absence, pinning the deletion:

| File | Pre-wave line | Change |
|---|---|---|
| `tests/host/test_demo_feed.py` | 145 | `assert "salary listed" in html` → `assert "salary listed" not in html` |
| `tests/host/test_day1_stranger_e2e.py` | 179 | `assert "salary listed" in preview_html` → `assert "salary listed" not in preview_html` |
| `tests/host/test_empty_states.py` | 192 | `assert "salary listed" in html` → `assert "salary listed" not in html` |
| `tests/host/test_empty_states.py` | 232 | `assert "salary listed" in html` → `assert "salary listed" not in html` |
| `tests/host/test_preview.py` | 575 | `assert "salary listed" in html` → `assert "salary listed" not in html` |

Positive control after editing: `grep -rn "salary listed" tests/ jobcannon/` must show ONLY negative assertions (`not in`) and zero production-code hits.

- [ ] **Step 3: Triage remaining failures.** A failure is yours to fix only if a Wave-1 task caused it (chip list shape, preview entry shape, new template scans, blueprint registration). A failure also present in `baseline-pytest.log` is pre-existing: report it, don't fix it. If a fix requires editing a Wave-1 production file, make it minimal and explain it in the commit body.

- [ ] **Step 4: Lint the whole tree**

Run: `uv run --no-sync ruff check .` and `uv run --no-sync ruff format --check .`
Fix any findings in Wave-1 files.

- [ ] **Step 5: Re-run the full suite to green** (same background+tee form), then commit:

```bash
git add tests/host/test_demo_feed.py tests/host/test_day1_stranger_e2e.py tests/host/test_empty_states.py tests/host/test_preview.py
git commit -m "test: pin salary-listed chip deletion after wave 1" -- tests/host/test_demo_feed.py tests/host/test_day1_stranger_e2e.py tests/host/test_empty_states.py tests/host/test_preview.py
```

(Additional files touched in Step 3 get their own scoped `fix:`/`test:` commits.) Report: pass/fail/skip counts vs baseline, every file touched, and any pre-existing failures left standing.

---

## Wave 2 (Tasks 7–9 run in parallel after the Task 6 gate reports green)

### Task 7: Shell — wordmark link, nav trim, icon links

**Files:**
- Modify: `jobcannon/web/templates/base.html`
- Modify: `tests/host/test_auth_nav.py`
- Modify: `tests/host/test_touch_targets.py` (ONLY the sabotage fixture, `test_sabotage_a_real_template_site_and_confirm_the_guard_fails`)

**Interfaces:**
- Consumes: Task 4's static filenames (`favicon.svg`, `apple-touch-icon.png`), Task 3's `a.jc-wordmark` styling, the existing `visitor_is_authed` context value (issue #205 semantics: real identity even on PUBLIC_PATHS pages, unlike `g.clerk_user`).
- Produces: `data-build-feed-nav-link` marker (pinned by this task's tests), wordmark-as-`<a>` (Task 9's templates don't care; nothing else consumes it).

- [ ] **Step 1: Update the tests first.** In `tests/host/test_auth_nav.py`, add two tests beside the existing authed/anon nav tests, mirroring the file's own `_app`/identity helper setup exactly (same `HostConfig`/`VERIFY_REQUEST` pattern the neighboring tests use — copy their two setup lines verbatim):

```python
def test_wordmark_links_home_and_nav_trims_for_authed_visitor():
    # Same setup as the authed /privacy test above this one.
    html = client.get("/privacy").get_data(as_text=True)
    assert '<a href="/" class="jc-wordmark' in html
    assert "data-build-feed-nav-link" not in html
    assert ">Feed</a>" not in html  # old nav links are gone, not just hidden
    assert ">Demo</a>" not in html


def test_wordmark_links_demo_and_build_feed_shows_for_anon():
    # Same setup as the anonymous /privacy test above this one.
    html = client.get("/privacy").get_data(as_text=True)
    assert '<a href="/demo" class="jc-wordmark' in html
    assert "data-build-feed-nav-link" in html
    assert 'href="/start"' in html
    assert ">Feed</a>" not in html
    assert ">Demo</a>" not in html
```

In `tests/host/test_touch_targets.py`, the sabotage fixture (`test_sabotage_a_real_template_site_and_confirm_the_guard_fails`, ~lines 391–432) pins base.html's Feed link SOURCE verbatim and will fail the moment that link is deleted. Retarget it to the new link — four edits:

1. `marker = '<a href="/" class="jc-nav-link {{ touch_target() }}">Feed</a>'` → `marker = '<a href="/start" class="jc-nav-link {{ touch_target() }}" data-build-feed-nav-link>Build your feed</a>'`
2. `literal = '<a href="/" class="jc-nav-link jc-touch">Feed</a>'` → `literal = '<a href="/start" class="jc-nav-link jc-touch" data-build-feed-nav-link>Build your feed</a>'`
3. The two assertion messages: `"base.html's Feed link markup changed -- update this sabotage fixture"` → `"base.html's Build your feed link markup changed -- update this sabotage fixture"`, and `"expected exactly the sabotaged Feed link to carry the literal class"` → `"expected exactly the sabotaged Build your feed link to carry the literal class"`.
4. In the docstring, `(base.html's "Feed" nav link)` → `(base.html's "Build your feed" nav link)`.

The `_class_value(attrs) == "jc-nav-link jc-touch"` filter and `len(feed_cases) == 1` stay as-is: the scan is over template SOURCE, where every healthy link still carries the un-rendered `{{ touch_target() }}` marker, so only the sabotaged link ever matches the literal class.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_auth_nav.py tests/host/test_touch_targets.py`
Expected: the two new auth-nav tests FAIL (wordmark is still a `<span>`; Feed/Demo links still render); the sabotage test FAILS with "base.html's Build your feed link markup changed" (marker not yet in the template).

- [ ] **Step 3: Edit `base.html` — head.** After the `jc.css` stylesheet link (line ~14), insert:

```html
  <link rel="icon" type="image/svg+xml" href="{{ url_for('static', filename='favicon.svg') }}">
  <link rel="apple-touch-icon" href="{{ url_for('static', filename='apple-touch-icon.png') }}">
```

(`url_for('static', …)` — never a literal path — keeps `tests/host/test_static_assets.py` deriving both assets automatically and keeps the third-party-host scan clean.)

- [ ] **Step 4: Edit `base.html` — header.** Replace exactly these four lines (~84–87):

```html
    <span class="jc-wordmark">Job Cannon</span>
    <nav class="jc-nav">
      <a href="/" class="jc-nav-link {{ touch_target() }}">Feed</a>
      <a href="/demo" class="jc-nav-link {{ touch_target() }}">Demo</a>
```

with:

```html
    {# Identity-aware wordmark (spec §4): the one route back to the feed
       now that the Feed/Demo nav links are gone. Gates on
       visitor_is_authed, NOT g.clerk_user (issue #205; see the
       My-postings comment below): g.clerk_user is force-None on every
       PUBLIC_PATHS render, so a bare g.clerk_user check would send a
       signed-in visitor on /demo, /privacy, or /terms to /demo instead
       of their own feed. #}
    <a href="{{ '/' if visitor_is_authed else '/demo' }}" class="jc-wordmark {{ touch_target() }}">Job Cannon</a>
    <nav class="jc-nav">
      {% if not visitor_is_authed %}
      {# Signed-out primary action (spec §4). The old Feed link only
         401-bounced anonymous visitors, and the separate Demo link is
         redundant once the wordmark above covers /demo. #}
      <a href="/start" class="jc-nav-link {{ touch_target() }}" data-build-feed-nav-link>Build your feed</a>
      {% endif %}
```

The `{% if visitor_is_authed %}` My-postings block that follows (with its #180/#205 comment) stays byte-identical, as does the `data-auth-nav` sign-in/sign-up cluster below it.

- [ ] **Step 5: Run the task tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_auth_nav.py tests/host/test_touch_targets.py tests/host/test_static_assets.py`
Expected: PASS — auth-nav (old tests untouched and still green: the My-postings and sign-in/up assertions never pinned Feed/Demo), touch-targets (new wordmark and Build-your-feed links both carry `{{ touch_target() }}`), static-assets (the two new `url_for('static', …)` references are auto-discovered and must serve 200 with no Set-Cookie — this is what proves Task 4's files actually exist on disk).

- [ ] **Step 6: Commit**

```bash
git add jobcannon/web/templates/base.html tests/host/test_auth_nav.py tests/host/test_touch_targets.py
git commit -m "feat: identity-aware wordmark link, trim nav, page icons" -- jobcannon/web/templates/base.html tests/host/test_auth_nav.py tests/host/test_touch_targets.py
```

---

### Task 8: Row template — emphasis tiers, chip dicts, expand button

**Files:**
- Modify: `jobcannon/web/feed_entries.py`
- Modify: `jobcannon/web/templates/_posting_row.html`
- Modify: `tests/host/test_feed_entries.py`
- Modify: `tests/host/test_feed_page.py` (markup-pin reconciliation only)

**Interfaces:**
- Consumes: Task 1's `chip_kinds(row, selections_or_profile) -> dict[str, str | None]` (keys exactly `"overlap"`, `"freshness"`, `"seniority"`, `"jd_quality"`) and `build_entry`'s `salary_display` / `display_location` / `show_workplace_badge` keys; Task 2's `posting_detail.detail` endpoint; Task 3's `.jc-chip--why`, `.jc-row > .jc-stack`, and `[data-posting-detail]` CSS.
- Produces: `select_chips(kinds) -> list[dict]` (each `{"label": str, "highlight": bool}`) — `entry.chips` becomes this shape for ALL surfaces at once (feed/demo via `build_entry` directly; /preview via Task 5's switch to `build_entry`; the actions.py re-render fragment via `_fetch_entry -> build_entry`). This task changes the producer and the one consuming template atomically, so no mid-wave mixed-shape state exists.

- [ ] **Step 1: Write the failing tests.** Append to `tests/host/test_feed_entries.py` (add `select_chips` to the existing `feed_entries` import):

```python
def test_select_chips_priority_and_cap():
    kinds = {
        "jd_quality": "detailed job description",
        "seniority": "senior role",
        "freshness": "posted in the last week",
        "overlap": "title matches your selections: python",
    }
    chips = select_chips(kinds)
    assert [c["label"] for c in chips] == [
        "title matches your selections: python",
        "posted in the last week",
        "senior role",
    ]  # priority order regardless of dict order; jd_quality capped off
    assert [c["highlight"] for c in chips] == [True, False, False]


def test_select_chips_freshness_leads_when_no_overlap():
    chips = select_chips({"freshness": "posted in the last week", "seniority": "senior role"})
    assert chips[0] == {"label": "posted in the last week", "highlight": True}
    assert chips[1]["highlight"] is False


def test_select_chips_never_highlights_boilerplate_kinds():
    # Even when seniority/jd_quality lead, green stays off (spec §2).
    chips = select_chips({"seniority": "senior role", "jd_quality": "detailed job description"})
    assert [c["highlight"] for c in chips] == [False, False]


def test_select_chips_skips_none_and_handles_empty():
    assert select_chips({}) == []
    assert select_chips({"overlap": None, "freshness": "posted in the last week"}) == [
        {"label": "posted in the last week", "highlight": True}
    ]


def test_build_entry_chips_are_selected_dicts():
    row = _row(title="Senior Python Engineer")  # this file's existing row helper
    entry = build_entry(row, {"titles": ["Python Engineer"]})
    assert entry["chips"], "expected at least the overlap chip"
    for chip in entry["chips"]:
        assert set(chip) == {"label", "highlight"}
    assert sum(1 for chip in entry["chips"] if chip["highlight"]) <= 1
    assert entry["chips"][0]["label"].startswith("title matches")
```

(If this file's Task-1 row-builder helper has a different name than `_row`, use that name — do not add a second row builder.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_entries.py`
Expected: FAIL — `ImportError: cannot import name 'select_chips'`

- [ ] **Step 3: Edit `jobcannon/web/feed_entries.py`.**

1. Import swap: `from jobcannon.web.why import why_chips` → `from jobcannon.web.why import chip_kinds`. Add `from collections.abc import Mapping` to the imports.
2. Add below `dedupe_location`:

```python
# Priority is a total order (spec §1: overlap > freshness > seniority >
# jd_quality — ties impossible), so selection is a stable slice, and the
# green honesty accent can only ever land on the single top chip.
_CHIP_PRIORITY = ("overlap", "freshness", "seniority", "jd_quality")
_HIGHLIGHT_KINDS = frozenset({"overlap", "freshness"})
_CHIP_CAP = 3


def select_chips(kinds: Mapping[str, str | None]) -> list[dict[str, object]]:
    """Cap and prioritize chip_kinds output for rendering (spec §1/§2).
    highlight=True (-> .jc-chip--why, the row's one green accent) goes to
    at most the FIRST selected chip, and only when its kind is overlap or
    freshness — seniority/JD boilerplate never earns green even when it
    happens to lead."""
    ordered = [(kind, kinds.get(kind)) for kind in _CHIP_PRIORITY if kinds.get(kind)]
    return [
        {"label": label, "highlight": index == 0 and kind in _HIGHLIGHT_KINDS}
        for index, (kind, label) in enumerate(ordered[:_CHIP_CAP])
    ]
```

3. In `build_entry`, change `"chips": why_chips(row, profile_or_selections),` → `"chips": select_chips(chip_kinds(row, profile_or_selections)),` and update the function docstring's chips sentence to say chips are `select_chips` dicts (`label`/`highlight`), capped at 3.

- [ ] **Step 4: Run to verify pass**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_entries.py`
Expected: PASS

- [ ] **Step 5: Rewrite `_posting_row.html`'s header comment and top section.** Replace the opening comment block (lines 1–33) with:

```html
{# One posting row. Included once per entry by _feed_list.html, which is
   shared by /preview, /, and /demo — all three now pass build_entry
   entries (jobcannon/web/feed_entries.py): row + salary_display +
   display_location + show_workplace_badge + chips, where chips is
   feed_entries.select_chips output (dicts of label/highlight, capped at
   3, at most ONE highlight=True carrying the .jc-chip--why green). The
   save/dismiss/apply controls below render only when the caller passes
   show_actions=True (the authenticated feed's own render, and the
   fragment jobcannon/web/actions.py re-renders after a mutation) —
   /preview and /demo withhold it. Save, dismiss, and undo-apply (#177)
   target THIS row's own `data-posting-row` root with
   hx-swap="outerHTML" (never 204 — jobcannon/web/actions.py's fragment
   routes always return 200). Apply is the one control that does NOT use
   hx-post/hx-swap — see the comment at its markup below for why a real
   outbound <a href> cannot also carry an htmx AJAX trigger on the same
   click, and for how its success path applies that same fragment as a
   manual (htmx-processed) outerHTML swap instead. An outerHTML re-render
   also collapses the expanded-detail slot at the bottom (the fresh
   fragment carries it empty) — an accepted interaction per the spec.

   The "signals still computing" marker below is keyed on
   entry.row.structural_axes being NULL — a real, transient state (the
   structural-axes batch caps at 500 rows per scan tick) rather than on
   entry.chips being empty: jobcannon/web/why.py's chip_kinds returns the
   title-overlap chip independently of structural_axes, so a row can have
   real chips AND still be missing its axis-derived signals. Keying on
   the actual NULL column means the axis absence is never hidden and
   never backfilled with a fabricated value. Single-sourced here (not in
   jobcannon/web/pages.py or jobcannon/web/onboarding.py) so every
   consuming route renders it identically. This marker branch emits no
   event of any kind — per-row posting_impression logging lives in the
   authed route in jobcannon/web/pages.py, never in this template. #}
```

Then replace the markup from `<article class="jc-row" data-posting-row>` down through the closing `{% endif %}` of the chips block (lines 34–66) with:

```html
<article class="jc-row" data-posting-row>
  <div class="jc-stack">
  <div class="jc-cluster">
    <h2 class="jc-row-title">{{ entry.row.title }}</h2>
    {# Primary expand control (spec §3): a real <button> for keyboard/AT;
       the whole-card click delegate in _feed_list.html is an enhancement
       that forwards to this button. Re-fetching an already-open panel is
       an idempotent innerHTML swap (documented deviation 3). #}
    <button type="button"
            class="jc-btn {{ touch_target() }}"
            hx-get="{{ url_for('posting_detail.detail', posting_id=entry.row.id) }}"
            hx-target="#posting-detail-{{ entry.row.id }}"
            hx-swap="innerHTML"
            data-action-expand>
      Details
    </button>
  </div>
  {% if entry.salary_display %}
    <p class="jc-meta"><span class="jc-meta-num">{{ entry.salary_display }}</span></p>
  {% endif %}
  <p class="jc-row-sub">
    {{ entry.row.company }}
    {%- if entry.display_location %} &middot; {{ entry.display_location }}{% endif %}
    {%- if entry.show_workplace_badge %} <span class="jc-meta-lab">{{ entry.row.workplace_type }}</span>{% endif %}
  </p>
  {% if entry.row.structural_axes is none %}
    <p class="jc-note" data-signals-pending>signals still computing for this posting</p>
  {% endif %}
  {% if entry.chips %}
    <div class="jc-why">
      <span class="lj-label">Highlights</span>
      <ul class="jc-chips" data-why-chips>
        {% for chip in entry.chips %}
          <li class="jc-chip{% if chip.highlight %} jc-chip--why{% endif %}">{{ chip.label }}</li>
        {% endfor %}
      </ul>
    </div>
  {% endif %}
```

This deletes the old `jc-note` workplace badge beside the title and the raw `salary_min`/`salary_max` block. Everything from `{% if show_actions %}` through the end of the signup-CTA block (lines 67–199) stays **byte-identical** — do not retype the Apply block. Finally, replace the closing two lines:

```html
  </div>
</article>
```

with:

```html
  </div>
  {# Expand slot (spec §3): OUTSIDE the .jc-stack grid — an empty grid
     child would still cost an 8px gap — and persistent, so htmx has a
     stable target. The card-click delegate (_feed_list.html) and the
     fragment's own Collapse button both clear it with replaceChildren();
     [data-posting-detail]:not(:empty) in jc.css owns its spacing. #}
  <div id="posting-detail-{{ entry.row.id }}" data-posting-detail></div>
</article>
```

- [ ] **Step 6: Reconcile `tests/host/test_feed_page.py`.**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_page.py`

Any failure here must be an assertion pinning the OLD row markup (raw salary numbers, the old `jc-note` badge placement, flat-string chips). Update each pin to the new markup, deriving expected salary strings from that file's own seed values via the Task-1 formatter rules (e.g. a seed of `salary_min=150000, salary_max=200000, USD, annual` renders `$150k–200k/yr` — en dash `\u2013`, `$` glued to the first amount only). Do NOT weaken an assertion to a mere existence check; swap the pinned string. If a failure is not markup-shaped, it's a real defect in this task — fix it here.

- [ ] **Step 7: Run the design + template closure tests** (the `.jc-chip--why` / `jc-meta` usage must be inside the closure Task 3 established)

Run: `uv run --no-sync --active pytest -q --tb=short tests/test_design_css.py tests/test_design_templates.py tests/host/test_feed_entries.py tests/host/test_feed_page.py`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add jobcannon/web/feed_entries.py jobcannon/web/templates/_posting_row.html tests/host/test_feed_entries.py tests/host/test_feed_page.py
git commit -m "feat: three-tier posting row with capped chips and expander" -- jobcannon/web/feed_entries.py jobcannon/web/templates/_posting_row.html tests/host/test_feed_entries.py tests/host/test_feed_page.py
```

---

### Task 9: Feed chrome — masthead, sort-select removal, card-click delegate

**Files:**
- Modify: `jobcannon/web/templates/_feed_list.html`
- Modify: `jobcannon/web/templates/feed.html`
- Modify: `jobcannon/web/templates/demo.html`
- Modify: `jobcannon/web/pages.py`
- Create: `tests/host/test_feed_list_template.py`

**Interfaces:**
- Consumes: Task 3's `.jc-masthead`; Task 8's `data-action-expand` button and `data-posting-detail` slot (the delegate only queries for them — rows without them are simply inert, so this task cannot break if it lands before Task 8 within the wave).
- Produces: nothing later tasks consume. Backend `?sort=` parsing (`pages.py`'s `_parse_feed_filters` and `_feed._SORTS`) is deliberately KEPT — only the UI select and its `sort_tokens` context plumbing go.

- [ ] **Step 1: Write the failing tests** — create `tests/host/test_feed_list_template.py`:

```python
"""Template-source pins for _feed_list.html (spec §3 click delegate + §4
sort-select removal). Reads the template file directly — the same
source-inspection style tests/host/test_touch_targets.py uses — so it needs
no app and no DB, and cannot collide with test_feed_page.py's rendered-HTML
ownership (Task 8's file)."""

from pathlib import Path

_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "jobcannon" / "web" / "templates" / "_feed_list.html"
).read_text(encoding="utf-8")


def test_sort_select_is_gone():
    assert 'name="sort"' not in _TEMPLATE
    assert "sort_tokens" not in _TEMPLATE


def test_expand_delegate_binds_once_at_document_level():
    # Must survive #feed-content outerHTML swaps and Load-more appends:
    # bound on document, guarded by a window flag so re-included fragments
    # never double-bind.
    assert "window.jcExpandBound" in _TEMPLATE
    assert "document.addEventListener('click'" in _TEMPLATE


def test_expand_delegate_ignores_interactive_targets():
    assert "[data-posting-actions]" in _TEMPLATE
    assert "[data-posting-detail]" in _TEMPLATE
    assert "getSelection" in _TEMPLATE
```

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_list_template.py`
Expected: FAIL — `name="sort"` and `sort_tokens` are still present; the delegate doesn't exist yet.

- [ ] **Step 2: Edit `_feed_list.html`.** Three changes:

1. Delete the sort select (lines ~53–57):

```html
    <select name="sort" class="jc-input {{ touch_target() }}">
      {% for token in sort_tokens %}
        <option value="{{ token }}" {% if token == filters.sort %}selected{% endif %}>{{ token }}</option>
      {% endfor %}
    </select>
```

(The workplace-type select and Apply-filters button stay.)

2. Update the header comment: in the line `jobcannon/web/pages.py's `_parse_feed_filters` — plus the `sort_tokens` / `workplace_types` option lists it needs)`, drop `` `sort_tokens` / `` (only `workplace_types` remains a template need; note that `?sort=` stays server-parsed for URL compatibility, the select was removed while only one token exists — spec §4). Also update line 2's entry-shape description: entries are `build_entry` dicts (`jobcannon/web/feed_entries.py`) whose `chips` are `select_chips` label/highlight dicts, not `why_chips(...) list[str]`.

3. Append at the very bottom of the file:

```html
{# Card-click expand delegate (spec §3): the whole card forwards to the
   row's real [data-action-expand] button (keyboard/AT parity for free) —
   unless the click landed on an interactive element, inside the expanded
   panel, or amid a text selection. Bound ONCE on document with a window
   guard: this partial is re-included by every #feed-content outerHTML
   swap (filters, clear-selection) and by Load-more appends, so an
   element-scoped or unguarded binding would stack duplicate listeners. #}
<script>
  (function () {
    if (window.jcExpandBound) { return; }
    window.jcExpandBound = true;
    document.addEventListener('click', function (evt) {
      var row = evt.target.closest('[data-posting-row]');
      if (!row) { return; }
      if (evt.target.closest('a, button, form, input, select, label, [data-posting-actions], [data-posting-detail]')) { return; }
      var selection = window.getSelection && window.getSelection();
      if (selection && String(selection).length > 0) { return; }
      var slot = row.querySelector('[data-posting-detail]');
      var button = row.querySelector('[data-action-expand]');
      if (!slot || !button) { return; }
      if (slot.childElementCount > 0) { slot.replaceChildren(); return; }
      button.click();
    });
  })();
</script>
```

- [ ] **Step 3: Wrap the mastheads.** In `feed.html`, wrap both branch headers in `<div class="jc-masthead">` … `</div>` (indent children one level):

Branch 1 — the not-wired branch (h1 + accent rule + lede, currently lines 11–16): wrap `<h1 class="jc-title">Your feed isn't wired up yet</h1>`, the `<svg class="jc-accent-rule" …>` line, and the `<p class="jc-lede" data-no-selections>…</p>` block together.

Branch 2 — the populated branch (lines 25–33): wrap `<h1 class="jc-title">Your feed</h1>`, the `<svg class="jc-accent-rule" …>` line, and the whole `<p class="lj-label" data-ordering-label>…</p>` block together.

In `demo.html`: wrap the guest-not-seeded group (h1 + rule + lede, lines ~30–32) and the populated group (h1 + rule + stats lede, lines ~34–41) the same way. The populated branch's `data-ordering-label` block (lines ~64–70) stays OUTSIDE any masthead — the profile card sits between it and the h1, so it isn't part of that visual group.

- [ ] **Step 4: Drop the `sort_tokens` context plumbing.** In `jobcannon/web/pages.py`, delete the line `sort_tokens=sorted(_feed._SORTS),` at BOTH call sites (~line 595, the feed render; ~line 686, `clear_selection`'s HX branch). Do not touch `_parse_feed_filters` or `_feed._SORTS` — `?sort=` URLs keep validating.

Positive control: `grep -rn "sort_tokens" jobcannon/ tests/` must return zero hits (control for the grep itself: `grep -rn "workplace_types" jobcannon/` still returns the two pages.py call sites and the template loop).

- [ ] **Step 5: Run the task tests**

Run: `uv run --no-sync --active pytest -q --tb=short tests/host/test_feed_list_template.py tests/test_design_templates.py`
Expected: PASS (jc-masthead is in Task 3's closure; the delegate script introduces no jc-*/lj-* class)

- [ ] **Step 6: Commit**

```bash
git add jobcannon/web/templates/_feed_list.html jobcannon/web/templates/feed.html jobcannon/web/templates/demo.html jobcannon/web/pages.py tests/host/test_feed_list_template.py
git commit -m "feat: masthead grouping, drop sort select, card-click expand" -- jobcannon/web/templates/_feed_list.html jobcannon/web/templates/feed.html jobcannon/web/templates/demo.html jobcannon/web/pages.py tests/host/test_feed_list_template.py
```

---

### Task 10: Wave-2 gate + `why_chips` cleanup (single agent, after Tasks 7–9 all report)

**Files:** `jobcannon/web/why.py`, `tests/host/test_why.py`, comment-only touches in `jobcannon/web/actions.py` / `jobcannon/web/pages.py`, plus anything the full suite reveals that Wave 2 caused.

- [ ] **Step 1: Full suite, background + tee:**

```bash
uv run --no-sync --active pytest -q --tb=short 2>&1 | tee wave2-pytest.log; echo "exit=$?"
```

Triage against `baseline-pytest.log` exactly as Task 6 did: Wave-2-caused failures are yours; pre-existing ones get reported, not fixed.

- [ ] **Step 2: Delete the `why_chips` compatibility wrapper.** Both of its consumers are gone (Task 5 moved onboarding to `build_entry`; Task 8 moved `feed_entries` to `chip_kinds`):

1. In `jobcannon/web/why.py`, delete the `why_chips` wrapper function Task 1 left behind, and update the module docstring's header line (`why_chips —` → `chip_kinds —`) plus any remaining `why_chips` mentions in it.
2. In `tests/host/test_why.py`, delete `test_why_chips_wrapper_preserves_legacy_order` and convert every remaining `why_chips`-based test to `chip_kinds`: `chips = why_chips(row, sel)` + `assert X in chips` becomes `kinds = chip_kinds(row, sel)` + `assert X in kinds.values()`; ordering assertions become key assertions (e.g. `kinds["freshness"] == …`); the two `"salary listed" not in` assertions become the structural pin `assert set(chip_kinds(row, sel)) == {"overlap", "freshness", "seniority", "jd_quality"}` (the salary kind cannot exist — no key for it). Remove `why_chips` from the import line.
3. Comment-only sweeps: `jobcannon/web/actions.py` (~line 142) and `jobcannon/web/pages.py` (~line 22) mention `why_chips` in prose — update each to `feed_entries.build_entry` / `why.chip_kinds` as fits the sentence. Verify Task 5 already fixed `onboarding.py`'s docstring mention (~line 72) and Tasks 8/9 fixed the template comments.

Positive control: `grep -rn "why_chips" jobcannon/ tests/ docs/design/` → ZERO hits (control for the grep: `grep -rln "chip_kinds" jobcannon/ tests/` is non-empty).

- [ ] **Step 3: Lint + full suite to green**

Run: `uv run --no-sync ruff check .` and `uv run --no-sync ruff format --check .`, then the full suite again (background + tee). Expected: green, modulo baseline pre-existing failures.

- [ ] **Step 4: Commit**

```bash
git add jobcannon/web/why.py tests/host/test_why.py jobcannon/web/actions.py jobcannon/web/pages.py
git commit -m "refactor: drop why_chips compat wrapper after migration" -- jobcannon/web/why.py tests/host/test_why.py jobcannon/web/actions.py jobcannon/web/pages.py
```

(Any other reconciliation files get their own scoped commits.) Report: counts vs baseline, files touched, pre-existing failures left standing.

---

## Execution Strategy (orchestrator playbook)

This plan is built for a Workflow run: Wave 1's five tasks are file-disjoint by the ownership map, as are Wave 2's three; each task carries its own test cycle; the only serialization points are the two gate agents. 18 agents total (8 implement + 8 verify + 2 gates) — above the medium size guideline, per the owner's explicit directive to maximally leverage subagent fleets.

**Before dispatch (orchestrator, NOT inside the workflow — stall-retries must never repeat setup):**

1. Execute **Task 0** in this session: `git checkout -b feat/feed-shell-redesign` from the `docs/feed-shell-redesign-spec` tip, baseline full-suite run tee'd to `baseline-pytest.log`, `uv run --no-sync ruff check .`, record whether `POSTGRES_ADMIN_DSN` is set. Do not commit the log.
2. Heed the workflow lint hook's FLEET-CONTENTION warning if it fires — dispatch between fleet waves rather than into a hot org-rate window.
3. On stall-kills, run `python ~/.claude/scripts/classify-workflow-stall.py <run-dir>` BEFORE relaunching; API_SILENCE means throttling (switch to plain Agent-tool subagents or wait), MID_COMMAND means fix the prompt. Resume with `resumeFromRunId` — completed agent() calls replay from cache.

**The workflow script:**

```javascript
export const meta = {
  name: 'feed-shell-redesign',
  description: 'Two-wave parallel implementation of the feed & shell redesign plan: file-disjoint implementer/verifier pairs plus two integration gates',
  phases: [
    { title: 'Wave 1', detail: 'Tasks 1-5: salary/chip data layer, detail route, CSS, icons, /start prefill' },
    { title: 'Gate 1', detail: 'Task 6: full suite + prescribed fallout' },
    { title: 'Wave 2', detail: 'Tasks 7-9: shell nav, row template, feed chrome' },
    { title: 'Gate 2', detail: 'Task 10: full suite + why_chips cleanup' },
  ],
}

const REPO = 'C:/Users/senki/repos/jobcannon'
const PLAN = 'docs/superpowers/plans/2026-08-30-feed-shell-redesign.md'
const BRANCH = 'feat/feed-shell-redesign'

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
  may run over 2 minutes: run in the background with output tee'd to a log
  file, then read the file and echo the exit code — never sit silent.
- Commits: pathspec-limited exactly as the plan's commit steps show. On an
  index.lock error wait 2 seconds and retry, at most 5 times.
- Return JSON only; every string field < 1500 chars — if schema validation
  fails, SHORTEN, never pad.`

const WAVE1 = [
  { label: 'task-1', phase: 'Wave 1', heading: 'Task 1', title: 'salary formatter, chip split, entry composer', tests: 'tests/host/test_salary_fmt.py tests/host/test_why.py tests/host/test_feed_entries.py' },
  { label: 'task-2', phase: 'Wave 1', heading: 'Task 2', title: 'posting detail fragment route', tests: 'tests/host/test_posting_detail.py' },
  { label: 'task-3', phase: 'Wave 1', heading: 'Task 3', title: 'jc.css density + green discipline', tests: 'tests/test_design_css.py tests/test_design_templates.py tests/test_design_tokens.py' },
  { label: 'task-4', phase: 'Wave 1', heading: 'Task 4', title: 'favicon + touch icon assets', tests: 'tests/test_design_css.py tests/test_design_templates.py' },
  { label: 'task-5', phase: 'Wave 1', heading: 'Task 5', title: '/start prefill + preview build_entry', tests: 'tests/host/test_start_prefill.py tests/host/test_onboarding.py' },
]

const WAVE2 = [
  { label: 'task-7', phase: 'Wave 2', heading: 'Task 7', title: 'shell: wordmark link, nav trim, icon links', tests: 'tests/host/test_auth_nav.py tests/host/test_touch_targets.py tests/host/test_static_assets.py' },
  { label: 'task-8', phase: 'Wave 2', heading: 'Task 8', title: 'row template: tiers, chip dicts, expander', tests: 'tests/host/test_feed_entries.py tests/host/test_feed_page.py' },
  { label: 'task-9', phase: 'Wave 2', heading: 'Task 9', title: 'feed chrome: masthead, sort removal, delegate', tests: 'tests/host/test_feed_list_template.py' },
]

const implement = (t) => agent(
  `${COMMON}

Implement "${t.heading}: ${t.title}" — the plan section headed "${t.heading}".
Follow its steps in order, TDD included. Your mid-wave green bar is ONLY:
${t.tests} plus tests/test_design_css.py tests/test_design_templates.py — the
FULL suite belongs to the gate agent, and expected cross-task fallout is
prescribed in the plan's gate tasks, NOT yours to fix.`,
  { label: t.label, phase: t.phase, model: 'sonnet', schema: RESULT_SCHEMA },
)

const verify = (t, r) => agent(
  `${COMMON}

Verify the completed "${t.heading}: ${t.title}" against its plan section on
the L1-L4 ladder, emphasizing L3 (WIRED: every new function/route/class is
imported AND called by the real consumer the plan names — grep for the call
site; an import alone is not wiring) and L4 (run ${t.tests} YOURSELF — never
trust the implementer's claim). Implementer report: ${JSON.stringify(r)}.
Confirm its commits exist in git log. Small in-scope defects in THIS task's
files: fix and pathspec-commit with a fix:/test: message. Anything larger, or
in sibling-owned files: record in "notes" only.`,
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

const gate = (heading, phase, waveSummary) => agent(
  `${COMMON}

You are the integration gate: execute the plan section "${heading}" in full
(background full-suite run tee'd to a log, the prescribed reconciliation
steps, triage against baseline-pytest.log, ruff, scoped commits). Wave
reports: ${JSON.stringify(waveSummary)}. Act on any "notes" naming real
cross-task defects; leave pre-existing baseline failures standing but report
them.`,
  { label: heading.toLowerCase().replace(/[^a-z0-9]+/g, '-'), phase, model: 'sonnet', schema: RESULT_SCHEMA },
)

const wave1 = await runWave(WAVE1)
const w1 = summarize(wave1)
if (w1.some((x) => x.verdict === 'blocked')) {
  return { halted: 'wave 1 blocked', wave1: w1 }
}

const gate1 = await gate('Task 6', 'Gate 1', w1)
if (gate1 && gate1.blocked) {
  return { halted: 'gate 1 blocked', gate1, wave1: w1 }
}

const wave2 = await runWave(WAVE2)
const w2 = summarize(wave2)
if (w2.some((x) => x.verdict === 'blocked')) {
  return { halted: 'wave 2 blocked', wave1: w1, gate1, wave2: w2 }
}

const gate2 = await gate('Task 10', 'Gate 2', w2)
return { wave1: w1, gate1, wave2: w2, gate2 }
```

**After the workflow (Task 11 — orchestrator, outside the workflow):**

1. **Final review** — dispatch an `opus48` Agent-tool subagent (owner-preferred verdict tier) to review the full branch diff (`git diff docs/feed-shell-redesign-spec...feat/feed-shell-redesign`) against the spec (`docs/superpowers/specs/2026-08-30-feed-shell-redesign-design.md`) and the Living Journal identity rules, with explicit attention to: the ≤1-green-per-row invariant end-to-end, the `public_get` exposure of the detail route, the Apply block surviving byte-identical, and stale comments. Fix its findings (inline for small ones; a `sonnet5` subagent for mechanical batches), re-running affected tests.
2. **Verify the suite one last time** (background + tee) and `uv run --no-sync ruff check .`.
3. **STOP — owner gate.** Pushing `feat/feed-shell-redesign` and opening a PR are outward-facing actions (global rule 8). Present the branch summary (commits, test counts vs baseline, deviations exercised) and wait for explicit owner approval before any `git push` / `gh pr create`. Note for the PR stage: jobcannon PRs carry the CodeQL default-setup gate with no local equivalent — query alerts via `ref=refs/pull/<N>/head` after opening.
