"""HybridRow: sqlite3.Row-compatible row for psycopg.

Engine code reads factory-connection rows BOTH ways: row["col"]
(stale_detector, source_registry predicates) and row[0]
(ats_scanner/_run.py's jd_full read-back). No stock psycopg row factory
supports both, so this one does — modeled on sqlite3.Row's contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class HybridRow(Sequence):
    __slots__ = ("_names", "_values", "_index")

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        self._names = tuple(names)
        self._values = tuple(values)
        self._index = {n: i for i, n in enumerate(self._names)}

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def __len__(self) -> int:
        return len(self._values)

    def keys(self):
        return list(self._names)

    def __repr__(self) -> str:
        return f"HybridRow({dict(zip(self._names, self._values))!r})"


def hybrid_row(cursor):
    """psycopg row factory producing HybridRow instances."""
    names = [c.name for c in cursor.description] if cursor.description else []

    def make_row(values):
        return HybridRow(names, values)

    return make_row
