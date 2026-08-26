"""Env-var-backed host config (spec §3.4: hosted has no config.yaml).

Fail-fast on required values (same philosophy as the private repo's config
loader); scan-tuning knobs are OPTIONAL — an unset knob is ABSENT from the
mapping so the engine reader's hardcoded default applies, exactly the
fail-closed semantics runtime_config.get_runtime_config() documents.

"Optional" means UNSET -> absent from the mapping (and unset/whitespace-only
are treated the same way). It does NOT mean best-effort parsing: if the var
IS present with a non-blank value, it must parse as an integer or startup
fails fast with a named RuntimeError identifying the offending env var — a
typo silently changing runtime concurrency is worse than a clear crash.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HostConfig:
    # Every env-backed field below carries `metadata={"env": ..., "declare_on":
    # ...}`. `declare_on` names the render.yaml service TYPES ("web" /
    # "worker") that must declare the var — tests/host/test_render_config.py
    # derives its required-env-var set from this metadata rather than
    # restating it, so a field added here without render.yaml coverage fails
    # CI instead of going unnoticed. `runtime` carries no `env` key and is
    # skipped by that derivation; it is a derived mapping, not a single var.
    database_url: str = field(metadata={"env": "DATABASE_URL", "declare_on": ("web", "worker")})
    runtime: dict = field(default_factory=dict)
    posthog_api_key: str | None = field(
        default=None, metadata={"env": "POSTHOG_API_KEY", "declare_on": ()}
    )
    posthog_host: str | None = field(
        default=None, metadata={"env": "POSTHOG_HOST", "declare_on": ()}
    )
    # Deterministic-pseudonym HMAC key for the PostHog distinct_id
    # (jobcannon/host/posthog_client.py's pseudonymize()) — declared on BOTH
    # services because the worker builds its own PostHog client through the
    # same wiring.py seam as the web service. Deliberately its OWN env var,
    # never the Flask session secret (secret_key below): reusing that key
    # would tie analytics pseudonymization's blast radius to session-signing
    # material, and rotating one for one purpose would silently rotate the
    # other. Requiredness is enforced where it's consumed, not here (same
    # rationale as secret_key) — an unset salt must still produce a valid
    # HostConfig; posthog_client.pseudonymize() fails closed (returns None,
    # never the raw id) when it is.
    analytics_pseudonym_salt: str | None = field(
        default=None,
        metadata={"env": "JC_ANALYTICS_PSEUDONYM_SALT", "declare_on": ("web", "worker")},
    )
    # Flask session signing key. Requiredness is enforced where it's
    # consumed (jobcannon.web.create_app's fail-fast), not here — an unset
    # var must still produce a valid HostConfig so load_host_config's only
    # fail-fast stays DATABASE_URL.
    secret_key: str = field(default="", metadata={"env": "JC_SECRET_KEY", "declare_on": ("web",)})
    clerk_sign_up_url: str = field(
        default="", metadata={"env": "CLERK_SIGN_UP_URL", "declare_on": ("web",)}
    )
    # The worker serves no requests and mints no sessions, so it never reads
    # signup_wave.
    signup_wave: str = field(
        default="0", metadata={"env": "JC_SIGNUP_WAVE", "declare_on": ("web",)}
    )
    # Clerk request/webhook verification (jobcannon.web.auth.build_clerk_verifier
    # and jobcannon.web.create_app's webhook-secret read). Requiredness is
    # enforced at those consumption sites, not here, same rationale as
    # secret_key above — an unset var must still produce a valid HostConfig.
    # The worker never verifies a Clerk request or a webhook, so declare_on
    # is web-only for all four (issue #47 folded these in from a literal
    # that test_render_config.py used to hand-maintain).
    clerk_secret_key: str = field(
        default="", metadata={"env": "CLERK_SECRET_KEY", "declare_on": ("web",)}
    )
    clerk_jwt_key: str = field(
        default="", metadata={"env": "CLERK_JWT_KEY", "declare_on": ("web",)}
    )
    # Frontend (clerk-js) publishable key — issue #149. Distinct from the
    # four backend-verification fields above: those authenticate REQUESTS
    # server-side, this loads clerk-js in the BROWSER so it can complete
    # Clerk's cross-domain handshake (read the shared __client_uat cookie,
    # talk to FAPI, and set/refresh __session on this host) — the Python
    # backend SDK has no handshake support of its own
    # (jobcannon.web.auth's docstring), so without this the hosted
    # Account Portal sign-in never hands this host a session, ever.
    # Requiredness is enforced at consumption (create_app, non-TESTING),
    # same rationale as secret_key above.
    clerk_publishable_key: str = field(
        default="", metadata={"env": "CLERK_PUBLISHABLE_KEY", "declare_on": ("web",)}
    )
    clerk_authorized_parties: str = field(
        default="", metadata={"env": "CLERK_AUTHORIZED_PARTIES", "declare_on": ("web",)}
    )
    clerk_webhook_signing_secret: str = field(
        default="", metadata={"env": "CLERK_WEBHOOK_SIGNING_SECRET", "declare_on": ("web",)}
    )
    # Render auto-injects this on every deploy (render.com/docs/environment-
    # variables#all-runtimes) — it is NOT a var this repo declares in
    # render.yaml (declare_on=() exempts it from
    # test_render_config.py's "every required var is declared" derivation,
    # same reasoning as posthog_api_key/posthog_host above, which ARE
    # declared but via their own dedicated test rather than the generic
    # derivation). Empty locally and in any test double that doesn't set it
    # (jobcannon.web's footer context processor falls back to the repo root
    # URL via getattr's default, mirroring clerk_sign_up_url's tolerance of
    # a bare SimpleNamespace double in tests/host/test_empty_states.py).
    render_git_commit: str = field(
        default="", metadata={"env": "RENDER_GIT_COMMIT", "declare_on": ()}
    )


def _put_int(mapping: dict, section: str, key: str, env_var: str) -> None:
    val = os.environ.get(env_var)
    if val is not None and val.strip():
        try:
            mapping.setdefault(section, {})[key] = int(val)
        except ValueError as exc:
            raise RuntimeError(f"Invalid value for {env_var}: {val!r} (expected integer)") from exc


def load_host_config() -> HostConfig:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required (Postgres DSN)")
    runtime: dict = {}
    # Do NOT add an ats.scan_concurrency pass-through here. The engine's
    # concurrent scan branch (scan_concurrency > 1) deadlocks when the scan
    # deadline trips with submitted work still queued — jobcannon issue #39. Hosted is
    # safe today ONLY because this mapping omits that knob, so the engine
    # resolves its default of 1 and stays on the serial branch
    # (tripwire test: tests/host/test_scan_services_deadline.py). Fix #39
    # before adding the knob.
    _put_int(runtime, "ats", "scan_memo_ttl_s", "JC_SCAN_MEMO_TTL_S")
    _put_int(runtime, "ats", "detail_fetch_concurrency", "JC_DETAIL_FETCH_CONCURRENCY")
    _put_int(runtime, "ats", "page_fetch_concurrency", "JC_PAGE_FETCH_CONCURRENCY")
    statuses = os.environ.get("JC_AUTH_BLOCK_STATUSES")
    if statuses and statuses.strip():
        try:
            runtime.setdefault("health", {})["auth_block_statuses"] = [
                int(s) for s in statuses.split(",") if s.strip()
            ]
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid value for JC_AUTH_BLOCK_STATUSES: {statuses!r} "
                "(expected comma-separated integers)"
            ) from exc
    posthog_api_key = (os.environ.get("POSTHOG_API_KEY") or "").strip() or None
    posthog_host = (os.environ.get("POSTHOG_HOST") or "").strip() or None
    analytics_pseudonym_salt = (os.environ.get("JC_ANALYTICS_PSEUDONYM_SALT") or "").strip() or None
    return HostConfig(
        database_url=database_url,
        runtime=runtime,
        posthog_api_key=posthog_api_key,
        posthog_host=posthog_host,
        analytics_pseudonym_salt=analytics_pseudonym_salt,
        secret_key=os.environ.get("JC_SECRET_KEY", ""),
        clerk_sign_up_url=os.environ.get("CLERK_SIGN_UP_URL", ""),
        signup_wave=os.environ.get("JC_SIGNUP_WAVE", "0"),
        clerk_secret_key=os.environ.get("CLERK_SECRET_KEY", ""),
        clerk_jwt_key=os.environ.get("CLERK_JWT_KEY", ""),
        clerk_publishable_key=os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip(),
        clerk_authorized_parties=os.environ.get("CLERK_AUTHORIZED_PARTIES", ""),
        clerk_webhook_signing_secret=os.environ.get("CLERK_WEBHOOK_SIGNING_SECRET", ""),
        render_git_commit=os.environ.get("RENDER_GIT_COMMIT", ""),
    )
