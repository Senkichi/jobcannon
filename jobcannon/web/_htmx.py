# PORTED from job_finder/web/_htmx.py @ b1aa40239b89327d2d0abdf38dd5cfd4f53698e3 (private job-cannon). Ledger L-0123.
"""Single-point HTMX fragment-route guard.

Fragment routes render a bare partial (no ``base.html`` shell), so a direct
browser navigation to the URL — or any non-HTMX request — would surface an
unstyled orphan fragment. The project contract (CLAUDE.md): "Fragment routes
MUST check the HX-Request header and return the full page for direct browser
access."

This decorator is the single enforcement point for that contract. Before it,
the guard was an ``if not request.headers.get("HX-Request"): return redirect(...)``
idiom hand-copied into some fragment routes and silently omitted from ~15
others (the exact "built but only half-wired" footgun). Routing every fragment
route through ``@htmx_fragment`` makes the guard impossible to forget and
impossible to drift.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import TypeVar

from flask import redirect, request, url_for

F = TypeVar("F", bound=Callable[..., object])


def htmx_fragment(redirect_to: str = "dashboard.index") -> Callable[[F], F]:
    """Serve a fragment-only view to HTMX requests; redirect everyone else.

    Apply BELOW the ``@<bp>.route(...)`` decorator so Flask registers the
    wrapped view::

        @jobs_bp.route("/<path:dedup_key>/score-cell", strict_slashes=False)
        @htmx_fragment("jobs.index")
        def score_cell(dedup_key): ...

    A request without the ``HX-Request`` header (e.g. a direct address-bar hit)
    is redirected to ``redirect_to`` — the parent page the fragment belongs to —
    instead of rendering the bare fragment.

    Args:
        redirect_to: endpoint name a non-HTMX request is redirected to.
    """

    def decorator(view: F) -> F:
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not request.headers.get("HX-Request"):
                return redirect(url_for(redirect_to))
            return view(*args, **kwargs)

        # Marker so a completeness test can introspect app.url_map and assert
        # every fragment route is guarded — catches a future fragment route
        # added without the decorator (the half-wiring this exists to prevent).
        wrapper._is_htmx_fragment = True  # type: ignore[attr-defined]
        wrapper._htmx_redirect_to = redirect_to  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
