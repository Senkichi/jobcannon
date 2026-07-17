"""Optional extraction-health recording seam.

The private repo's ats_platforms._registry recorded raw-payload health via
job_finder.web.autoheal. The engine keeps the call site but forwards to a
host-registered recorder; with none registered it is a no-op. Hosts call
set_recorder() once at startup.
"""

from __future__ import annotations

from typing import Any, Callable

_recorder: Callable[..., Any] | None = None
_min_meaningful_len: int = 0


def set_recorder(recorder: Callable[..., Any] | None, *, min_meaningful_len: int = 0) -> None:
    global _recorder, _min_meaningful_len
    _recorder = recorder
    _min_meaningful_len = min_meaningful_len


def min_meaningful_len() -> int:
    return _min_meaningful_len


def record(**kwargs: Any) -> None:
    if _recorder is None:
        return
    _recorder(**kwargs)
