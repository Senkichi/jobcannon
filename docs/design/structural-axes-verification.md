# Structural-axes threshold verification

The four structural axes (`freshness`, `seniority_clarity`, `comp_transparency`,
`jd_quality`, `jobcannon/host/structural_axes/__init__.py:25-48`) are
zero-LLM, rules-based scores computed at ingest and persisted into
`postings.structural_axes` under `structural_scoring_method = "rules_v1"`.
This document records, mechanically, what the numeric thresholds inside those
rules actually are, where the boundary behavior is now pinned by a
characterization test, and the two places where the code's live behavior
diverges from what an earlier description of it claimed.

This is a record plus a pin. It changes no scoring behavior and asserts no
opinion on whether a threshold is well-chosen.

## 1. Freshness age buckets

`score_freshness` (`jobcannon/host/structural_axes/freshness.py:50-69`):

- A row already flagged `is_stale=True`, or with `expiry_status == "expired"`,
  overrides age entirely and returns `0.1` (`:57-58`).
- Otherwise an anchor date is selected: `posted_date` is used **only** when
  `posted_date_precision` is `'exact'` or `'approximate'` (never `'proxy'`);
  any other case (including no usable `posted_date`) falls back to
  `last_seen` (`:60-64`).
- With no usable date at all (neither anchor resolves), the result is a flat
  `0.3` — a deliberate non-guess, not a bucket (`:65-66`).
- Otherwise the anchor's age in days is bucketed by `_age_bucket`
  (`:40-47`): `age_days <= 7` -> `1.0`; `<= 30` -> `0.7`; `<= 90` -> `0.4`;
  else `0.2`.

## 2. JD-quality boilerplate ratio

`_boilerplate_ratio` (`jobcannon/host/structural_axes/jd_quality.py:37-51`):
the max, over up to 5 sibling postings, of the word-5-gram Jaccard overlap
between the posting's own `jd_full` and the sibling's. Texts under 5 words
collapse to a single shingle (the whole text) rather than producing zero
shingles (`_shingles`, `jobcannon/host/structural_axes/jd_quality.py:20-27`,
specifically `:25-26`). With no siblings to compare against, the function
returns `0.0` unconditionally — no penalty, not a penalty of the wrong sign
(`:45-46`).
Siblings are the up to 5 most-recently-seen JD-bearing postings at the same
company, fetched once per pending row
(`jobcannon/host/structural_axes/__init__.py:100-104`).

## 3. JD-quality composite score

`score_jd_quality` (`jobcannon/host/structural_axes/jd_quality.py:54-64`):

```
value = round(0.4 * band + 0.4 * section + 0.2 * (1.0 - boiler), 3)
```

- `band` is `1.0` when `200 <= word_count <= 1200` (both inclusive), else
  `0.5`. The `200`/`1200` literals are `score_jd_quality`'s
  `ideal_min`/`ideal_max` default keyword arguments (`:55`); the comparison
  itself is at `:60`.
- `section` is `1.0` when
  `jobcannon.engine.jd_content_contract.has_recognizable_jd_shape` finds a
  positive JD-shape signal anywhere in the body, else `0.0`
  (`jobcannon/engine/jd_content_contract.py:403-414`). This is a presence
  check — one vocabulary hit anywhere in the text is sufficient — and it is
  the identical check the engine's own `classify_jd_content` uses en route
  to a CLEAN verdict (`jobcannon/engine/jd_content_contract.py:449`), not a
  duplicated regex.
- `boiler` is `_boilerplate_ratio` (item 2 above).

## 4. No specification pins these numbers

Neither `freshness.py` nor `jd_quality.py` carries a citation of its own to
any specification document, and no file under `docs/**` or `analyses/**` in
this repository defines the `7`/`30`/`90` day buckets, the `0.1`/`0.3` flat
values, the `200`/`1200` word-count band, or the `0.4`/`0.4`/`0.2` weighting.
Phase 1C is the first consumer of these values in this repository. They are
the implementer's tuning choice made at port time. `tests/host/test_structural_axes_boundaries.py`
now pins each boundary exactly, so any future change to a number is a
deliberate, visible test edit rather than a silent drift.

## 5. Two live-behavior divergences

- **`comp_transparency` is structured-salary presence only.**
  `score_comp_transparency` (`jobcannon/host/structural_axes/comp_transparency.py:25-34`)
  returns `True` iff `salary_min is not None or salary_max is not None`,
  method `"structured"`; `jd_full` is accepted for call-site parity but
  intentionally unused. The module docstring
  (`jobcannon/host/structural_axes/comp_transparency.py:12-19`) records that
  an earlier revision scanned `jd_full` with a salary-grammar heuristic, and
  that this was deliberately reverted after adversarial review found that a
  currency figure in JD body text is as easily a funding round, revenue
  figure, budget, or bonus as it is base pay — a free-text salary
  *attribution* problem, not merely a parsing one.
- **The freshness stale-override branch is currently unreachable.**
  `is_stale` defaults to `false` and `expiry_status` has no default
  (`jobcannon/db/migrations/m0001_initial_schema.py:74-75`), and nothing in
  the hosted path ever writes either column: `run_stale_detect_task` raises
  `NotImplementedError` unconditionally
  (`jobcannon/host/scan_tasks.py:89-93`). Every posting therefore takes the
  age-bucket path in `score_freshness`; the `0.1` override exists in code but
  has never fired against a real row in this repository.
