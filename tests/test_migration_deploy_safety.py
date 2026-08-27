"""Static analysis guard over jobcannon.db.migrations.MIGRATIONS (issue #199):
fails the moment a future migration ships contract-shaped DDL against a
table/column an EARLIER migration created, or documents/contains a backfill
that assumes the old "worker deploys before web" ordering Render's pre-deploy
step now inverts.

Why this exists: `jobcannon-web`'s preDeployCommand
(`python -m jobcannon.db.migrate`, docs/deploy-runbook.md §3) runs
migrations before the new release's code goes live, while the PREVIOUS
release keeps serving requests until Render's zero-downtime cutover. Every
migration therefore has to stay backward-compatible with the outgoing
code for that overlap window -- "expand, don't contract". #197's review
manually audited m0001-m0010 for this; this guard makes that audit
automatic and mandatory for every migration that ships after it.

Every input is derived from `jobcannon.db.migrations.MIGRATIONS` -- never a
hand-maintained version list -- so a new `m00NN_*.py` file is covered the
moment it lands, with zero guard-file edits required.

## Parser: pglast (libpg_query bindings), not a hand-rolled tokenizer

PR #218's first version hand-rolled a paren/quote-depth-aware tokenizer plus
a set of anchored regexes for statement/clause shapes. Three independent
reviews (issue #199 PR history) proved that approach fails OPEN on every
ALTER TABLE clause shape it doesn't enumerate: `RENAME COLUMN`/`RENAME TO`,
`DROP CONSTRAINT`, `TRUNCATE`, `DROP INDEX`, anonymous `ADD UNIQUE`/`ADD
CHECK`, `PRIMARY KEY`/`FOREIGN KEY`/`EXCLUDE` constraints, the optional
`COLUMN` keyword, `ALTER TABLE ONLY`/`IF EXISTS`/schema-qualified/quoted
identifiers, comments (leading `--` and inline `/* */`), `DO $$ ... $$`
bodies, and multi-statement `;`-joined strings all silently passed a real
contract-shaped statement -- the opposite of this guard's whole purpose.

This version parses every `sql` string with `pglast.parse_sql` (a real
libpg_query/Postgres grammar, cp3xx-win_amd64 prebuilt wheel -- no compiler
needed, verified to round-trip every statement in the real MIGRATIONS
registry) and classifies the resulting AST nodes directly: comments,
quoting, schema-qualification, `ONLY`/`IF EXISTS`, and `;`-splitting are all
handled by the parser itself and simply cannot desync from the real syntax.

Conservative by construction, now actually enforced end-to-end: every
`AlterTableCmd.subtype` this scanner recognizes as unconditionally
expand-safe is in the small `_ALWAYS_EXPAND_SAFE_SUBTYPES` allowlist below;
every OTHER subtype -- known-dangerous, or one this scanner has no specific
rule for at all, including any future libpg_query AlterTableType this file
has never seen -- is treated as contract-shaped by default. The old
tokenizer's silent `continue`-past-anything-unmatched is gone; there is no
code path left that falls through an unrecognized DDL shape without flagging
it. The cost of a false positive is one `contract_step = True` annotation;
the cost of a false negative is a broken zero-downtime deploy.

## Documented, deliberate coverage gaps

- **`DO $$ ... $$` procedural blocks**: libpg_query treats the body as an
  opaque string (the PL/pgSQL parser is a separate, much heavier
  dependency this guard doesn't take on) -- always treated as
  contract-shaped; requires `contract_step = True` even if the DO block is
  actually expand-safe.
- **`migration.py` callable hooks**: arbitrary Python, not SQL text --
  never scanned; always treated as contract-shaped.
- **`DROP <object>` for object types other than TABLE and INDEX** (SEQUENCE,
  VIEW, TYPE, FUNCTION, ...): not scanned.
- **`ALTER COLUMN ... TYPE` widen-vs-narrow**: not distinguished (e.g.
  `integer` -> `bigint` flags the same as a narrowing) -- deliberate
  over-flag per the invariant above; use `contract_step` for a proven-widen
  case (see m0003's CHECK-widen pattern for the general escape-hatch shape).
- **`CREATE INDEX` (non-unique, non-concurrent) lock duration** on a large
  pre-existing table: explicitly out of scope here, filed as #219.
  `CREATE UNIQUE INDEX` on a pre-existing table IS scanned (it's a contract
  break, not just a lock-duration concern) and IS flagged.
"""

from __future__ import annotations

import dataclasses
import importlib
import pathlib
import re

import pglast
import pytest
from pglast import ast as pg_ast
from pglast import enums, visitors

import jobcannon.db.migrations as _migrations_pkg
from jobcannon.db.migrations import MIGRATIONS, _fold_contract_step
from jobcannon.db.migrations.types import Migration

# ---------------------------------------------------------------------------
# Schema-state tracking: walked across MIGRATIONS in registry (version)
# order so "pre-existing" means exactly what docs/deploy-runbook.md's
# discipline means -- "a table/column/constraint/index an EARLIER migration
# created" -- with no hand-maintained snapshot of the schema.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _SchemaState:
    table_created_at: dict[str, int] = dataclasses.field(default_factory=dict)
    column_created_at: dict[tuple[str, str], int] = dataclasses.field(default_factory=dict)
    constraint_created_at: dict[tuple[str, str], int] = dataclasses.field(default_factory=dict)
    index_created_at: dict[str, int] = dataclasses.field(default_factory=dict)


def _is_new_this_migration(
    table: str, col: str, version: int, state: _SchemaState, new_tables: set[str]
) -> bool:
    if table in new_tables:
        return True
    return state.column_created_at.get((table, col)) == version


class _ColumnRefCollector(visitors.Visitor):
    """Collects only genuine column references from a CHECK expression's AST
    -- a function name (`char_length(bio)`), a cast target (`::int`), or a
    keyword like ANY/ARRAY never becomes a ColumnRef node, so (unlike a
    text-token regex) this can never mistake one for a column."""

    def __init__(self):
        self.names: set[str] = set()

    def visit_ColumnRef(self, _ancestors, node):
        # `_ancestors` is required by pglast.visitors.Visitor's dispatch
        # signature (Visitor.__call__ invokes every visit_XYZ method
        # positionally as (ancestors, node)) -- unused here since a CHECK
        # expression's column references never need parent-node context.
        parts = [f.sval for f in node.fields if isinstance(f, pg_ast.String)]
        if parts:
            self.names.add(parts[-1])


def _referenced_columns(expr) -> set[str]:
    if expr is None:
        return set()
    collector = _ColumnRefCollector()
    collector(expr)
    return collector.names


def _column_has_not_null(coldef: pg_ast.ColumnDef) -> bool:
    return any(c.contype == enums.ConstrType.CONSTR_NOTNULL for c in (coldef.constraints or ()))


def _column_has_value_source(coldef: pg_ast.ColumnDef) -> bool:
    """True when an old-release INSERT that never mentions this column still
    gets a non-null value for it: an explicit DEFAULT, or an identity/
    generated column (Postgres supplies the value itself)."""
    has_default = any(
        c.contype == enums.ConstrType.CONSTR_DEFAULT for c in (coldef.constraints or ())
    )
    is_identity = coldef.identity not in (None, "\x00")
    is_generated = coldef.generated not in (None, "\x00")
    return has_default or is_identity or is_generated


# ---------------------------------------------------------------------------
# ALTER TABLE sub-command classification.
# ---------------------------------------------------------------------------

# Unconditionally expand-safe regardless of which table/column they target --
# every one of these either loosens an existing constraint, or changes
# metadata/storage/ownership the previous release's queries never depend on.
_ALWAYS_EXPAND_SAFE_SUBTYPES = frozenset(
    {
        enums.AlterTableType.AT_DropNotNull,  # loosening
        enums.AlterTableType.AT_ColumnDefault,  # SET DEFAULT / DROP DEFAULT
        enums.AlterTableType.AT_ValidateConstraint,  # validates an already-NOT-VALID constraint
        enums.AlterTableType.AT_SetStatistics,  # planner stats target
        enums.AlterTableType.AT_SetOptions,  # per-column storage options
        enums.AlterTableType.AT_ResetOptions,
        enums.AlterTableType.AT_SetStorage,
        enums.AlterTableType.AT_ChangeOwner,  # OWNER TO
        enums.AlterTableType.AT_EnableRowSecurity,
        enums.AlterTableType.AT_ForceRowSecurity,
        enums.AlterTableType.AT_SetRelOptions,  # table storage params (fillfactor, ...)
        enums.AlterTableType.AT_ResetRelOptions,
        enums.AlterTableType.AT_ReplaceRelOptions,
    }
)


def _scan_add_constraint(
    table: str,
    constraint: pg_ast.Constraint | None,
    version: int,
    state: _SchemaState,
    new_tables: set[str],
) -> list[str]:
    if constraint is None:
        return []
    if constraint.conname:
        state.constraint_created_at.setdefault((table, constraint.conname), version)

    if constraint.skip_validation:
        # NOT VALID: the standard zero-downtime pattern for adding a CHECK
        # or FOREIGN KEY constraint -- defers the lock-heavy scan of
        # EXISTING rows to a follow-up VALIDATE CONSTRAINT. NOTE: the
        # constraint is still enforced against every write (including the
        # previous release's) from the moment it's added -- NOT VALID only
        # defers proving *existing* rows comply, it does not by itself
        # guarantee the previous release's *new* writes comply. An author
        # adding a NOT VALID constraint against a pre-existing column must
        # still independently confirm that. This allowlist entry is about
        # the lock-duration hazard, not backward-write-compatibility.
        return []

    contype = constraint.contype
    if contype == enums.ConstrType.CONSTR_CHECK:
        risky = {
            col
            for col in _referenced_columns(constraint.raw_expr)
            if not _is_new_this_migration(table, col, version, state, new_tables)
        }
        if risky:
            return [
                f"{table}: ADD CONSTRAINT CHECK references pre-existing/unresolved "
                f"column(s) {sorted(risky)}"
            ]
        return []

    if contype in (enums.ConstrType.CONSTR_UNIQUE, enums.ConstrType.CONSTR_PRIMARY):
        cols = {k.sval for k in (constraint.keys or ())}
        risky = {
            col
            for col in cols
            if not _is_new_this_migration(table, col, version, state, new_tables)
        }
        if risky:
            kind = "UNIQUE" if contype == enums.ConstrType.CONSTR_UNIQUE else "PRIMARY KEY"
            return [f"{table}: ADD CONSTRAINT {kind} on pre-existing column(s) {sorted(risky)}"]
        return []

    if contype == enums.ConstrType.CONSTR_FOREIGN:
        cols = {k.sval for k in (constraint.fk_attrs or ())}
        risky = {
            col
            for col in cols
            if not _is_new_this_migration(table, col, version, state, new_tables)
        }
        if risky:
            return [
                f"{table}: ADD CONSTRAINT FOREIGN KEY on pre-existing column(s) {sorted(risky)}"
            ]
        return []

    # CONSTR_EXCLUSION and anything else: fail closed, no attempt to resolve
    # which columns are involved.
    return [f"{table}: ADD CONSTRAINT {contype.name} -- not resolvable as expand-safe"]


def _scan_alter_table(
    node: pg_ast.AlterTableStmt, version: int, state: _SchemaState, new_tables: set[str]
) -> list[str]:
    violations: list[str] = []
    table = node.relation.relname

    for cmd in node.cmds:
        subtype = cmd.subtype

        if subtype == enums.AlterTableType.AT_AddColumn:
            coldef = cmd.def_
            col = coldef.colname
            state.column_created_at.setdefault((table, col), version)
            if (
                table not in new_tables
                and _column_has_not_null(coldef)
                and not _column_has_value_source(coldef)
            ):
                violations.append(
                    f"{table}.{col}: ADD COLUMN ... NOT NULL with no value source (no "
                    f"DEFAULT, identity, or generated expression) for the previous "
                    f"release's un-migrated INSERTs"
                )
            continue

        if table in new_tables:
            continue  # nothing below can be contract-shaped against a table
            # that didn't exist before this migration

        if subtype in _ALWAYS_EXPAND_SAFE_SUBTYPES:
            continue

        if subtype == enums.AlterTableType.AT_DropColumn:
            col = cmd.name
            if not _is_new_this_migration(table, col, version, state, new_tables):
                violations.append(f"{table}.{col}: DROP COLUMN")
            continue

        if subtype == enums.AlterTableType.AT_AlterColumnType:
            col = cmd.name
            if not _is_new_this_migration(table, col, version, state, new_tables):
                violations.append(
                    f"{table}.{col}: ALTER COLUMN ... TYPE (widen vs narrow not "
                    f"distinguished -- see module docstring)"
                )
            continue

        if subtype == enums.AlterTableType.AT_SetNotNull:
            col = cmd.name
            if not _is_new_this_migration(table, col, version, state, new_tables):
                violations.append(f"{table}.{col}: ALTER COLUMN ... SET NOT NULL")
            continue

        if subtype == enums.AlterTableType.AT_DropConstraint:
            if state.constraint_created_at.get((table, cmd.name)) != version:
                violations.append(f"{table}: DROP CONSTRAINT {cmd.name}")
            continue

        if subtype == enums.AlterTableType.AT_AddConstraint:
            violations.extend(_scan_add_constraint(table, cmd.def_, version, state, new_tables))
            continue

        # Fail closed: any AlterTableType this scanner doesn't have a
        # specific rule for -- today, or a new one a future libpg_query
        # version adds -- is contract-shaped by default. There is
        # deliberately no bare `continue` left in this function for an
        # unmatched subtype.
        violations.append(
            f"{table}: unreviewed ALTER TABLE sub-command {subtype.name} -- not in "
            f"the expand-safe allowlist"
        )

    return violations


# ---------------------------------------------------------------------------
# Per-statement dispatch and the migration-level walk.
# ---------------------------------------------------------------------------


def _scan_statement(node, version: int, state: _SchemaState, new_tables: set[str]) -> list[str]:
    if isinstance(node, pg_ast.CreateStmt):
        table = node.relation.relname
        state.table_created_at.setdefault(table, version)
        new_tables.add(table)
        for elt in node.tableElts or ():
            if isinstance(elt, pg_ast.ColumnDef):
                state.column_created_at.setdefault((table, elt.colname), version)
        return []

    if isinstance(node, pg_ast.CreateTableAsStmt):
        table = node.into.rel.relname
        state.table_created_at.setdefault(table, version)
        new_tables.add(table)
        return []

    if isinstance(node, pg_ast.IndexStmt):
        table = node.relation.relname
        if node.idxname:
            state.index_created_at.setdefault(node.idxname, version)
        if node.unique and table not in new_tables:
            return [
                f"{table}: CREATE UNIQUE INDEX on pre-existing table (contract break -- "
                f"distinct from the plain-index lock-duration concern filed as #219)"
            ]
        return []

    if isinstance(node, pg_ast.DropStmt):
        violations: list[str] = []
        if node.removeType == enums.ObjectType.OBJECT_TABLE:
            for names in node.objects:
                table = names[-1].sval
                if table not in new_tables:
                    violations.append(f"DROP TABLE {table} (pre-existing/untracked table)")
        elif node.removeType == enums.ObjectType.OBJECT_INDEX:
            for names in node.objects:
                idx = names[-1].sval
                if state.index_created_at.get(idx) != version:
                    violations.append(f"DROP INDEX {idx} (not created in this migration)")
        return violations

    if isinstance(node, pg_ast.TruncateStmt):
        return [
            f"TRUNCATE {rel.relname} (pre-existing table)"
            for rel in node.relations
            if rel.relname not in new_tables
        ]

    if isinstance(node, pg_ast.RenameStmt):
        if node.renameType == enums.ObjectType.OBJECT_COLUMN:
            table = node.relation.relname
            col = node.subname
            if not _is_new_this_migration(table, col, version, state, new_tables):
                return [f"{table}.{col}: RENAME COLUMN to {node.newname!r}"]
            state.column_created_at.setdefault((table, node.newname), version)
            return []
        if node.renameType == enums.ObjectType.OBJECT_TABLE:
            table = node.relation.relname
            if table not in new_tables:
                return [f"RENAME TABLE {table} to {node.newname!r} (pre-existing table)"]
            return []
        return []  # renaming a constraint/index/trigger label: not a contract-shape hazard

    if isinstance(node, pg_ast.AlterTableStmt):
        return _scan_alter_table(node, version, state, new_tables)

    if isinstance(node, pg_ast.DoStmt):
        return [
            "DO $$ ... $$ block: opaque to this scanner (libpg_query does not parse the "
            "procedural-language body) -- always treated as contract-shaped"
        ]

    return []  # SELECT/GRANT/COMMENT/CREATE EXTENSION/plain CREATE INDEX/... out of scope


def _scan_migration(migration: Migration, state: _SchemaState) -> list[str]:
    """Contract-shaped-DDL violations for ONE migration, given the schema
    state established by every EARLIER migration. Mutates `state` with
    whatever this migration itself creates, so the caller can feed the same
    state object through MIGRATIONS in order."""
    violations: list[str] = []
    new_tables: set[str] = set()

    for stmt_sql in migration.sql:
        try:
            raw_stmts = pglast.parse_sql(stmt_sql)
        except pglast.parser.ParseError as exc:
            violations.append(f"unparseable SQL, treated as contract-shaped: {exc} -- {stmt_sql!r}")
            continue
        for raw in raw_stmts:
            violations.extend(_scan_statement(raw.stmt, migration.version, state, new_tables))

    if migration.py is not None:
        violations.append(
            "migration.py callable hook: arbitrary Python, unscannable by this guard -- "
            "always treated as contract-shaped"
        )

    return violations


# ---------------------------------------------------------------------------
# Rule 2: an inverted-deploy-order backfill (#199). Two independent signals,
# either one triggers the requirement to declare `inverted_order_safe`:
#   (a) prose: a docstring matching "Deploy order: ... AFTER" (predates
#       #197's guarantee that pre-deploy always runs first).
#   (b) structural: an UPDATE, or an INSERT ... SELECT (not a literal VALUES
#       insert), targeting a table an EARLIER migration created -- the
#       actual shape that creates a "who backfills the stragglers written by
#       the still-live previous release" hazard, independent of whether the
#       author wrote a "Deploy order" paragraph at all.
# ---------------------------------------------------------------------------

_DEPLOY_ORDER_AFTER_RE = re.compile(r"deploy order:.{0,400}?\bafter\b", re.IGNORECASE | re.DOTALL)
_STRAGGLERS_MARKER_RE = re.compile(r"^Stragglers:", re.IGNORECASE | re.MULTILINE)


def _has_backfill_against_preexisting(migration: Migration, pre_existing_tables: set[str]) -> bool:
    new_tables_this_migration: set[str] = set()
    for stmt_sql in migration.sql:
        try:
            raw_stmts = pglast.parse_sql(stmt_sql)
        except pglast.parser.ParseError:
            continue
        for raw in raw_stmts:
            node = raw.stmt
            if isinstance(node, pg_ast.CreateStmt):
                new_tables_this_migration.add(node.relation.relname)
                continue
            if isinstance(node, pg_ast.UpdateStmt) and node.relation is not None:
                table = node.relation.relname
            elif (
                isinstance(node, pg_ast.InsertStmt)
                and node.selectStmt is not None
                and not node.selectStmt.valuesLists
            ):
                table = node.relation.relname
            else:
                continue
            if table in pre_existing_tables and table not in new_tables_this_migration:
                return True
    return False


def _migrations_with_backfill_signal() -> dict[int, bool]:
    """version -> True if that migration contains an UPDATE or INSERT ...
    SELECT against a table an EARLIER migration created."""
    state = _SchemaState()
    signal: dict[int, bool] = {}
    for migration in MIGRATIONS:
        pre_existing = set(state.table_created_at)
        signal[migration.version] = _has_backfill_against_preexisting(migration, pre_existing)
        _scan_migration(migration, state)  # advance state past this migration too
    return signal


def _inverted_order_violation(
    label: str, doc: str, inverted_order_safe: bool, prose_hit: bool, structural_hit: bool
) -> str | None:
    """Pure classification, decoupled from real migration modules so it can
    be sabotage-verified with synthetic fixtures (see
    test_inverted_order_rule_fixtures below)."""
    if not (prose_hit or structural_hit):
        return None
    if not inverted_order_safe:
        reason = (
            "docstring reads 'Deploy order: ... AFTER'"
            if prose_hit
            else "contains a backfill UPDATE/INSERT...SELECT against a pre-existing table"
        )
        return (
            f"{label} {reason} -- pre-deploy migrations now always run BEFORE the new "
            f"release's code (docs/deploy-runbook.md §3) -- declare "
            f"`inverted_order_safe = True` with a docstring 'Stragglers:' line describing "
            f"what happens to rows the OLD writer creates after this migration commits, or "
            f"fix the migration to not depend on writer-first ordering"
        )
    if not _STRAGGLERS_MARKER_RE.search(doc):
        return (
            f"{label} sets inverted_order_safe = True but its docstring has no "
            f"'Stragglers:' line addressing rows the old writer creates after this "
            f"migration commits (the hazard is completeness, not idempotence)"
        )
    return None


def _all_migration_modules():
    """(migration, module) for every registry entry, in registry order.
    Re-imports each migration module (already cached in sys.modules as a
    side effect of collecting MIGRATIONS) purely to reach its docstring and
    any contract_step/inverted_order_safe module attribute -- the SET of
    migrations checked still comes entirely from MIGRATIONS, never a
    separate list."""
    for migration in MIGRATIONS:
        module = importlib.import_module(f"jobcannon.db.migrations.{migration.name}")
        yield migration, module


# ---------------------------------------------------------------------------
# The guard itself, run against the real registry.
# ---------------------------------------------------------------------------


def test_no_unjustified_contract_shaped_ddl():
    state = _SchemaState()
    failures = []
    for migration in MIGRATIONS:
        violations = _scan_migration(migration, state)
        if violations and not migration.contract_step:
            failures.append(
                f"migration {migration.version} ({migration.name}) has unjustified "
                f"contract-shaped DDL: {violations}. Declare `contract_step = True` "
                f"on the module with a docstring 'Contract justification:' section "
                f"explaining why this is safe for the previous release during "
                f"Render's zero-downtime overlap window, or change the DDL to be "
                f"expand-only."
            )
    assert not failures, "\n".join(failures)


def test_contract_step_migrations_declare_a_justification():
    failures = []
    for migration, module in _all_migration_modules():
        if not _contract_step_ok(migration.contract_step, module.__doc__ or ""):
            failures.append(
                f"migration {migration.version} ({migration.name}) sets "
                f"contract_step = True but its docstring has no "
                f"'Contract justification:' section"
            )
    assert not failures, "\n".join(failures)


def test_no_undeclared_inverted_deploy_order():
    backfill_signal = _migrations_with_backfill_signal()
    failures = []
    for migration, module in _all_migration_modules():
        doc = module.__doc__ or ""
        normalized = re.sub(r"\s+", " ", doc)
        prose_hit = bool(_DEPLOY_ORDER_AFTER_RE.search(normalized))
        structural_hit = backfill_signal.get(migration.version, False)
        failure = _inverted_order_violation(
            f"migration {migration.version} ({migration.name})",
            doc,
            getattr(module, "inverted_order_safe", False),
            prose_hit,
            structural_hit,
        )
        if failure:
            failures.append(failure)
    assert not failures, "\n".join(failures)


def _py_hook_violation(migration: Migration) -> str | None:
    """Pure classification, decoupled from real migration modules so it can
    be sabotage-verified with a synthetic fixture (see
    test_py_hook_without_contract_step_is_flagged below) -- no shipped
    migration currently sets `py=`, so a check that only ever walked the
    real MIGRATIONS registry would pass vacuously and could never prove this
    rule fires."""
    if migration.py is not None and not migration.contract_step:
        return (
            f"migration {migration.version} ({migration.name}) defines a `py` "
            f"callable hook (unscannable) but doesn't declare contract_step = True"
        )
    return None


def test_py_hook_migrations_are_contract_step():
    """migration.py callable hooks are unscannable (module docstring); the
    only way this guard can require review of one is the same contract_step
    escape hatch used for everything else it can't positively prove safe."""
    failures = [v for m in MIGRATIONS if (v := _py_hook_violation(m)) is not None]
    assert not failures, "\n".join(failures)


def test_py_hook_without_contract_step_is_flagged():
    """#218 review corroboration: zero shipped migrations set `py=` (grep
    confirms it), so test_py_hook_migrations_are_contract_step above passes
    with an empty loop against the real registry -- deleting the rule
    entirely wouldn't fail it. This synthetic fixture pins the rule
    directly, independent of whether any real migration ever uses `py=`."""
    undeclared = Migration(
        version=999998,
        description="py hook probe",
        py=lambda ctx: None,
        name="m999998_py_hook_probe",
        contract_step=False,
    )
    violation = _py_hook_violation(undeclared)
    assert violation is not None, "py hook without contract_step should be flagged"
    assert "py` callable hook" in violation

    declared_safe = dataclasses.replace(undeclared, contract_step=True)
    assert _py_hook_violation(declared_safe) is None, (
        "py hook WITH contract_step = True should not be flagged"
    )


def test_contract_step_fold_neither_source_silently_overwrites_the_other():
    """#218 review M1: jobcannon/db/migrations/__init__.py builds each
    Migration's final `contract_step` by OR-ing the module-attribute form
    (documented, preferred) with the Migration(...) kwarg form (also valid).
    Before this fix the fold used `getattr(_mod, "contract_step", False)`
    alone, so a migration author who wrote `Migration(..., contract_step=True)`
    directly (no bare module attribute) had it silently discarded back to
    False -- the exact bug this test pins. Exercises the real
    `_fold_contract_step` helper the package uses, not a re-derived copy."""
    assert _fold_contract_step(module_attr=False, migration_kwarg=True) is True
    assert _fold_contract_step(module_attr=True, migration_kwarg=False) is True
    assert _fold_contract_step(module_attr=True, migration_kwarg=True) is True
    assert _fold_contract_step(module_attr=False, migration_kwarg=False) is False


# ---------------------------------------------------------------------------
# Sabotage-verify: known-bad synthetic statements fed through the SAME
# detector, seeded with the real registry's schema history so "pre-existing"
# reflects the actual shipped shape. This is test data (a fixture list), not
# a hand-maintained production allow/deny-list.
# ---------------------------------------------------------------------------

## Each fixture pairs a known-bad statement with the substring its OWN rule
## must emit. A bare `assert violations` would also pass if the statement
## fell through to the fail-closed DEFAULT branch instead of the rule it's
## meant to pin (every AlterTableType not in the allowlist ends up flagged
## either way) -- so deleting a specific rule's branch would silently keep
## these tests green via the default. Asserting the substring instead means
## the fixture fails when its rule stops firing, even though `violations` is
## still non-empty (#218 review corroboration: assertion-strength gap).
_KNOWN_BAD_FIXTURES = [
    pytest.param(
        ["ALTER TABLE users DROP COLUMN email"], "DROP COLUMN", id="drop-column-preexisting"
    ),
    pytest.param(["DROP TABLE users"], "DROP TABLE users", id="drop-table-preexisting"),
    pytest.param(
        ["ALTER TABLE users ALTER COLUMN email TYPE varchar(50)"],
        "ALTER COLUMN ... TYPE",
        id="alter-column-type",
    ),
    pytest.param(
        ["ALTER TABLE users ADD COLUMN foo boolean NOT NULL"],
        "ADD COLUMN ... NOT NULL",
        id="add-column-not-null-without-default",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT foo_check CHECK (ats_probe_status IN ('a','b'))"],
        "ADD CONSTRAINT CHECK",
        id="add-check-constraint-preexisting-column",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT foo_uq UNIQUE (name)"],
        "ADD CONSTRAINT UNIQUE",
        id="add-unique-constraint-preexisting-column",
    ),
    pytest.param(
        ["ALTER TABLE users ALTER COLUMN email SET NOT NULL"],
        "SET NOT NULL",
        id="alter-column-set-not-null-preexisting",
    ),
    pytest.param(
        ["DROP TABLE never_created_by_any_migration"], "DROP TABLE", id="drop-table-untracked"
    ),
    pytest.param(
        ["  ALTER TABLE users DROP COLUMN email"],
        "DROP COLUMN",
        id="drop-column-preexisting-leading-whitespace",
    ),
    # --- evasions the regex/tokenizer version (PR #218 v1) fell for ---
    pytest.param(
        ["ALTER TABLE ONLY users DROP COLUMN email"], "DROP COLUMN", id="alter-table-only-keyword"
    ),
    pytest.param(
        ["ALTER TABLE IF EXISTS users DROP COLUMN email"], "DROP COLUMN", id="alter-table-if-exists"
    ),
    pytest.param(
        ["ALTER TABLE public.users DROP COLUMN email"], "DROP COLUMN", id="schema-qualified-alter"
    ),
    pytest.param(['ALTER TABLE "users" DROP COLUMN email'], "DROP COLUMN", id="quoted-table-alter"),
    pytest.param(['DROP TABLE "users"'], "DROP TABLE", id="quoted-table-drop"),
    pytest.param(
        ["ALTER TABLE users DROP email"], "DROP COLUMN", id="drop-column-no-column-keyword"
    ),
    pytest.param(
        ["ALTER TABLE users ADD foo boolean NOT NULL"],
        "ADD COLUMN ... NOT NULL",
        id="add-column-no-column-keyword",
    ),
    pytest.param(
        ["ALTER TABLE users RENAME COLUMN email TO mail"],
        "RENAME COLUMN",
        id="rename-column-preexisting",
    ),
    pytest.param(
        ["ALTER TABLE users RENAME TO members"], "RENAME TABLE", id="rename-table-preexisting"
    ),
    pytest.param(
        ["ALTER TABLE companies DROP CONSTRAINT companies_ats_probe_status_check"],
        "DROP CONSTRAINT",
        id="drop-constraint-preexisting",
    ),
    pytest.param(["TRUNCATE users"], "TRUNCATE", id="truncate-preexisting-table"),
    pytest.param(["DROP INDEX some_untracked_idx"], "DROP INDEX", id="drop-index-untracked"),
    pytest.param(
        ["ALTER TABLE users ADD UNIQUE (email)"], "ADD CONSTRAINT UNIQUE", id="anonymous-add-unique"
    ),
    pytest.param(
        ["ALTER TABLE users ADD CHECK (email IS NOT NULL)"],
        "ADD CONSTRAINT CHECK",
        id="anonymous-add-check",
    ),
    pytest.param(
        ["ALTER TABLE users ADD CONSTRAINT pk1 PRIMARY KEY (email)"],
        "ADD CONSTRAINT PRIMARY KEY",
        id="add-primary-key-preexisting",
    ),
    pytest.param(
        ["ALTER TABLE users ADD CONSTRAINT fk1 FOREIGN KEY (email) REFERENCES companies(name)"],
        "ADD CONSTRAINT FOREIGN KEY",
        id="add-foreign-key-preexisting-validated",
    ),
    pytest.param(
        ["ALTER TABLE users ADD CONSTRAINT ex1 EXCLUDE USING gist (email WITH =)"],
        "not resolvable as expand-safe",
        id="add-exclude-constraint",
    ),
    pytest.param(
        ["-- comment\nALTER TABLE users DROP COLUMN email"],
        "DROP COLUMN",
        id="leading-comment-evasion",
    ),
    pytest.param(
        ["ALTER TABLE users DROP /* x */ COLUMN email"], "DROP COLUMN", id="inline-comment-evasion"
    ),
    pytest.param(
        ["DO $$ BEGIN ALTER TABLE users DROP COLUMN email; END $$;"],
        "opaque to this scanner",
        id="do-block-hides-drop",
    ),
    pytest.param(
        # First statement is harmless on its own (a nullable ADD COLUMN);
        # only the SECOND, semicolon-joined statement in this one `sql`
        # list entry is the violation -- proves `;`-splitting reaches past
        # the first statement rather than merely re-detecting a violation
        # that would have flagged anyway.
        ["ALTER TABLE users ADD COLUMN throwaway_zzz text; ALTER TABLE users DROP COLUMN email;"],
        "DROP COLUMN",
        id="multi-statement-second-statement-caught",
    ),
    pytest.param(
        ["CREATE UNIQUE INDEX ON companies(name)"],
        "CREATE UNIQUE INDEX",
        id="create-unique-index-preexisting-table",
    ),
    # Proves the fail-closed DEFAULT branch itself, not any specific rule:
    # AT_SetTableSpace has no dedicated handler and isn't in
    # _ALWAYS_EXPAND_SAFE_SUBTYPES, so it can only be caught by the bottom
    # "unreviewed ALTER TABLE sub-command" fallthrough.
    pytest.param(
        ["ALTER TABLE users SET TABLESPACE pg_default"],
        "unreviewed ALTER TABLE sub-command",
        id="unrecognized-subtype-fail-closed-default",
    ),
]


@pytest.mark.parametrize("sql_statements,expected_substring", _KNOWN_BAD_FIXTURES)
def test_sabotage_fixtures_are_all_detected(sql_statements, expected_substring):
    state = _SchemaState()
    for migration in MIGRATIONS:
        _scan_migration(migration, state)  # seed state with the real schema history

    probe = Migration(
        version=999999,
        description="sabotage probe",
        sql=sql_statements,
        name="m999999_sabotage",
    )
    violations = _scan_migration(probe, state)
    assert violations, f"scanner did not flag known-bad statement(s) {sql_statements!r}"
    joined = "\n".join(violations)
    assert expected_substring in joined, (
        f"{sql_statements!r} was flagged, but not by its OWN rule -- expected "
        f"{expected_substring!r} in {violations!r}. This usually means the specific "
        f"rule branch is gone and the fail-closed default caught it instead, which "
        f"would let this fixture stay green even with the rule deleted."
    )


# ---------------------------------------------------------------------------
# Known-safe shapes must NOT be flagged (negative control for the positive
# assertions above -- a scanner that flags everything would pass the
# sabotage fixtures for the wrong reason).
# ---------------------------------------------------------------------------

_KNOWN_SAFE_FIXTURES = [
    pytest.param(["ALTER TABLE users ADD COLUMN bio text"], id="add-nullable-column"),
    pytest.param(
        ["ALTER TABLE users ADD COLUMN plan_tier2 text NOT NULL DEFAULT 'free'"],
        id="add-not-null-column-with-default",
    ),
    pytest.param(
        [
            "ALTER TABLE users ADD COLUMN comp_floor_usd integer",
            "ALTER TABLE users ADD CONSTRAINT users_comp_floor_usd_nonneg "
            "CHECK (comp_floor_usd IS NULL OR comp_floor_usd >= 0)",
        ],
        id="check-constraint-on-column-added-same-migration",
    ),
    pytest.param(
        ["CREATE TABLE t_new (id bigserial PRIMARY KEY, x text NOT NULL)"], id="new-table"
    ),
    # --- H2b fix: function/cast/ANY-ARRAY in a same-migration CHECK must
    # not false-flag (the old text-token regex mistook the function/cast
    # name for a column reference; the AST-based ColumnRef walk can't). ---
    pytest.param(
        [
            "ALTER TABLE users ADD COLUMN bio text",
            "ALTER TABLE users ADD CONSTRAINT bio_len_chk CHECK (char_length(bio) > 0)",
        ],
        id="check-with-function-call-on-new-column",
    ),
    pytest.param(
        [
            "ALTER TABLE users ADD COLUMN meta jsonb",
            "ALTER TABLE users ADD CONSTRAINT meta_x_chk CHECK ((meta ->> 'x')::int > 0)",
        ],
        id="check-with-cast-on-new-column",
    ),
    pytest.param(
        [
            "ALTER TABLE users ADD COLUMN kind text",
            "ALTER TABLE users ADD CONSTRAINT kind_chk CHECK (kind = ANY(ARRAY['a','b']))",
        ],
        id="check-with-any-array-on-new-column",
    ),
    # --- F6 fix: CREATE TABLE ... AS / quoted CREATE names must be tracked
    # into new_tables so a same-migration ALTER on them isn't false-flagged.
    pytest.param(
        [
            "CREATE TABLE report_cache AS SELECT 1 AS score",
            "ALTER TABLE report_cache ADD COLUMN label text NOT NULL DEFAULT ''",
        ],
        id="create-table-as-then-alter-same-migration",
    ),
    pytest.param(
        ['CREATE TABLE "Weird" (id bigserial PRIMARY KEY)'],
        id="quoted-create-table-name-tracked",
    ),
    # --- allowlisted expand-safe ALTER shapes ---
    pytest.param(["ALTER TABLE users ALTER COLUMN email DROP NOT NULL"], id="drop-not-null-safe"),
    pytest.param(["ALTER TABLE users ALTER COLUMN email SET DEFAULT 'x'"], id="set-default-safe"),
    pytest.param(["ALTER TABLE users ALTER COLUMN email DROP DEFAULT"], id="drop-default-safe"),
    pytest.param(
        ["ALTER TABLE users VALIDATE CONSTRAINT companies_pkey"], id="validate-constraint-safe"
    ),
    pytest.param(
        ["ALTER TABLE users ALTER COLUMN email SET STATISTICS 100"], id="set-statistics-safe"
    ),
    pytest.param(["ALTER TABLE users OWNER TO someone"], id="owner-to-safe"),
    pytest.param(["ALTER TABLE users ENABLE ROW LEVEL SECURITY"], id="enable-rls-safe"),
    pytest.param(["ALTER TABLE users FORCE ROW LEVEL SECURITY"], id="force-rls-safe"),
    pytest.param(["ALTER TABLE users SET (fillfactor = 70)"], id="storage-param-safe"),
    pytest.param(
        [
            "ALTER TABLE users ADD CONSTRAINT fk_safe FOREIGN KEY (email) "
            "REFERENCES companies(name) NOT VALID"
        ],
        id="add-constraint-not-valid-safe",
    ),
    # plain (non-unique) CREATE INDEX on a pre-existing table: out of scope,
    # filed as #219 -- must not be flagged here.
    pytest.param(["CREATE INDEX ON companies(name)"], id="plain-create-index-out-of-scope"),
    # DROP CONSTRAINT on a constraint THIS migration itself added: safe
    # (mirrors the new_tables exemption; tracked via constraint_created_at).
    # NOTE: no equivalent "CREATE UNIQUE INDEX then DROP INDEX in the same
    # migration" fixture -- that pattern IS safe in practice (both
    # statements run in the same migration transaction, so the index is
    # never visible outside it), but this scanner doesn't special-case it;
    # CREATE UNIQUE INDEX on a pre-existing table is flagged unconditionally
    # per the module docstring, and that's the correct conservative default
    # for a pattern no real migration in this repo uses.
    pytest.param(
        [
            "ALTER TABLE users ADD CONSTRAINT tmp_chk CHECK (true)",
            "ALTER TABLE users DROP CONSTRAINT tmp_chk",
        ],
        id="add-then-drop-constraint-same-migration",
    ),
]


@pytest.mark.parametrize("sql_statements", _KNOWN_SAFE_FIXTURES)
def test_known_safe_shapes_are_not_flagged(sql_statements):
    state = _SchemaState()
    for migration in MIGRATIONS:
        _scan_migration(migration, state)

    probe = Migration(
        version=999998,
        description="safe probe",
        sql=sql_statements,
        name="m999998_safe",
    )
    violations = _scan_migration(probe, state)
    assert not violations, f"scanner false-flagged known-safe statement(s): {violations}"


# ---------------------------------------------------------------------------
# Rule 2 (inverted deploy order) sabotage fixtures, decoupled from real
# migration modules -- refuter-3's Finding 1: the real registry only ever
# exercises the "already declared correctly" path (m0010), so neutering the
# detection regex or the justification check left 17/17 green. These call
# the pure classifier directly with synthetic doc/flag combinations.
# ---------------------------------------------------------------------------

_INVERTED_ORDER_FIXTURES = [
    pytest.param(
        "Deploy order: run this AFTER the writer deploys.",
        False,
        True,
        False,
        id="prose-undeclared",
    ),
    pytest.param(
        "UPDATE-based backfill, no Deploy order paragraph.",
        False,
        False,
        True,
        id="structural-undeclared",
    ),
    pytest.param(
        "Deploy order: run this AFTER the writer deploys.\nStragglers: none, no-op UPDATE.",
        True,
        True,
        False,
        id="declared-with-stragglers-ok",
    ),
    pytest.param(
        "Deploy order: run this AFTER the writer deploys.\nThis UPDATE is idempotent.",
        True,
        True,
        False,
        id="declared-idempotent-keyword-alone-not-enough",
    ),
]


@pytest.mark.parametrize(
    "doc,inverted_order_safe,prose_hit,structural_hit", _INVERTED_ORDER_FIXTURES
)
def test_inverted_order_rule_fixtures(doc, inverted_order_safe, prose_hit, structural_hit):
    failure = _inverted_order_violation(
        "probe", doc, inverted_order_safe, prose_hit, structural_hit
    )
    if inverted_order_safe and _STRAGGLERS_MARKER_RE.search(doc):
        assert failure is None, f"known-good fixture was flagged: {failure!r}"
    else:
        assert failure is not None, "scanner did not flag a known-bad inverted-order fixture"


def test_backfill_structural_signal_detects_update_against_preexisting_table():
    """The pure-classifier fixtures above pass `structural_hit` in directly,
    which decouples them from whether `_has_backfill_against_preexisting`
    itself actually computes that signal correctly -- exercise the real
    function here, on a synthetic Migration, independent of the real
    registry (where m0010 already satisfies Rule 2 via the prose signal
    alone, so a neutered structural detector would go uncaught -- the exact
    class of gap refuter-3 found in Rule 2's original zero negative
    coverage)."""
    pre_existing = {"events"}
    probe = Migration(
        version=999997,
        description="structural probe",
        sql=["UPDATE events SET payload = payload WHERE true"],
        name="m999997_structural_probe",
    )
    assert _has_backfill_against_preexisting(probe, pre_existing)


def test_backfill_structural_signal_ignores_values_insert_and_same_migration_table():
    pre_existing = {"events"}
    safe_probe = Migration(
        version=999996,
        description="safe structural probe",
        sql=[
            "CREATE TABLE brand_new (id int)",
            "UPDATE brand_new SET id = 1",  # same-migration table: not a straggler hazard
            "INSERT INTO events (payload) VALUES ('{}')",  # literal VALUES, not a SELECT backfill
        ],
        name="m999996_structural_safe",
    )
    assert not _has_backfill_against_preexisting(safe_probe, pre_existing)


def _contract_step_ok(contract_step: bool, doc: str) -> bool:
    """Same predicate test_contract_step_migrations_declare_a_justification
    applies per real module -- pulled out so it can be sabotage-verified
    with synthetic (contract_step, doc) pairs, independent of m0003
    happening to already satisfy it (refuter-3 Finding 3)."""
    return not (contract_step and "Contract justification:" not in doc)


def test_contract_step_justification_rule_fixtures():
    assert not _contract_step_ok(True, "no justification here")
    assert not _contract_step_ok(True, "")
    assert _contract_step_ok(True, "Contract justification: widen-only CHECK, see above")
    assert _contract_step_ok(False, "")


# ---------------------------------------------------------------------------
# Registry sanity: prove the guard is actually exercising the real registry,
# not an empty/stale import. Derived from an independent on-disk file count
# (glob), never a hand-maintained version list -- a single migration file
# silently dropped by a registry-discovery bug fails this, not just a fully
# empty registry (issue #199 review Finding 5 / Devin LOW-2).
# ---------------------------------------------------------------------------


def test_registry_has_no_hand_maintained_stand_in():
    migrations_module_file = _migrations_pkg.__file__
    assert migrations_module_file is not None, (
        "jobcannon.db.migrations.__file__ is None -- unexpected for a real "
        "on-disk package (only namespace/built-in modules lack it) -- can't "
        "cross-check the registry against migration files on disk"
    )
    migrations_dir = pathlib.Path(migrations_module_file).parent
    on_disk = sorted(p.stem for p in migrations_dir.glob("m[0-9]*_*.py"))
    registry_names = sorted(m.name for m in MIGRATIONS)
    assert len(MIGRATIONS) >= 9
    assert registry_names == on_disk, (
        f"MIGRATIONS registry doesn't match migration files on disk: "
        f"registry={registry_names} disk={on_disk}"
    )
