# Posting lifespan by ATS platform

First analysis in the Job Cannon series. Reproduce with:

    JOBCANNON_SOURCE_DB=<path-to-corpus.db> uv run python -m analyses.posting_lifespan.run

The source corpus is private (it is one person's job-search data); everything
committed here is platform-level aggregates. Method, filters, exclusion
accounting, and the exact SQL are in NUMBERS.md.
