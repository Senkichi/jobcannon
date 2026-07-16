# Corpus-construction honesty: aggregator-listed vs. ATS-confirmed postings

Second analysis in the Job Cannon series. Reproduce with:

    JOBCANNON_SOURCE_DB=<path-to-corpus.db> uv run python -m analyses.corpus_honesty.run

The source corpus is private (it is one person's job-search data); everything
committed here is provenance-class-level aggregates. Method, the exact
classification taxonomy (and where each label set was verified against the
private pipeline), exclusion accounting, and the SQL are in NUMBERS.md.
