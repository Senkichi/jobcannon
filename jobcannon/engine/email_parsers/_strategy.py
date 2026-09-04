# PORTED from job_finder/parsers/_strategy.py @ 7405a93c8003fdf8a74f8be383cac457c0056803 (private job-cannon). Ledger L-0460.
"""Ordered extraction-strategy chain for email parsers.

First strategy to return a non-empty list wins; a strategy that raises is
skipped (with a DEBUG log) and execution falls through to the next.

Phase B introduces this for the email surface (primary parser + positional
fallback). A future phase may extend it to ATS/careers surfaces.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

#: A strategy is any callable that accepts a raw input and returns a list.
#: An empty list means "nothing found / not recognised".
Strategy = Callable[[object], list]


class Extractor:
    """Run an ordered chain of strategies; return the first non-empty result.

    Strategies are tried in order. If a strategy raises an exception it is
    logged at DEBUG level and the next strategy is tried. If all strategies
    return empty lists, ``run`` returns ``[]``.
    """

    def __init__(self, strategies: Sequence[Strategy]) -> None:
        self._strategies = list(strategies)

    def run(self, raw: object) -> list:
        """Run each strategy in order; return the first non-empty result.

        Args:
            raw: The input passed to each strategy (typically an email body).

        Returns:
            The first non-empty list returned by a strategy, or ``[]`` if all
            strategies return empty or raise.
        """
        for strat in self._strategies:
            try:
                result = strat(raw)
            except Exception:
                logger.debug("strategy %r raised; falling through", strat, exc_info=True)
                continue
            if result:
                return result
        return []
