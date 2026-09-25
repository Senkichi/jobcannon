"""Overnight self-monitoring unit: the watermark-driven sampler plus the
once-daily morning audit/review/issue-filer/deadman run.

Landed in two ledger units -- L-0471 (sampler, signatures, baselines,
checkpoint packet+verdict, state; dark) and L-0387 (morning_driver,
audit_stage, review_stage, issue_filer, report, error_budget, deadman,
disagreement_baseline, model_session). Every entry point is gated behind
JC_NIGHTLY_MONITOR_ENABLED (config.py) and never raises into the host
periodic worker (jobcannon.host.tasks owns the wrappers).

Alarm-pairing convention (issue #398, the "deadman/degraded-key +
escalation-test pairing" follow-up): every ALARM-GRADE
record_scan_health call in this package -- one that passes a literal
level="ERROR" or level="WARNING", so the row lands in error_budget.py's
WARNING/ERROR digest -- must pair with a test that captures the call
through a monkeypatched record_scan_health and asserts the emitted row's
level plus its identifying field (kind= or source=). The reference
pairings are deadman.py's "deadman_report_missing" ERROR row vs
tests/host/test_nightly_deadman.py, and sampler.py's FAIL-escalation
ERROR row (source="nightly_sampler") vs tests/host/test_nightly_sampler.py;
tests/host/test_nightly_alarm_pairing.py AST-enforces the convention so a
new alarm path without a paired assertion fails the build. Forensic rows
(audit_stage.py's nightly_audit_* batch-failure records carry no level --
their signal reaches the review through audit_summary instead) and
heartbeats (morning_driver.py's level="INFO" nightly_morning_report row)
are outside the convention by definition: they are not alarm paths.
"""
