"""ADAPTED from job_finder/web/nightly_monitor/_sessions.py
@ b34eb0b00a13c0bf0c9c95020ceddfeca71022b8 (private job-cannon). Ledger L-0387.

Model dispatch seam shared by the morning audit and review stages.

# PORT-SEAM: private's _sessions.py spawns a tool-enabled `claude -p`
# subprocess per session (Read-only for an audit batch, Read+Grep for
# review), parsing the deliverable back out of stdout. That whole spawn
# mechanism DIES on this host: there is no local `claude` CLI, no session
# tool sandbox, and no cwd artifact directory to hand it. It is replaced by
# jobcannon.host.model_provider.call_model, one structured-output request
# per audit batch / per review night, matching the injection seam
# jobcannon.host.nightly.checkpoint_verdict.checkpoint_verdict already
# established in the same package (PR #355).
#
# That module's docstring spells out the gap this port shares: call_model
# states plainly that with no user_id, "the tenant's available-provider
# set is empty and the call fails closed via ProviderCascadeExhaustedError"
# -- no caller on this host has a live user_id-scoped dispatcher wired to
# a nightly-monitor tick yet. call_model is therefore injected here as an
# Optional[Callable] (default None, mirroring checkpoint_verdict's own
# call_model parameter), and this module's primary, exercised path in
# production is the "unavailable" branch below, not the happy path. That
# is not a bug in this port; it is the documented state of the host's
# model-dispatch architecture (the owner-tenant-identity resolution a
# future caller needs is unscoped here, same as checkpoint_verdict.py),
# tracked as a follow-up rather than invented in this unit.
#
# Session/reset-time/not-logged-in classification (_SESSION_LIMIT_RE,
# _RESET_TIME_RE, _NOT_LOGGED_IN_RE, SessionResult.session_limit/
# reset_time/auth_failed) DIES with the subprocess it classified:
# call_model raises typed exceptions (ProviderCascadeExhaustedError,
# ProviderCascadeTimeoutError) instead of an exit code plus stdout to
# pattern-match, so there is nothing left to classify with a regex.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from jobcannon.host.model_provider import (
    ProviderCascadeExhaustedError,
    ProviderCascadeTimeoutError,
)

logger = logging.getLogger(__name__)

_ERROR_CLIP = 300


@dataclass(frozen=True, slots=True)
class SessionResult:
    """Outcome of one structured model dispatch.

    ``ok`` is True only when call_model returned AND the result passed
    schema validation (``ModelResult.schema_valid``). ``unavailable`` is
    set when no dispatcher was wired (``call_model`` is None) or the
    cascade could not reach any provider / timed out -- the expected path
    on this host today (see module docstring); distinct from a generic
    dispatch failure so callers can report "not wired yet" separately from
    "wired and broken". ``error`` carries a truncated diagnostic string in
    every non-ok case.
    """

    ok: bool
    data: dict | None
    error: str | None
    unavailable: bool = False


def run_structured_session(
    *,
    tier: str,
    system: str,
    prompt: str,
    output_schema: dict,
    conn: Any,
    config: dict,
    call_model: Callable[..., Any] | None,
    purpose: str = "",
    job_id: str | None = None,
    max_tokens: int = 4096,
    timeout: float | None = None,
) -> SessionResult:
    """Dispatch one structured-output model call; never raises.

    ``timeout`` omitted (None, the default) takes call_model's per-tier
    default -- this seam does not forward call_model's separate "explicit
    None means unbounded" opt-in, since no caller in this unit needs it.
    """
    try:
        kwargs: dict[str, Any] = dict(
            tier=tier,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            conn=conn,
            config=config,
            output_schema=output_schema,
            job_id=job_id,
            purpose=purpose,
            max_tokens=max_tokens,
        )
        if timeout is not None:
            kwargs["timeout"] = timeout
        result = call_model(**kwargs)
    except (ProviderCascadeExhaustedError, ProviderCascadeTimeoutError) as exc:
        return SessionResult(ok=False, data=None, error=str(exc)[:_ERROR_CLIP], unavailable=True)
    except TypeError as exc:
        # call_model is None on this host today (see module docstring) --
        # calling None(...) raises TypeError, the same fail-safe branch a
        # real cascade exhaustion takes.
        return SessionResult(ok=False, data=None, error=str(exc)[:_ERROR_CLIP], unavailable=True)
    except Exception as exc:  # noqa: BLE001 - never raise into the caller's stage loop
        logger.warning("nightly model_session dispatch failed: %s", exc, exc_info=True)
        return SessionResult(ok=False, data=None, error=str(exc)[:_ERROR_CLIP])

    if not result.schema_valid:
        return SessionResult(
            ok=False, data=result.data, error="model output failed schema validation"
        )
    return SessionResult(ok=True, data=result.data, error=None)
