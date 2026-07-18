"""Comp-transparency structural axis: does the posting disclose real pay?

True iff the posting carries a structured salary (``salary_min`` / ``salary_max``
present). Those columns are the single authoritative record of disclosed pay:
they are written ONLY by the ingest/capture layer
(``jobcannon.db._jobs.upsert_job`` via the engine's salary-capture functions,
which already run the source / JD text through ``salary_normalizer``). A posting
that discloses pay anywhere the pipeline can parse it — ATS structured field,
feed string, or JD body — has that value promoted into ``salary_min`` /
``salary_max`` at ingest.

This axis therefore READS that structured result instead of re-parsing the JD
body itself. An earlier revision scanned ``jd_full`` with a salary-grammar
heuristic; adversarial review showed regex free-text salary *attribution* is
not reliably achievable here (a currency figure in the body is as easily a
funding round, revenue figure, budget, or bonus as base pay), so that path was
removed. If JD-body-only disclosures should count, the correct fix is to
extract them into ``salary_min`` / ``salary_max`` at the capture layer (the
single salary writer), not to re-derive pay in this read-only ranking axis.
"""

from __future__ import annotations


def score_comp_transparency(
    salary_min: object, salary_max: object, jd_full: str | None = None
) -> dict:
    """True iff the row carries a structured salary. ``jd_full`` is accepted for
    call-site parity with the other axis scorers but intentionally unused — see
    the module docstring for why JD-body text is not re-parsed here."""
    return {
        "value": salary_min is not None or salary_max is not None,
        "method": "structured",
    }
