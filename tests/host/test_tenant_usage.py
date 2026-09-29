"""jobcannon.host.tenant_usage -- per-tenant spend/quota + rate accounting
over model_usage_ledger (issue #333, migration m0031).

Not a port: the private repo's owner-budget cost_gate and process-global
daily-request tracker were deliberately not carried (see
jobcannon.host.model_provider's module docstring); this module is their
per-tenant replacement. These tests exercise the ledger SQL, both gates,
and the RLS tenant_isolation policy itself against real Postgres --
a bug in either the module or the migration produces the same visible
symptom (a query returns rows it should not, or none).
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from jobcannon.host import tenant_usage as tu

from tests.host.conftest import requires_postgres

pytestmark = requires_postgres


def _seed_user(conn, user_id: str) -> None:
    conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))


def _age_rows(conn, user_id: str, interval_sql: str) -> None:
    """Backdate one tenant's ledger rows (deterministic window edges --
    no wall-clock sleeps)."""
    conn.execute(
        f"UPDATE model_usage_ledger SET created_at = now() - {interval_sql} WHERE user_id = %s",
        (user_id,),
    )


# --- DDL shape: the owner-approved user_id column ----------------------


def test_model_usage_ledger_has_not_null_user_id_column(db_conn):
    """The issue's explicit verification: the hosted cost/usage table's
    user_id column exists and is NOT NULL."""
    cols = db_conn.execute(
        "SELECT column_name, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'model_usage_ledger'"
    ).fetchall()
    by_name = {r["column_name"]: r["is_nullable"] for r in cols}
    assert by_name["user_id"] == "NO"
    assert by_name["provider"] == "NO"
    assert by_name["cost_usd"] == "NO"
    assert by_name["created_at"] == "NO"


# --- record_usage ------------------------------------------------------


def test_record_usage_writes_tenant_scoped_row(db_conn):
    _seed_user(db_conn, "tu-u1")

    tu.record_usage(
        db_conn,
        user_id="tu-u1",
        provider="groq",
        model="llama-3.1-8b-instant",
        cost_usd=0.002,
        input_tokens=11,
        output_tokens=7,
        job_id="j|1",
        purpose="score",
        schema_valid=True,
    )

    row = db_conn.execute("SELECT * FROM model_usage_ledger").fetchone()
    assert row["user_id"] == "tu-u1"
    assert (row["provider"], row["model"]) == ("groq", "llama-3.1-8b-instant")
    assert row["cost_usd"] == pytest.approx(0.002)
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert (row["job_id"], row["purpose"], row["schema_valid"]) == ("j|1", "score", True)


def test_record_usage_zeroes_free_provider_cost(db_conn):
    """gemini is is_free: the notional adapter cost_usd records as 0.0 so
    quota SUMs never charge a tenant for free-tier calls."""
    _seed_user(db_conn, "tu-u2")

    tu.record_usage(
        db_conn, user_id="tu-u2", provider="gemini", model="gemini-2.5-flash", cost_usd=0.999
    )

    row = db_conn.execute("SELECT cost_usd FROM model_usage_ledger").fetchone()
    assert row["cost_usd"] == 0.0


def test_record_usage_requires_user_id(db_conn):
    with pytest.raises(ValueError):
        tu.record_usage(db_conn, user_id="", provider="groq", model="m", cost_usd=0.0)


def test_recorded_cost_usd_normalizes_free_vs_paid():
    assert tu.recorded_cost_usd("gemini", 0.5) == 0.0
    assert tu.recorded_cost_usd("groq", 0.5) == 0.5
    assert tu.recorded_cost_usd("cerebras", 0.001) == pytest.approx(0.001)


# --- check_quota (per-tenant spend gate) -------------------------------


def test_check_quota_passes_under_cap(db_conn):
    _seed_user(db_conn, "tu-q1")
    tu.record_usage(db_conn, user_id="tu-q1", provider="groq", model="m", cost_usd=0.50)

    tu.check_quota(db_conn, "tu-q1", limits=tu.TenantUsageLimits(rolling_day_spend_cap_usd=1.0))


def test_check_quota_raises_at_cap(db_conn):
    _seed_user(db_conn, "tu-q2")
    for _ in range(3):
        tu.record_usage(db_conn, user_id="tu-q2", provider="groq", model="m", cost_usd=0.40)

    with pytest.raises(tu.TenantQuotaExceededError):
        tu.check_quota(db_conn, "tu-q2", limits=tu.TenantUsageLimits(rolling_day_spend_cap_usd=1.0))


def test_check_quota_ignores_rows_outside_rolling_window(db_conn):
    _seed_user(db_conn, "tu-q3")
    tu.record_usage(db_conn, user_id="tu-q3", provider="groq", model="m", cost_usd=99.0)
    _age_rows(db_conn, "tu-q3", "interval '2 days'")

    tu.check_quota(db_conn, "tu-q3", limits=tu.TenantUsageLimits(rolling_day_spend_cap_usd=1.0))


def test_check_quota_sums_across_providers_but_not_tenants(db_conn):
    """The quota is per-TENANT across all providers: gemini+groq spend
    pools into one cap, and another tenant's spend never counts."""
    _seed_user(db_conn, "tu-q4")
    _seed_user(db_conn, "tu-q5")
    tu.record_usage(db_conn, user_id="tu-q4", provider="groq", model="m", cost_usd=0.60)
    tu.record_usage(db_conn, user_id="tu-q4", provider="cerebras", model="m", cost_usd=0.60)
    tu.record_usage(db_conn, user_id="tu-q5", provider="groq", model="m", cost_usd=9.0)
    cap = tu.TenantUsageLimits(rolling_day_spend_cap_usd=1.0)

    with pytest.raises(tu.TenantQuotaExceededError):
        tu.check_quota(db_conn, "tu-q4", limits=cap)
    # Isolation proof: if tu-q5's $9.00 leaked into tu-q4's SUM the total
    # would read $10.20 and this $1.21 cap would still block -- it clears
    # because only tu-q4's own $1.20 counted.
    tu.check_quota(db_conn, "tu-q4", limits=tu.TenantUsageLimits(rolling_day_spend_cap_usd=1.21))


def test_check_quota_noop_when_cap_disabled(db_conn):
    _seed_user(db_conn, "tu-q6")
    tu.record_usage(db_conn, user_id="tu-q6", provider="groq", model="m", cost_usd=999.0)

    tu.check_quota(db_conn, "tu-q6", limits=tu.TenantUsageLimits(rolling_day_spend_cap_usd=None))


# --- check_rate_limit (per-(user_id, provider)) ------------------------


def test_check_rate_limit_raises_at_minute_cap(db_conn):
    _seed_user(db_conn, "tu-r1")
    limits = tu.TenantUsageLimits(per_provider_calls_per_minute=2, per_provider_calls_per_day=None)
    for _ in range(2):
        tu.record_usage(db_conn, user_id="tu-r1", provider="groq", model="m", cost_usd=0.0)

    with pytest.raises(tu.TenantRateLimitExceededError):
        tu.check_rate_limit(db_conn, "tu-r1", "groq", limits=limits)


def test_check_rate_limit_is_scoped_per_user_and_provider(db_conn):
    """A tenant over the cap on one provider stays served on another, and
    a DIFFERENT tenant's identical burst is unaffected."""
    _seed_user(db_conn, "tu-r2")
    _seed_user(db_conn, "tu-r3")
    limits = tu.TenantUsageLimits(per_provider_calls_per_minute=1, per_provider_calls_per_day=None)
    tu.record_usage(db_conn, user_id="tu-r2", provider="groq", model="m", cost_usd=0.0)
    tu.record_usage(db_conn, user_id="tu-r3", provider="groq", model="m", cost_usd=0.0)

    with pytest.raises(tu.TenantRateLimitExceededError):
        tu.check_rate_limit(db_conn, "tu-r2", "groq", limits=limits)
    # other provider and other tenant both still pass
    tu.check_rate_limit(db_conn, "tu-r2", "cerebras", limits=limits)
    tu.check_rate_limit(db_conn, "tu-r3", "gemini", limits=limits)


def test_check_rate_limit_minute_window_ignores_aged_rows(db_conn):
    _seed_user(db_conn, "tu-r4")
    tu.record_usage(db_conn, user_id="tu-r4", provider="groq", model="m", cost_usd=0.0)
    _age_rows(db_conn, "tu-r4", "interval '90 seconds'")

    tu.check_rate_limit(
        db_conn,
        "tu-r4",
        "groq",
        limits=tu.TenantUsageLimits(
            per_provider_calls_per_minute=1, per_provider_calls_per_day=None
        ),
    )


def test_check_rate_limit_day_cap_and_window_edge(db_conn):
    _seed_user(db_conn, "tu-r5")
    limits = tu.TenantUsageLimits(per_provider_calls_per_minute=None, per_provider_calls_per_day=2)
    for _ in range(2):
        tu.record_usage(db_conn, user_id="tu-r5", provider="groq", model="m", cost_usd=0.0)

    with pytest.raises(tu.TenantRateLimitExceededError):
        tu.check_rate_limit(db_conn, "tu-r5", "groq", limits=limits)

    # Backdate past the 24h window -> the cap clears.
    _age_rows(db_conn, "tu-r5", "interval '2 days'")
    tu.check_rate_limit(db_conn, "tu-r5", "groq", limits=limits)


def test_check_rate_limit_noop_when_both_caps_disabled(db_conn):
    _seed_user(db_conn, "tu-r6")
    for _ in range(5):
        tu.record_usage(db_conn, user_id="tu-r6", provider="groq", model="m", cost_usd=0.0)

    tu.check_rate_limit(
        db_conn,
        "tu-r6",
        "groq",
        limits=tu.TenantUsageLimits(
            per_provider_calls_per_minute=None, per_provider_calls_per_day=None
        ),
    )


# --- limits_from_config ------------------------------------------------


def test_limits_from_config_defaults_when_section_absent():
    assert tu.limits_from_config({}) == tu.DEFAULT_TENANT_USAGE_LIMITS
    assert tu.limits_from_config(None) == tu.DEFAULT_TENANT_USAGE_LIMITS
    assert tu.limits_from_config({"unrelated": 1}) == tu.DEFAULT_TENANT_USAGE_LIMITS


def test_limits_from_config_overrides_and_disables():
    limits = tu.limits_from_config(
        {
            "tenant_usage": {
                "rolling_day_spend_cap_usd": 2.5,
                "per_provider_calls_per_minute": None,
                "per_provider_calls_per_day": 5,
            }
        }
    )
    assert limits.rolling_day_spend_cap_usd == 2.5
    assert limits.per_provider_calls_per_minute is None
    assert limits.per_provider_calls_per_day == 5


def test_limits_from_config_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown"):
        tu.limits_from_config({"tenant_usage": {"daily_cap_usd": 1}})


def test_limits_from_config_rejects_nonpositive_or_nonmapping():
    with pytest.raises(ValueError):
        tu.limits_from_config({"tenant_usage": {"rolling_day_spend_cap_usd": 0}})
    with pytest.raises(ValueError):
        tu.limits_from_config({"tenant_usage": {"per_provider_calls_per_minute": -3}})
    with pytest.raises(ValueError):
        tu.limits_from_config({"tenant_usage": "not-a-mapping"})


# --- RLS tenant_isolation (m0031) --------------------------------------


def _impersonate_nonsuperuser_reader(db_conn) -> str:
    """db_conn connects as the Postgres superuser (POSTGRES_ADMIN_DSN), and
    superusers bypass RLS regardless of FORCE -- a bare SELECT/INSERT under
    db_conn's own role would pass even with a broken policy. Same
    convention as test_byo_key_credentials' impersonation helper: create a
    throwaway NOLOGIN role, grant the table surface, SET ROLE to it.
    CREATE ROLE is transactional, so db_conn's per-test ROLLBACK undoes it.
    """
    role = f"tu_rls_test_{uuid.uuid4().hex[:8]}"
    db_conn.execute(f"CREATE ROLE {role} NOLOGIN")
    db_conn.execute(f"GRANT SELECT, INSERT ON model_usage_ledger TO {role}")
    db_conn.execute(f"SET ROLE {role}")
    return role


def test_rls_blocks_cross_tenant_and_unscoped_access(db_conn):
    """m0031's tenant_isolation policy: rows are only visible/writable for
    the app.user_id session var's tenant -- a different tenant's scope sees
    and may write nothing, and an unset var defaults to deny."""
    _seed_user(db_conn, "tu-owner")
    tu.record_usage(db_conn, user_id="tu-owner", provider="groq", model="m", cost_usd=1.0)

    _impersonate_nonsuperuser_reader(db_conn)
    try:
        db_conn.execute("SELECT set_config('app.user_id', %s, true)", ("tu-owner",))
        owner_rows = db_conn.execute("SELECT COUNT(*) AS n FROM model_usage_ledger").fetchone()
        assert owner_rows["n"] == 1

        db_conn.execute("SELECT set_config('app.user_id', %s, true)", ("tu-stranger",))
        stranger_rows = db_conn.execute("SELECT COUNT(*) AS n FROM model_usage_ledger").fetchone()
        assert stranger_rows["n"] == 0

        # A stranger's INSERT for the owner's user_id fails the WITH CHECK.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db_conn.transaction():
                db_conn.execute(
                    "INSERT INTO model_usage_ledger (user_id, provider, model) "
                    "VALUES (%s, 'groq', 'm')",
                    ("tu-owner",),
                )

        # Unset session var -> current_setting(..., true) is NULL ->
        # predicate never true -> default deny.
        db_conn.execute("RESET app.user_id")
        unscoped_rows = db_conn.execute("SELECT COUNT(*) AS n FROM model_usage_ledger").fetchone()
        assert unscoped_rows["n"] == 0
    finally:
        db_conn.execute("RESET ROLE")
