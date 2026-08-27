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
    _mig = dataclasses.replace(
        _mod.MIGRATION,
        name=_info.name,
        # contract_step is a bare module attribute (never a Migration(...)
        # kwarg in the migration file itself) so it lives next to the
        # module's "Contract justification:" docstring section that
        # tests/test_migration_deploy_safety.py requires alongside it. This
        # is the single place that reads it and folds it onto the dataclass.
        contract_step=getattr(_mod, "contract_step", False),
    )
    MIGRATIONS.append(_mig)

MIGRATIONS.sort(key=lambda m: m.version)

_versions = [m.version for m in MIGRATIONS]
if len(_versions) != len(set(_versions)):
    raise RuntimeError(f"duplicate migration versions: {_versions}")
