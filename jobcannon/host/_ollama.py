# PORTED from job_finder/web/scheduler/_ollama.py @ 99bde92e667fcc1fb2c66fdcbfaa526367c900f1 (private job-cannon). Ledger L-0054.
"""Operator-endpoint Ollama liveness probe.

Two-stage probe for an operator-supplied Ollama endpoint:

  Stage 1a — HTTP liveness (GET /api/tags, timeout=1.0 s)
  Stage 1b — One 500 ms backoff retry on connection failure
  Stage 2  — Schema check (``{"models": [...]}`` shape)

Returns one of:

  AlreadyRunning(model_present=<bool>)
      A reachable /api/tags endpoint was found.

  Unavailable()
      Not reachable, or reachable but schema-mismatched. Cascade should
      fall through to the next provider.

URL resolution precedence (applied before any probe):
  1. ``JC_OLLAMA_URL`` environment variable
  2. ``config["providers"]["ollama"]["base_url"]``
  3. Default ``http://localhost:11434``

# PORT-SEAM: this is an ADAPT-with-drop reduction (design note PR-4 section 1d), not a
# 1:1 port. DROPPED entirely: ``_find_ollama_binary``, ``spawn_ollama``,
# ``register_owned_process``, the Win32 Job Object machinery, and
# ``probe_ollama``'s installability/spawn-decision stage (Stage 3) — hosted
# never spawns a local Ollama binary, only ever probes an operator-supplied
# endpoint. The ``Installable`` result type is dropped along with its sole
# producer. ``spawned_by_us`` is dropped from ``AlreadyRunning`` (always False
# once nothing here ever spawns). The private ``JOB_CANNON_OLLAMA_URL`` env var
# is renamed ``JC_OLLAMA_URL`` to match this repo's ``JC_*`` convention (see
# ``JC_SCAN_CRON`` et al. in ``jobcannon/host/tasks.py``); ``OLLAMA_EXE`` has no
# analog here since binary discovery is dropped with it.

# PORT-SEAM: wiring this probe into a provider's health check (design note
# section 5 Q2's recommended fold into ``providers/ollama_provider``) is explicitly
# OUT OF SCOPE for this row — owned by the byokey provider design
# (design-providers-byokey.md), a different unit. This module is ported
# standalone and unwired, matching the row's own "reduced to just the
# operator-endpoint probe" scope.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_DEFAULT_OLLAMA_URL = "http://localhost:11434"
_PROBE_TIMEOUT = 1.0  # seconds — single attempt
_RETRY_BACKOFF = 0.5  # seconds — one retry after first failure


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AlreadyRunning:
    """Ollama is already reachable at the resolved URL."""

    model_present: bool = False


@dataclass
class Unavailable:
    """Ollama is not reachable (or reachable but schema-mismatched)."""


OllamaState = AlreadyRunning | Unavailable


# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------


def resolve_ollama_url(config: dict) -> str:
    """Resolve the Ollama base URL from env > config > default.

    Args:
        config: Full app config dict (or a sub-section — reads
                ``config["providers"]["ollama"]["base_url"]``).

    Returns:
        Resolved URL string (trailing slash stripped).
    """
    # 1. Env var override
    env_url = os.environ.get("JC_OLLAMA_URL", "").strip()
    if env_url:
        return env_url.rstrip("/")

    # 2. Config key
    provider_cfg = config.get("providers", {}).get("ollama", {})
    cfg_url = provider_cfg.get("base_url", "").strip()
    if cfg_url:
        return cfg_url.rstrip("/")

    # 3. Default
    return _DEFAULT_OLLAMA_URL


# ---------------------------------------------------------------------------
# Liveness probe (with one retry)
# ---------------------------------------------------------------------------


def _probe_liveness(resolved_url: str) -> dict | None:
    """Attempt GET /api/tags. Returns parsed JSON dict on success, None on failure.

    Tries once, then waits ``_RETRY_BACKOFF`` seconds and tries once more
    (stage 1b). Returns None on any error — the caller decides what that means.
    """
    url = f"{resolved_url}/api/tags"

    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=_PROBE_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if attempt == 0:
                time.sleep(_RETRY_BACKOFF)
            # second attempt failing -> fall through to return None

    return None


# ---------------------------------------------------------------------------
# Main probe entry point
# ---------------------------------------------------------------------------


def probe_ollama(target_model: str, resolved_url: str) -> OllamaState:
    """Two-stage Ollama liveness probe (operator endpoint only).

    Args:
        target_model: Model tag the caller intends to use (e.g. "qwen2.5:14b").
                      Used only to populate ``AlreadyRunning.model_present``.
        resolved_url: Base URL resolved by ``resolve_ollama_url()`` — passed in
                      so the caller can store it and mutate live config once.

    Returns:
        ``AlreadyRunning`` or ``Unavailable``.
    """
    data = _probe_liveness(resolved_url)

    if data is None:
        logger.info("Ollama endpoint unreachable at %s", resolved_url)
        return Unavailable()

    # Stage 2 — schema check
    if not (isinstance(data, dict) and "models" in data and isinstance(data["models"], list)):
        logger.warning(
            "Port responded but did not look like Ollama (`/api/tags` schema mismatch); "
            "skipping. Set `JC_OLLAMA_URL=http://otherhost:port` to override."
        )
        return Unavailable()

    # Healthy Ollama — check if the target model is already pulled
    model_present = any(
        m.get("name", "") == target_model or m.get("model", "") == target_model
        for m in data["models"]
    )
    logger.info("Ollama reachable (model_present=%s)", model_present)
    return AlreadyRunning(model_present=model_present)
