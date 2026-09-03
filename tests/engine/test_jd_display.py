"""pick_jd_display — content-quality-aware picker for the detail-view JD text.

Regression coverage for the naive "whichever field is longer" picker that
used to live inline in jobs/detail.html and jobs/_detail_content.html: a
verbose conversational refusal persisted into `description` by a
pre-llm_refusal_guard reformat pass would beat a short (or absent) `jd_full`
purely on length and get rendered as "the job description".
"""

from jobcannon.engine.jd_display import pick_jd_display

_REFUSAL = (
    "I'd be happy to reformat this job description professionally! However, "
    "the content you've provided appears to be incomplete — I only have a "
    "few fragmented sentences about qualifications, salary, and role "
    "requirements."
)
_SHORT_CLEAN_JD_FULL = "We need a data scientist. Python required."
_LONG_CLEAN_DESCRIPTION = "Build ML models. Deploy pipelines. Monitor performance in prod."


class TestPickJdDisplay:
    def test_no_job_returns_empty(self):
        assert pick_jd_display(None) == ""
        assert pick_jd_display({}) == ""

    def test_picks_longer_when_both_clean(self):
        job = {"description": _LONG_CLEAN_DESCRIPTION, "jd_full": _SHORT_CLEAN_JD_FULL}
        assert pick_jd_display(job) == _LONG_CLEAN_DESCRIPTION

    def test_picks_shorter_jd_full_over_longer_refusal_description(self):
        """The bug this fixes: a verbose refusal must never beat a short, clean jd_full."""
        job = {"description": _REFUSAL, "jd_full": _SHORT_CLEAN_JD_FULL}
        assert len(_REFUSAL) > len(_SHORT_CLEAN_JD_FULL)  # sanity: length alone would pick wrong
        assert pick_jd_display(job) == _SHORT_CLEAN_JD_FULL

    def test_refusal_description_with_no_jd_full_yields_nothing(self):
        """A refusal must never be shown, even when it's the only candidate."""
        job = {"description": _REFUSAL, "jd_full": None}
        assert pick_jd_display(job) == ""

    def test_refusal_jd_full_falls_back_to_clean_description(self):
        job = {"description": _LONG_CLEAN_DESCRIPTION, "jd_full": _REFUSAL}
        assert pick_jd_display(job) == _LONG_CLEAN_DESCRIPTION

    def test_both_corrupted_yields_nothing(self):
        job = {"description": _REFUSAL, "jd_full": _REFUSAL}
        assert pick_jd_display(job) == ""

    def test_only_description_present(self):
        job = {"description": _LONG_CLEAN_DESCRIPTION, "jd_full": None}
        assert pick_jd_display(job) == _LONG_CLEAN_DESCRIPTION

    def test_only_jd_full_present(self):
        job = {"description": None, "jd_full": _SHORT_CLEAN_JD_FULL}
        assert pick_jd_display(job) == _SHORT_CLEAN_JD_FULL
