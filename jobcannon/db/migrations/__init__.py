"""Migration discovery: every m*.py module in this package exports MIGRATION;
this collects them (sorted by version) and injects name= from the filename."""

from __future__ import annotations

import dataclasses
import importlib
import pkgutil
import re

from jobcannon.db.migrations.types import Migration


def _fold_contract_step(module_attr: bool, migration_kwarg: bool) -> bool:
    """contract_step is DOCUMENTED as a bare module attribute (never a
    Migration(...) kwarg in the migration file itself) so it lives next to
    the module's "Contract justification:" docstring section that
    tests/test_migration_deploy_safety.py requires alongside it. But
    `Migration.contract_step` is still a real, settable dataclass field --
    `Migration(..., contract_step=True)` is valid Python a future author
    could reasonably write. OR the two sources (rather than letting the
    module-attribute default of False unconditionally win) so that kwarg is
    never silently discarded (#218 review M1): whichever source declared it
    True wins. This is the single place that folds either source onto the
    dataclass -- extracted as a pure function so the fold itself has direct
    unit coverage (tests/test_migration_deploy_safety.py) independent of the
    package's import-time module-discovery side effect."""
    return module_attr or migration_kwarg


def _fold_lock_step(module_attr: bool, migration_kwarg: bool) -> bool:
    """Same fold shape as _fold_contract_step, for the same reason: lock_step
    is DOCUMENTED as a bare module attribute (next to the "Lock
    justification:" docstring section tests/test_migration_deploy_safety.py
    requires alongside it), but the dataclass field is still directly
    settable, so this ORs both sources rather than letting the module-
    attribute default silently win over an explicit kwarg (issue #219)."""
    return module_attr or migration_kwarg


def _fold_autocommit(module_attr: bool, migration_kwarg: bool) -> bool:
    """Same fold shape as _fold_contract_step/_fold_lock_step, for the same
    reason: autocommit is DOCUMENTED as a bare module attribute, but the
    dataclass field is still directly settable, so this ORs both sources
    (issue #219)."""
    return module_attr or migration_kwarg


MIGRATIONS: list[Migration] = []

for _info in pkgutil.iter_modules(__path__):
    if not re.match(r"^m\d+_", _info.name):
        continue
    _mod = importlib.import_module(f"{__name__}.{_info.name}")
    _mig = dataclasses.replace(
        _mod.MIGRATION,
        name=_info.name,
        contract_step=_fold_contract_step(
            getattr(_mod, "contract_step", False), _mod.MIGRATION.contract_step
        ),
        lock_step=_fold_lock_step(getattr(_mod, "lock_step", False), _mod.MIGRATION.lock_step),
        autocommit=_fold_autocommit(getattr(_mod, "autocommit", False), _mod.MIGRATION.autocommit),
    )
    MIGRATIONS.append(_mig)

MIGRATIONS.sort(key=lambda m: m.version)

_versions = [m.version for m in MIGRATIONS]
if len(_versions) != len(set(_versions)):
    raise RuntimeError(f"duplicate migration versions: {_versions}")
