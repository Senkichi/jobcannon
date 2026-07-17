"""Host-injectable runtime-config provider (None-default seam).

The private source reads scan-tuning knobs from Flask's app config
(``current_app.config["JF_CONFIG"]``) at call time. The engine has no
Flask; a host injects a zero-arg provider returning the same nested
mapping shape (e.g. ``{"ats": {...}, "health": {...}}``). With no
provider registered, ``get_runtime_config()`` returns ``{}`` so every
reader's hardcoded default applies — the same behavior the private code
has outside an app context.

``get_runtime_config()`` catches ANY exception the host-supplied provider
raises and returns ``{}`` at this layer — an arbitrary host callable can
raise anything (``OSError`` reading a config file, ``KeyError`` from a
bespoke mapping, custom exceptions), not just Flask's narrow
``RuntimeError``/``AttributeError`` failure modes. A raising or
misbehaving provider therefore degrades to defaults *here*, before it
ever reaches a reader. Readers may keep their own historical
``try/except`` around the returned mapping as defense-in-depth against
invalid *values* (not provider exceptions), but must not rely on a
provider exception propagating out of ``get_runtime_config()`` — it never
does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

_provider: Callable[[], Mapping[str, Any]] | None = None


def set_config_provider(provider: Callable[[], Mapping[str, Any]] | None) -> None:
    """Register (or clear, with None) the host's runtime-config provider."""
    global _provider
    _provider = provider


def get_runtime_config() -> Mapping[str, Any]:
    """Return the host-supplied config mapping, or ``{}`` on no provider / provider failure.

    A raising provider degrades to ``{}`` so every reader's hardcoded
    default applies — the same outcome the private code has outside an app
    context. Catching here (rather than relying on each reader's historical
    ``except`` tuple) is deliberate: those tuples were sized for Flask's
    specific failure modes (``RuntimeError``/``AttributeError``), while an
    arbitrary host callable can raise anything (``OSError`` reading a config
    file, ``KeyError`` from a bespoke mapping, custom exceptions). Readers
    keep their own ``try/except`` for invalid *values*.
    """
    if _provider is None:
        return {}
    try:
        return _provider() or {}
    except Exception:
        logger.debug("runtime_config provider raised; falling back to defaults", exc_info=True)
        return {}
