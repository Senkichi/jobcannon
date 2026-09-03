"""Direct unit coverage for llm_refusal_guard.looks_like_llm_refusal.

description_reformatter.py's own test suite covers the live-corpus positive
samples (via the re-exported `_looks_like_llm_refusal` alias). This file adds
direct coverage of the detector itself, including a live-DB-remediation false
positive: generic first-person ATS "Apply" button consent boilerplate ("I
understand that my employment application process...") must NOT be treated
as a refusal — it is genuine (if junky) scraped page content, not an LLM
declining to answer.
"""

from jobcannon.engine.llm_refusal_guard import looks_like_llm_refusal

_ATS_CONSENT_BOILERPLATE = (
    'By clicking the "Apply" button, I understand that my employment '
    "application process with Takeda will commence and that the information "
    "I provide in my application will be processed in line with Takeda's "
    "Privacy Notice and Terms of Use."
)

_GENUINE_JD_HEAD = (
    "We are looking for a Senior Data Scientist to join our growing team. "
    "In this role you will build models and partner with product teams."
)


class TestLooksLikeLlmRefusal:
    def test_ats_apply_consent_boilerplate_is_not_a_refusal(self):
        """Regression: 'I understand' was removed from the refusal regex after
        this exact live-DB text false-triggered it during remediation."""
        assert looks_like_llm_refusal(_ATS_CONSENT_BOILERPLATE) is False

    def test_genuine_jd_head_is_not_a_refusal(self):
        assert looks_like_llm_refusal(_GENUINE_JD_HEAD) is False

    def test_empty_and_none_like_input_is_not_a_refusal(self):
        assert looks_like_llm_refusal("") is False

    def test_detects_id_be_happy_to(self):
        assert looks_like_llm_refusal("I'd be happy to help reformat this!") is True

    def test_detects_unable_to_complete(self):
        assert looks_like_llm_refusal("Unable to complete this request.") is True

    def test_detects_please_provide_the(self):
        assert looks_like_llm_refusal("Please provide the full job description text.") is True

    def test_only_checks_head_window(self):
        """A refusal phrase far past the head window must not trigger — mirrors
        the module's documented _REFUSAL_HEAD_WINDOW discipline."""
        padding = "Responsibilities include shipping features. " * 30
        assert len(padding) > 400
        text = padding + "I'd be happy to help with that."
        assert looks_like_llm_refusal(text) is False
