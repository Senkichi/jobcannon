# PORTED from tests/test_autoheal_email_capture.py @ 2dc6c820aff0a3b213e4f6c4c535097f8dd0a816 (private job-cannon). Ledger L-0562.
"""Tests for autoheal email capture: label→parser uniqueness invariant (Task 5).

Issue #658: drift detection coverage for email parsers
------------------------------------------------------
Every email parser is covered by the break-counter detection machinery:

Detection path:
1. job_finder/sources/gmail_source.py:168-176 / job_finder/sources/imap_source.py:223-231 append to extraction_records
   with canonical label (from SENDER_LABEL)
2. job_finder/web/ingestion_runner.py:127-148 _record_email_extractions drains records
3. job_finder/web/autoheal/health_monitor.py:27-142 record_extraction implements break detection
4. job_finder/web/autoheal/health_monitor.py:144-202 run_detection promotes to DEGRADED

The invariant enforced by test_label_to_parser_is_functional ensures that
no two distinct parser callables share the same label. This prevents drift
masking where one parser's healthy extractions reset the break counter for
a different parser that is actually drifting (Issue #658, Defect 1).

Structural residuals (known bounds of the instrument):
- A parser that never established a baseline (no positive-yield extractions)
  cannot flag DEGRADED — the break counter requires a baseline to compute
  consecutive_zero_yields.
- Sender-address drift (parser stops running entirely because the FROM address
  changes) freezes the counter healthy — no extractions occur, so the counter
  never increments. This is a limitation of the per-label detection model.
"""

from jobcannon.engine.email_senders import SENDERS


def test_label_to_parser_is_functional():
    """Each label must map to exactly one parser callable.

    Detection is per-LABEL, not per-parser. If two distinct parsers share a
    label, healthy extractions from one reset the break counter for the other,
    masking drift. This test enforces the SenderSpec docstring's "Canonical
    one-per-parser health label" contract (Issue #658, Defect 1).
    """
    label_to_parsers: dict[str, list] = {}
    for spec in SENDERS:
        label_to_parsers.setdefault(spec.label, []).append(spec.parser)

    # Find labels mapped to multiple distinct parsers
    violations = {
        label: parsers
        for label, parsers in label_to_parsers.items()
        if len({id(p) for p in parsers}) > 1
    }

    assert not violations, (
        f"Labels mapped to multiple distinct parsers (drift masking risk): {violations}"
    )


def test_linkedin_addresses_share_one_label():
    """LinkedIn addresses correctly collapse to one label (allowed pattern)."""
    from jobcannon.engine.email_senders import SENDER_LABEL, SENDER_PARSERS

    labels = {SENDER_LABEL[k] for k in SENDER_PARSERS if "linkedin" in k}
    assert labels == {"linkedin"}
