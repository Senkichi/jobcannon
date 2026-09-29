"""Per-tenant spend/quota + rate accounting over model_usage_ledger (issue #333).

Single reader/writer for the ``model_usage_ledger`` table
(jobcannon/db/migrations/m0031_*), the hosted cost and usage table whose
``user_id`` column the owner ruling approved. This module owns three
things that were deliberately NOT carried by the model_provider port and
that this issue lands as a unit:

- The ledger write itself (``record_usage``) -- one row per completed
  model call, scoped by ``user_id``.
- The per-tenant quota gate (``check_quota``) -- the replacement for the
  owner-budget ``cost_gate`` semantics the private, single-user app ran
  inside its dispatcher. Private's version summed an OWNER's daily spend
  and raised BudgetExceededError; hosted sums one TENANT's rolling-24h
  ledger spend and raises TenantQuotaExceededError.
- Per-(user_id, provider) rate limits (``check_rate_limit``) -- read
  from this same tenant-scoped ledger. NEVER a process-global counter:
  the private repo's _daily_usage/_check_daily_limit/_increment_usage
  bookkeeping was process-global, which leaks across tenants and across
  gunicorn workers; the ledger is the only place call counts live, so a
  limit holds no matter how many workers serve the same tenant.

Tenant scoping is enforced twice, deliberately: every function sets the
m0020 ``app.user_id`` session var (``set_config('app.user_id', %s,
true)``, transaction-local) so the table's FORCE RLS tenant_isolation
policy passes, AND carries an explicit ``WHERE user_id = %s`` -- the
session var is the isolation boundary, the WHERE clause is the query
shape a reader can see without knowing the RLS convention. Same
belt-and-suspenders shape as jobcannon/db/_byo_key_credentials.py.

Config seam: ``limits_from_config`` reads an optional
``config["tenant_usage"]`` mapping (same key names as TenantUsageLimits
fields). Absent section -> defaults (limits ON). An explicit ``None``
value disables that one limit (the kill switch); unknown keys or
garbage values raise -- a typo'd limit silently not applying is worse
than a loud failure (host/config.py's documented philosophy).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import Any

from jobcannon.db.pool import commit_unless_nested, unwrap_raw
from jobcannon.host.provider_catalog import FREE_PROVIDER_NAMES

logger = logging.getLogger(__name__)


class TenantQuotaExceededError(RuntimeError):
    """A tenant's rolling-24h model spend reached the configured cap.

    Raised by check_quota before a dispatch is attempted -- the hosted
    counterpart of the private repo's BudgetExceededError, scoped to one
    tenant's ledger instead of an owner's process-global budget.
    """


class TenantRateLimitExceededError(RuntimeError):
    """A tenant's call rate for one provider reached the configured
    per-(user_id, provider) limit.

    call_model treats this as a skip-this-provider signal inside the
    cascade (a rate-limited provider is unavailable, not failed), and
    raises it at the top level only when EVERY provider in the resolved
    chain was skipped for rate limiting.
    """


# Rolling windows, not calendar days: a UTC-midnight reset both lets
# spend "double" across the boundary and pins the gate's behavior to a
# timezone choice the deployment does not otherwise make. A trailing
# window is monotonic, unambiguous, and cannot be gamed by scheduling
# calls around midnight.
_SPEND_WINDOW_SQL = "interval '24 hours'"
_RATE_MINUTE_WINDOW_SQL = "interval '60 seconds'"


@dataclass(frozen=True)
class TenantUsageLimits:
    """Per-tenant accounting limits. ``None`` on any field disables that
    one check (the kill switch) -- limits ship ON by default.

    Defaults are initial protective ceilings, not usage shaping: the
    tenant's own provider key is billed under BYO, so these bound
    runaway/abuse blast radius, not normal cost. Tunable per deployment
    through ``config["tenant_usage"]`` (see limits_from_config).
    """

    rolling_day_spend_cap_usd: float | None = 10.0
    per_provider_calls_per_minute: int | None = 30
    per_provider_calls_per_day: int | None = 1000


DEFAULT_TENANT_USAGE_LIMITS = TenantUsageLimits()


def recorded_cost_usd(provider: str, cost_usd: float) -> float:
    """The cost a ledger row actually stores for ``provider``.

    Free providers (provider_catalog.FREE_PROVIDER_NAMES -- gemini on the
    tenant's own Google quota, CLI/local transports) report a NOTIONAL
    cost_usd in their ModelResult; the ledger must record 0.0 for them or
    every quota SUM charges fake spend against a tenant whose calls cost
    nothing. This is the single place that normalization lives -- both
    model_provider.record_cost's log line and record_usage's INSERT route
    through it, so what is logged and what is stored can never disagree.
    """
    return 0.0 if provider in FREE_PROVIDER_NAMES else float(cost_usd)


def limits_from_config(config: dict | None) -> TenantUsageLimits:
    """Resolve TenantUsageLimits from ``config["tenant_usage"]``.

    ``config`` is call_model's own ``config`` dict (the host runtime
    mapping -- ScanServices.config). An absent/non-mapping-missing
    section yields the defaults (ON); a present section may override any
    field, with ``None`` disabling that limit. Unknown keys raise --
    a misspelled limit name must surface at dispatch time, not silently
    run unlimited.
    """
    section = (config or {}).get("tenant_usage")
    if section is None:
        return DEFAULT_TENANT_USAGE_LIMITS
    if not isinstance(section, dict):
        raise ValueError(
            f"tenant_usage limits config must be a mapping, got {type(section).__name__}"
        )
    known = {f.name for f in fields(TenantUsageLimits)}
    unknown = sorted(set(section) - known)
    if unknown:
        raise ValueError(f"unknown tenant_usage limit key(s): {unknown}")

    def _float_or_none(name: str) -> float | None:
        raw = section.get(name, getattr(DEFAULT_TENANT_USAGE_LIMITS, name))
        if raw is None:
            return None
        value = float(raw)
        if value <= 0:
            raise ValueError(f"tenant_usage.{name} must be > 0 or null (got {raw!r})")
        return value

    def _int_or_none(name: str) -> int | None:
        raw = section.get(name, getattr(DEFAULT_TENANT_USAGE_LIMITS, name))
        if raw is None:
            return None
        value = int(raw)
        if value < 0:
            raise ValueError(f"tenant_usage.{name} must be >= 0 or null (got {raw!r})")
        return value

    return TenantUsageLimits(
        rolling_day_spend_cap_usd=_float_or_none("rolling_day_spend_cap_usd"),
        per_provider_calls_per_minute=_int_or_none("per_provider_calls_per_minute"),
        per_provider_calls_per_day=_int_or_none("per_provider_calls_per_day"),
    )


def _set_tenant(raw: Any, user_id: str) -> None:
    """Scope this transaction to ``user_id`` for the table's RLS policy.

    Transaction-local (``is_local=true``) so a pooled connection can
    never carry one tenant's scope into a later checkout's queries --
    the convention m0020/_byo_key_credentials established for
    tenant-scoped tables.
    """
    raw.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))


def record_usage(
    conn: Any,
    *,
    user_id: str,
    provider: str,
    model: str,
    cost_usd: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    job_id: str | None = None,
    purpose: str = "",
    schema_valid: bool | None = None,
) -> None:
    """Append one cost/usage row to model_usage_ledger for ``user_id``.

    ``cost_usd`` is normalized through recorded_cost_usd() before the
    INSERT (free providers store 0.0) so quota SUMs over the table are
    truthful by construction rather than by caller discipline. Commits
    when the connection carries no ambient transaction; inside one (e.g.
    tests' rollback-isolated fixture) it rides the caller's boundary
    (commit_unless_nested semantics).
    """
    if not user_id:
        raise ValueError("record_usage: user_id must be non-empty -- ledger rows are tenant-scoped")
    raw = unwrap_raw(conn)
    with raw.transaction():
        _set_tenant(raw, user_id)
        raw.execute(
            "INSERT INTO model_usage_ledger (user_id, provider, model, cost_usd, "
            "input_tokens, output_tokens, job_id, purpose, schema_valid) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                provider,
                model,
                recorded_cost_usd(provider, cost_usd),
                input_tokens,
                output_tokens,
                job_id,
                purpose,
                schema_valid,
            ),
        )
    commit_unless_nested(raw)


def check_quota(
    conn: Any,
    user_id: str,
    *,
    limits: TenantUsageLimits = DEFAULT_TENANT_USAGE_LIMITS,
) -> None:
    """The per-tenant quota gate: raise when the tenant's rolling-24h
    spend is at/over the configured cap.

    No-op when ``limits.rolling_day_spend_cap_usd`` is None (disabled).
    """
    cap = limits.rolling_day_spend_cap_usd
    if cap is None:
        return
    raw = unwrap_raw(conn)
    _set_tenant(raw, user_id)
    row = raw.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM model_usage_ledger "
        f"WHERE user_id = %s AND created_at >= now() - {_SPEND_WINDOW_SQL}",
        (user_id,),
    ).fetchone()
    spend = float(row["spend"])
    if spend >= cap:
        raise TenantQuotaExceededError(
            f"tenant daily spend cap reached: user_id={user_id} spent "
            f"${spend:.4f} in the last 24h (cap ${cap:.4f})"
        )


def check_rate_limit(
    conn: Any,
    user_id: str,
    provider: str,
    *,
    limits: TenantUsageLimits = DEFAULT_TENANT_USAGE_LIMITS,
) -> None:
    """Per-(user_id, provider) rate gate: raise when the tenant's ledger
    shows too many calls to ``provider`` inside a limit window.

    Both windows are read in ONE query (per-minute is a FILTER on the
    per-day window, so a single scan answers both). No-op when both
    count limits are None.
    """
    minute_cap = limits.per_provider_calls_per_minute
    day_cap = limits.per_provider_calls_per_day
    if minute_cap is None and day_cap is None:
        return
    raw = unwrap_raw(conn)
    _set_tenant(raw, user_id)
    row = raw.execute(
        "SELECT COUNT(*) FILTER (WHERE created_at >= now() - "
        f"{_RATE_MINUTE_WINDOW_SQL}) AS per_minute, COUNT(*) AS per_day "
        "FROM model_usage_ledger "
        f"WHERE user_id = %s AND provider = %s AND created_at >= now() - {_SPEND_WINDOW_SQL}",
        (user_id, provider),
    ).fetchone()
    if minute_cap is not None and row["per_minute"] >= minute_cap:
        raise TenantRateLimitExceededError(
            f"per-provider rate limit reached: user_id={user_id} provider={provider} "
            f"{row['per_minute']} calls in the last 60s (cap {minute_cap})"
        )
    if day_cap is not None and row["per_day"] >= day_cap:
        raise TenantRateLimitExceededError(
            f"per-provider daily call cap reached: user_id={user_id} provider={provider} "
            f"{row['per_day']} calls in the last 24h (cap {day_cap})"
        )
