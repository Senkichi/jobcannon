"""End-to-end runner: env-var DB -> extract -> KM -> committed aggregates."""

from pathlib import Path

from analyses.common.db import open_readonly, resolve_source_db
from analyses.posting_lifespan.extract import load_exclusion_counts, load_lifespan_records
from analyses.posting_lifespan.report import write_outputs
from analyses.posting_lifespan.survival import km_by_platform


def main(outdir: Path | None = None) -> Path:
    outdir = outdir or Path(__file__).parent
    con = open_readonly(resolve_source_db())
    try:
        primary = km_by_platform(load_lifespan_records(con))
        robustness = km_by_platform(
            load_lifespan_records(con, include_inconclusive_as_censored=True)
        )
        exclusions = load_exclusion_counts(con)
    finally:
        con.close()
    write_outputs(primary, robustness, exclusions, outdir)
    return outdir


if __name__ == "__main__":
    print(f"wrote {main()}")
