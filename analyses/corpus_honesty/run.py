"""End-to-end runner: env-var DB -> extract -> classify -> committed aggregates."""

from pathlib import Path

from analyses.common.db import open_readonly, resolve_source_db
from analyses.corpus_honesty.extract import load_exclusion_counts, load_provenance_records
from analyses.corpus_honesty.report import write_outputs
from analyses.corpus_honesty.transform import (
    LARGEST_AGGREGATOR_SOURCE,
    effective_stale_aggregates,
    provenance_aggregates,
)


def main(outdir: Path | None = None) -> Path:
    outdir = outdir or Path(__file__).parent
    con = open_readonly(resolve_source_db())
    try:
        primary_df = load_provenance_records(con)
        primary = provenance_aggregates(primary_df)
        excl_largest = provenance_aggregates(
            load_provenance_records(con, exclude_source=LARGEST_AGGREGATOR_SOURCE)
        )
        effective_stale = effective_stale_aggregates(primary_df)
        exclusions = load_exclusion_counts(con)
    finally:
        con.close()
    write_outputs(primary, excl_largest, effective_stale, exclusions, outdir)
    return outdir


if __name__ == "__main__":
    print(f"wrote {main()}")
