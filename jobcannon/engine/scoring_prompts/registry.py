# PORTED from job_finder/web/scoring_prompts/registry.py @ 07cdef9c3c2d93f8c38170affa344e27f7f2c133 (private job-cannon). Ledger L-0057.
"""Prompt-variant registry: the single resolver for ``scoring.prompt_variant``.

``scoring.prompt_variant`` names a module under
``jobcannon.engine.scoring_prompts.variants`` (``baseline`` aliases the frozen
production ``v3_scoring_prompt``). Resolution is a dynamic ``importlib`` import,
which a grep-based dead-code pass cannot see: #570 deleted ``v4_finalist`` while
the live ``config.yaml`` still named it, and the #2002 config restore later put
that name back on disk. The app booted, reported healthy, and every scoring
call for the next ~15 hours failed with ``Unknown scoring prompt variant``
(one WARNING per job, no alarm).

# PORT-SEAM: the private module also owns validate_prompt_variant, a
# fail-fast check that raises job_finder.config.ConfigError at two host
# config boundaries (app boot, Settings-save write). An engine module
# cannot import that host-shaped exception type (config.py is ledgered
# DIES), and there is no public landing site for the check itself yet, so
# only the pure resolvers below are ported. The fail-fast half lands
# wherever a future PR wires score_and_persist_job into a host, using a
# host-owned error type there. See ledger L-0057.
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType

# PORT-SEAM: job_finder.config.ConfigError not carried -- see the module
# docstring above (ledger L-0057).
BASELINE_VARIANT = "baseline"


def variant_name(config: dict | None) -> str:
    """Read ``scoring.prompt_variant`` from config; default to ``baseline``."""
    if not config:
        return BASELINE_VARIANT
    return (config.get("scoring") or {}).get("prompt_variant") or BASELINE_VARIANT


def resolve_variant_module(name: str) -> ModuleType:
    """Return the prompt module for a named variant.

    ``baseline`` aliases the production v3_scoring_prompt module. Any other
    name is resolved as ``jobcannon.engine.scoring_prompts.variants.<name>``.
    Unknown names raise ImportError mentioning the requested variant — never
    silently fall back to baseline (silent fallback masks experiment errors).
    """
    if name == BASELINE_VARIANT:
        from jobcannon.engine.scoring_prompts import v3_scoring_prompt as mod

        return mod
    try:
        return importlib.import_module(f"jobcannon.engine.scoring_prompts.variants.{name}")
    except ImportError as exc:
        raise ImportError(f"Unknown scoring prompt variant: {name!r}") from exc


def list_prompt_variants() -> tuple[str, ...]:
    """Every name ``scoring.prompt_variant`` may take, discovered from the package.

    Derived from the modules physically present under ``variants/`` (private
    ``_helpers`` excluded) plus the ``baseline`` alias — so a variant added or
    removed on disk changes the answer with no list to maintain.
    """
    import jobcannon.engine.scoring_prompts.variants as variants_pkg

    names = {
        m.name for m in pkgutil.iter_modules(variants_pkg.__path__) if not m.name.startswith("_")
    }
    names.add(BASELINE_VARIANT)
    return tuple(sorted(names))


# PORT-SEAM: validate_prompt_variant (the fail-fast enforcement half; see
# the module docstring) stops here -- ledger L-0057.
