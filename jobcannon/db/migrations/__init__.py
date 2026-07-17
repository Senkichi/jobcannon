"""Migration discovery: every m*.py module in this package exports MIGRATION;
this collects them (sorted by version) and injects name= from the filename."""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import re

from jobcannon.db.migrations.types import Migration

MIGRATIONS: list[Migration] = []

for _info in pkgutil.iter_modules(__path__):
    if not re.match(r"^m\d+_", _info.name):
        continue
    _mod = importlib.import_module(f"{__name__}.{_info.name}")
    _mig = dataclasses.replace(_mod.MIGRATION, name=_info.name)
    MIGRATIONS.append(_mig)

MIGRATIONS.sort(key=lambda m: m.version)

_versions = [m.version for m in MIGRATIONS]
if len(_versions) != len(set(_versions)):
    raise RuntimeError(f"duplicate migration versions: {_versions}")
