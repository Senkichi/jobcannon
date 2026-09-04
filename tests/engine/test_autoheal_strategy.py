# PORTED from tests/test_autoheal_strategy.py @ 1b3b47b304f8af1791c12ed5d9462899ee98f321 (private job-cannon). Ledger L-0551.
"""Tests for the Strategy/Extractor chain (job_finder/parsers/_strategy.py)."""

from jobcannon.engine.email_parsers._strategy import Extractor


def test_first_nonempty_wins() -> None:
    """The first strategy that returns a non-empty list wins; later ones are skipped."""
    ex = Extractor([lambda raw: [], lambda raw: ["a"], lambda raw: ["b"]])
    assert ex.run("x") == ["a"]


def test_all_empty_returns_empty() -> None:
    """If every strategy returns an empty list, run() returns []."""
    ex = Extractor([lambda raw: [], lambda raw: []])
    assert ex.run("x") == []


def test_strategy_exception_falls_through() -> None:
    """A strategy that raises is skipped; the next strategy is tried."""

    def boom(raw):
        raise ValueError("nope")

    ex = Extractor([boom, lambda raw: ["ok"]])
    assert ex.run("x") == ["ok"]


def test_all_strategies_raise_returns_empty() -> None:
    """If every strategy raises, run() returns []."""

    def boom(raw):
        raise RuntimeError("fail")

    ex = Extractor([boom, boom])
    assert ex.run("anything") == []


def test_single_strategy_returning_value() -> None:
    """A single strategy that returns a result is used."""
    ex = Extractor([lambda raw: [1, 2, 3]])
    assert ex.run("body") == [1, 2, 3]


def test_empty_strategy_list_returns_empty() -> None:
    """No strategies ⇒ always returns []."""
    ex = Extractor([])
    assert ex.run("body") == []
