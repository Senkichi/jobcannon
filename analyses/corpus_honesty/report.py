"""Write committed outputs: provenance-class aggregates only (no raw rows)."""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from analyses.corpus_honesty.extract import PROVENANCE_SQL  # noqa: E402
from analyses.corpus_honesty.transform import (  # noqa: E402
    EXPIRY_STATUSES,
    LARGEST_AGGREGATOR_SOURCE,
    headline_ratio,
)

BUCKET_ORDER = ["ats_confirmed", "direct_crawl", "email_alert_only", "aggregator_only"]

CAVEATS = f"""
- The provenance taxonomy (which source tags count as ATS-confirmed / direct
  crawl / aggregator / email-alert) is a verified snapshot of the private
  pipeline's source-tag vocabulary, not a live import - this repo is
  deliberately host-agnostic and does not depend on the private pipeline's
  code. See `extract.py`'s module docstring for the exact private-repo files
  each label set was verified against, and the exclusion-accounting rows
  below for the drift guard (an unrecognized source tag is excluded and
  counted, never silently misclassified).
- `direct_crawl` (the project's own first-party careers-page crawler) is
  reported as its own class, separate from third-party aggregators - an
  earlier exploration pass had bucketed it as "portal-like" for expedience,
  which overstated the aggregator share.
- `is_stale` and `expiry_status` measure different things by construction,
  and the primary-vs-Variant-2 ratio gap (Variant 2's ratio is far smaller)
  is a direct consequence, not noise. `is_stale` is a PASSIVE decay-clock
  flag: the pipeline's nightly detector sets it only for a job with zero
  re-sighting evidence for 14+ days (5 for a job stuck at
  expiry_status='inconclusive'), and clears it the moment the job is
  re-sighted. `expiry_status='expired'` is an ACTIVE verdict from an
  independent verification cascade that requires either a known ATS
  slug/platform or a resolvable company homepage to run at all. Gated
  aggregator sources (the largest being `portal_jooble`) often have
  neither, so those postings land at expiry_status='inconclusive' (see
  Results: aggregator_only's inconclusive_rate vs. ats_confirmed's) rather
  than ever being confirmable as 'expired' - their staleness signal comes
  almost entirely through the passive `is_stale` clock instead, which is
  exactly why the primary result concentrates so heavily in `is_stale`
  there. ATS-confirmed postings, by contrast, get resolved definitively one
  way or the other by the active cascade, so they rarely linger long enough
  to trip the passive clock. Read the primary result as "how often is a
  posting in this class going dark with nobody able to independently
  confirm it either way" - a real corpus-construction signal - not as "how
  often is a posting in this class actually gone," which Variant 2
  approximates and shows is a much smaller gap.
- Corpus is a single owner's tracked query set (tech/data-skewed), not a
  random sample of the job market - same scope caveat as every other
  analysis in this series.
- The exact percentage split across provenance classes is a function of
  which sources (IMAP senders, SerpAPI, DataForSEO, portal APIs) happen to
  be configured right now; the qualitative ranking (ATS-confirmed is far
  less often stale than aggregator-only) is the robust claim, not the exact
  digits - the Robustness section is designed to test that directly, twice
  (`{LARGEST_AGGREGATOR_SOURCE}` removed; an alternate staleness definition).
""".strip()


def _rows(aggregates: dict[str, dict], variant: str) -> list[dict]:
    rows = []
    for bucket, stats in sorted(aggregates.items()):
        row = {
            "variant": variant,
            "bucket": bucket,
            "n": stats["n"],
            "stale_n": stats["stale_n"],
            "stale_rate": stats["stale_rate"],
            "has_jd_rate": stats["has_jd_rate"],
            "scored_rate": stats["scored_rate"],
        }
        for status in EXPIRY_STATUSES:
            row[f"{status}_rate"] = stats[f"{status}_rate"]
        rows.append(row)
    return rows


def _figure(primary: dict[str, dict], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 5))
    buckets = [b for b in BUCKET_ORDER if b in primary]
    rates = [primary[b]["stale_rate"] for b in buckets]
    ns = [primary[b]["n"] for b in buckets]
    bars = ax.bar(buckets, rates, color="#4C72B0")
    for bar, n in zip(bars, ns, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"n={n:,}",
            ha="center",
            va="bottom",
            fontsize=9,
        )
    ax.set_ylabel("stale-flag rate (is_stale)")
    ax.set_title("Job-posting stale-flag rate by provenance class")
    ax.set_ylim(0, max(rates) * 1.25 if rates else 1)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _numbers_md(
    primary: dict[str, dict],
    excl_largest: dict[str, dict],
    effective_stale: dict[str, dict],
    exclusions: dict[str, int],
) -> str:
    all_n = primary["ALL"]["n"]
    ac = primary.get("ats_confirmed", {})
    ao = primary.get("aggregator_only", {})
    ratio = headline_ratio(primary)
    ratio_text = f"**{ratio:.1f}x**" if ratio is not None else "undefined (zero denominator)"

    split_lines = "\n".join(
        f"- {b}: {primary[b]['n']:,} ({primary[b]['n'] / all_n:.1%})"
        for b in BUCKET_ORDER
        if b in primary
    )

    results_table = pd.DataFrame(_rows(primary, "primary")).to_markdown(index=False)
    excl_table = pd.DataFrame(
        _rows(excl_largest, f"exclude_{LARGEST_AGGREGATOR_SOURCE}")
    ).to_markdown(index=False)
    eff_table = pd.DataFrame(_rows(effective_stale, "effective_stale_definition")).to_markdown(
        index=False
    )
    excl_ratio = headline_ratio(excl_largest)
    eff_ratio = headline_ratio(effective_stale)

    exclusion_lines = "\n".join(f"- {k}: {v:,}" for k, v in exclusions.items())

    return f"""# Corpus-construction honesty: aggregator-listed vs ATS-confirmed postings - verified numbers

All numbers below are machine-generated by `run.py`. Interpretation is written
by the owner only (founder-authorship plank).

## Headline

Of {all_n:,} classified postings:

{split_lines}

ATS-confirmed postings are flagged stale (`is_stale`) at **{ac.get("stale_rate", float("nan")):.3%}**
(n={ac.get("n", 0):,}); aggregator-only postings at **{ao.get("stale_rate", float("nan")):.3%}**
(n={ao.get("n", 0):,}) - aggregator-only postings are flagged stale {ratio_text} more often.

## Exclusion accounting

{exclusion_lines}

## Method

Every posting's `sources` JSON array is classified into exactly one
provenance class by precedence (direct ATS-scanner sighting beats
first-party crawl beats third-party aggregator beats inbox email alert);
postings with no attributable real source, or a source tag outside the
verified taxonomy, are excluded from the primary result (see Exclusion
accounting) and Method docstring in `extract.py` for the exact label sets
and their private-repo provenance. Staleness (primary metric) is the
pipeline's own `is_stale` flag; `expiry_status` (live/expired/inconclusive/
null) is reported alongside it, not folded in, for the primary result.

## Results

{results_table}

## Robustness

Variant 1 - drop `{LARGEST_AGGREGATOR_SOURCE}` (the single largest aggregator
source tag, n={excl_largest.get("aggregator_only", {}).get("n", 0):,} remaining vs.
{ao.get("n", 0):,} in the primary result) before classifying, to check the
result isn't one source's artifact. Ratio under this variant:
{f"{excl_ratio:.1f}x" if excl_ratio is not None else "undefined"}.

{excl_table}

Variant 2 - alternative staleness definition (`is_stale` OR
`expiry_status == 'expired'`), same population as the primary result. Ratio
under this variant: {f"{eff_ratio:.1f}x" if eff_ratio is not None else "undefined"}.

{eff_table}

## Caveats

{CAVEATS}

## SQL appendix

```sql
{PROVENANCE_SQL.strip()}
```
"""


def write_outputs(
    primary: dict[str, dict],
    excl_largest: dict[str, dict],
    effective_stale: dict[str, dict],
    exclusions: dict[str, int],
    outdir: Path,
) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "figures").mkdir(exist_ok=True)
    all_rows = (
        _rows(primary, "primary")
        + _rows(excl_largest, f"exclude_{LARGEST_AGGREGATOR_SOURCE}")
        + _rows(effective_stale, "effective_stale_definition")
    )
    pd.DataFrame(all_rows).to_csv(outdir / "aggregates.csv", index=False)
    _figure(primary, outdir / "figures" / "stale_rate_by_provenance.png")
    (outdir / "NUMBERS.md").write_text(
        _numbers_md(primary, excl_largest, effective_stale, exclusions),
        encoding="utf-8",
        newline="\n",
    )
