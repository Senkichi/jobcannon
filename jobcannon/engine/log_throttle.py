# PORTED from job_finder/web/log_throttle.py @ ebb9f865b13b6304615b0ba11e5e39750151de1b (private job-cannon). Ledger L-0197.
"""Rate-limited logging for high-frequency scheduled jobs.

Tracks (logger_name, message_template) pairs. After the first occurrence,
identical messages are suppressed for `cooldown_seconds` (default 3600 = 1 hour).
Suppressed messages log at DEBUG with a count of how many were suppressed.
"""

import logging
import threading
import time

_lock = threading.Lock()
_seen: dict[
    tuple[str, str], tuple[float, int]
] = {}  # (logger, msg) -> (last_logged_at, suppress_count)

DEFAULT_COOLDOWN_SECONDS = 3600  # 1 hour


def throttled_log(
    logger: logging.Logger,
    level: int,
    msg: str,
    *args,
    cooldown: int = DEFAULT_COOLDOWN_SECONDS,
    **kwargs,
) -> None:
    """Log a message, suppressing duplicates within the cooldown window.

    First occurrence always logs at the requested level. Subsequent identical
    messages within `cooldown` seconds log at DEBUG with suppression count.
    After the cooldown expires, the next occurrence logs at full level again
    with a separate suppression summary line.
    """
    key = (logger.name, msg)
    now = time.monotonic()

    with _lock:
        if key in _seen:
            last_time, count = _seen[key]
            if now - last_time < cooldown:
                # Within cooldown — suppress to DEBUG
                _seen[key] = (last_time, count + 1)
                logger.debug("[suppressed %d] %s", count + 1, msg % args if args else msg)
                return
            else:
                # Cooldown expired — log at full level, then note suppressions
                _seen[key] = (now, 0)
                logger.log(level, msg, *args, **kwargs)
                if count > 0:
                    logger.log(
                        level, "[%d identical messages suppressed in last %ds]", count, cooldown
                    )
                return

        # First occurrence
        _seen[key] = (now, 0)

    logger.log(level, msg, *args, **kwargs)
