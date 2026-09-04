"""ADAPTED from job_finder/web/nightly_monitor/_morning.py (the review
session invocation, assets/nightly_monitor/review_brief.md, and
_parse_review_stdout / _stub_report / _write_review_failure)
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8 (private job-cannon).
Ledger L-0387.

Adversarial review of one night's audit + checkpoint + coverage evidence,
via one structured-output call_model dispatch. Produces candidate GitHub
issues (filed by the caller through issue_filer.py) and a markdown report
body.

# PORT-SEAM: private ran this as a tool-enabled (Read+Grep) `claude -p`
# session that read artifact_dirs/open_issues.json/filed_issues.json off
# its own local filesystem and communicated back via a delimited stdout
# contract (_JSON_BEGIN/_REPORT_BEGIN markers, _parse_review_stdout).
# There is no local filesystem here for the session to read, and (per
# jobcannon.host.nightly.model_session's own seam, established in
# checkpoint_verdict.py / audit_stage.py by PR #355 and this unit) the
# driver already supplies every evidence source as arguments -- so this
# port collapses to a single structured-output run_structured_session
# (tier="craft") dispatch with the whole evidence bundle embedded in the
# prompt (no Read/Grep tools, no stdout-marker parsing, no artifact_dirs).
# "craft" is the closest jobcannon.host.model_provider._VALID_WORKLOADS
# tier to private's DEFAULT_NIGHTLY_REVIEW_MODEL = "opus" -- every
# provider_catalog.py entry maps "craft" to its top reasoning model,
# matching private's model choice for this session.
#
# dispatch_bash_rats DIES with _dispatch_bash_rats / the whole bash_rats
# config block (design note Q4: no `charlie` subprocess on this host,
# hand-off is issue_filer.py's `automated-ready` label only) -- dropped
# from both the brief text and the output schema, not stubbed to
# always-false.
#
# filed_issues.json / open_issues.json / artifact_dirs dedup references
# become plain arguments (prior_filed, open_issues) the caller
# (morning_driver.py) already assembled via issue_filer.list_open_issues +
# cross_check_prior_filings -- the review prompt embeds them directly
# instead of pointing at a path for the session to open itself.
#
# The stdout-marker parsing (_parse_review_stdout, _JSON_BEGIN/_REPORT_BEGIN
# regexes, _first_balanced_bracket_span and its jsonl/records fallbacks)
# DIES outright: model_session.run_structured_session already returns
# validated, schema-checked JSON via SessionResult.data -- there is no
# free-text stdout stream to delimit and re-parse.
"""

from __future__ import annotations

import json
import logging

from jobcannon.host.nightly.model_session import SessionResult, run_structured_session

logger = logging.getLogger(__name__)

_MAX_ISSUES_PER_RUN = 10
_ISSUE_TITLE_CLIP = 200
_ISSUE_BODY_CLIP = 15_000
_REVIEW_MAX_TOKENS = 8192

REVIEW_SYSTEM_PROMPT = """You are the morning adversarial reviewer for last night's overnight scheduled jobs on this host. Default SKEPTICAL: your job is to REFUTE findings, and only what survives refutation becomes a candidate GitHub issue. The user message carries a "Tonight's context" JSON block: audit_summary, checkpoint_summary, window_coverage, observer_offline, error_budget, disagreement_alarm_rate, disagreement_alarm_min_sample, disagreement_rate_anomalous, open_issues, prior_filed.

You have no tools. You cannot write a report, file issues, or run anything yourself -- you return a decision payload and the driver (a plain Python process, not you) performs every side effect: persisting the report, filing GitHub issues against a fixed, config-pinned repo. This is deliberate: your evidence includes attacker-influenced text (job descriptions, audit notes), so the driver -- never you -- is the only thing allowed to touch the database or GitHub.

## Inputs

- `open_issues`: {"status": "ok" | "unavailable", "reason": ..., "issues": [{"number", "title", "body", "url"}, ...]}. This is your dedup reference, already fetched by the driver from the real configured repo. When status is "unavailable", the driver will not file any issues this run (proposed titles still appear in report_md) -- note the limitation but still list validated findings for manual review.
- `prior_filed`: a list of issue-filing outcome records from the previous run ({"title", "outcome": "created"|"failed"|"skipped", "url", "reason"}), the host equivalent of private's filed_issues.json. Use it alongside `open_issues` to avoid proposing the same finding twice: a finding already recorded as "created" or "failed" should not be re-proposed until its underlying condition changes; "skipped" entries mean the driver could not verify repo state that run, so dedup those only against `open_issues`.
- `checkpoint_summary`: {"rejected_reasons": int | None, "by_verdict": {...}}. On this host, only FAIL-verdict checkpoints are durably recorded (the sampler is a PASSIVE, non-flooding observer -- PASS/ANOMALY verdict detail is not persisted); `rejected_reasons` is None when that detail is unavailable rather than a fabricated zero. Do not treat a None/sparse checkpoint_summary as a clean run -- it means limited visibility, not the absence of problems.
- `window_coverage` / `observer_offline`: this run's sampler-tick and checkpoint-tick coverage of the review window. `observer_offline` true means the window was blind -- treat as NO DATA, not a clean run.
- `error_budget`: overnight WARNING/ERROR scan_health_log counts, per-source, and new patterns since the prior window (structured, not log text).

## Tasks, in order

1. **Refute first.** For every FAIL checkpoint and every audit dispute in the evidence: adversarially attempt to refute it. Baseline noise? Already covered by an open issue (check `open_issues`)? Only validated findings survive.
2. **Systemic audit patterns.** Cross-check audit disputes for systemic patterns (same axis drifting across jobs, same JD-quality failure class) vs one-off disagreements. Systemic patterns become findings; one-offs do not. A raw disagreement_rate above disagreement_alarm_rate is NOT by itself noteworthy -- normal runs routinely land at 45-59% and a small audit sample (n below disagreement_alarm_min_sample) is statistically meaningless. Only lead the report with the disagreement rate when disagreement_rate_anomalous is true (it already encodes both the min-sample floor and a baseline-relative comparison against recent runs). If it is false, do not propose a disagreement-rate issue no matter how the raw rate looks.
3. **Dedup survivors** against `open_issues` and `prior_filed` (by title/topic similarity and recorded outcome) before proposing any issue.
4. **Compose report_md**: verdict line first; validated anomalies with evidence; audit summary (disagreement rate, disputes list); error budget summary; proposed issues (title only -- you cannot know which the driver actually managed to file); UNMAPPED / UNOBSERVED gaps with reasons; window boundary note.

## Output contract

Return a JSON object: {"issues_to_file": [{"title": "<type>(<subsystem>): <description>", "body": "Problem / Proposal / Acceptance Criteria (checkboxes) / Scoping / Evidence (field:value references into tonight's context) / Test Commands", "labels": ["automated-ready"]}], "report_md": "# Nightly Monitor Report ...full markdown..."}.

- `issues_to_file` may be an empty list on a clean run.
- Label `automated-ready` ONLY when dispatch-safe without human decisions.
- The driver files each issue verbatim -- it does not re-interpret your title/body, so write them exactly as you want them to appear.
- This contract is mandatory even on a clean run -- an empty issues_to_file and a one-line PASS verdict in report_md is a complete, valid response.
- Never pause, resume, or trigger any scheduled job. You are an observer."""

REVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "issues_to_file": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": _ISSUE_TITLE_CLIP},
                    "body": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "body"],
            },
        },
        "report_md": {"type": "string"},
    },
    "required": ["issues_to_file", "report_md"],
}


def _stub_report(date_str: str, audit_summary: dict, session: SessionResult) -> str:
    """Fallback report when the review session did not return a usable
    result -- mirrors private's _stub_report (INCOMPLETE status + the raw
    audit summary), minus the stderr-fenced-code-block field (there is no
    subprocess stderr on this host to embed)."""
    reason = session.error or "review session did not return valid structured output"
    return (
        f"# Nightly Monitor Report -- {date_str}\n\n"
        "**STATUS: INCOMPLETE** -- the review session did not produce a "
        "usable result.\n\n"
        f"- session error: {reason}\n"
        f"- audit summary: {json.dumps(audit_summary)}\n\n"
    )


def run_review_stage(
    *,
    date_str: str,
    conn,
    config: dict,
    call_model,
    audit_summary: dict,
    checkpoint_summary: dict,
    window_coverage: dict,
    observer_offline: bool,
    error_budget: dict,
    disagreement_alarm_rate: float,
    disagreement_alarm_min_sample: int,
    disagreement_rate_anomalous: bool,
    open_issues: dict,
    prior_filed: list[dict],
) -> dict:
    """Run the adversarial review session; never raises.

    Returns {"incomplete": bool, "report_md": str, "issues_to_file": [...]}.
    On any non-ok SessionResult, ``incomplete`` is True, ``issues_to_file``
    is empty (nothing is ever filed off an unparsed/unavailable review),
    and ``report_md`` is the stub report -- matching private's
    incomplete-review fallback path.
    """
    context = {
        "audit_summary": audit_summary,
        "checkpoint_summary": checkpoint_summary,
        "window_coverage": window_coverage,
        "observer_offline": observer_offline,
        "error_budget": error_budget,
        "disagreement_alarm_rate": disagreement_alarm_rate,
        "disagreement_alarm_min_sample": disagreement_alarm_min_sample,
        "disagreement_rate_anomalous": disagreement_rate_anomalous,
        "open_issues": open_issues,
        "prior_filed": prior_filed,
    }
    prompt = f"## Tonight's context\n```json\n{json.dumps(context, indent=2)}\n```"

    session = run_structured_session(
        tier="craft",
        system=REVIEW_SYSTEM_PROMPT,
        prompt=prompt,
        output_schema=REVIEW_OUTPUT_SCHEMA,
        conn=conn,
        config=config,
        call_model=call_model,
        purpose="nightly_review",
        max_tokens=_REVIEW_MAX_TOKENS,
    )

    if not session.ok or not isinstance(session.data, dict):
        return {
            "incomplete": True,
            "report_md": _stub_report(date_str, audit_summary, session),
            "issues_to_file": [],
        }

    raw_issues = session.data.get("issues_to_file") or []
    issues: list[dict] = []
    for entry in raw_issues:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()[:_ISSUE_TITLE_CLIP]
        body = str(entry.get("body") or "").strip()[:_ISSUE_BODY_CLIP]
        if not title or not body:
            continue
        labels = [str(x) for x in (entry.get("labels") or []) if isinstance(x, str)]
        issues.append({"title": title, "body": body, "labels": labels})

    report_md = str(session.data.get("report_md") or "").strip()
    if not report_md:
        return {
            "incomplete": True,
            "report_md": _stub_report(date_str, audit_summary, session),
            "issues_to_file": [],
        }

    return {
        "incomplete": False,
        "report_md": report_md + "\n",
        "issues_to_file": issues[:_MAX_ISSUES_PER_RUN],
    }
