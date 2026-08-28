"""Static analysis guard over jobcannon.db.migrations.MIGRATIONS (issues #199,
#219): fails the moment a future migration ships contract-shaped DDL against a
table/column an EARLIER migration created, documents/contains a backfill
that assumes the old "worker deploys before web" ordering Render's pre-deploy
step now inverts, or ships a non-CONCURRENT `CREATE INDEX` against a
pre-existing table without explicitly accepting the lock-duration risk.

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

Conservative by construction, now actually enforced end-to-end for Rule 1's
contract-shape hazard: every `AlterTableCmd.subtype` this scanner recognizes
as unconditionally expand-safe is in the small `_ALWAYS_EXPAND_SAFE_SUBTYPES`
allowlist below; every OTHER subtype -- known-dangerous, or one this scanner
has no specific rule for at all, including any future libpg_query
AlterTableType this file has never seen -- is treated as contract-shaped by
default. The old tokenizer's silent `continue`-past-anything-unmatched is
gone; there is no code path left that falls through an unrecognized
**`ALTER TABLE` sub-command** without flagging it (this guarantee is scoped
to `AlterTableCmd.subtype` specifically -- a top-level statement kind Rule 1
has no rule for at all, e.g. `REINDEX`/`CLUSTER`, still falls through
`_scan_statement`'s final `return []` for the CONTRACT-shape check, because
they change no schema/data compatibility; their lock-DURATION hazard is a
separate, narrower concern Rule 3 covers explicitly below -- see A4 in the
Rule 3 section). The cost of a false positive is one `contract_step = True`
annotation; the cost of a false negative is a broken zero-downtime deploy.

## Documented, deliberate coverage gaps

- **`DO $$ ... $$` procedural blocks**: libpg_query treats the body as an
  opaque string (the PL/pgSQL parser is a separate, much heavier
  dependency this guard doesn't take on) -- always treated as BOTH
  contract-shaped (Rule 1, requires `contract_step = True`) AND
  lock-duration-risky (Rule 3, requires `lock_step = True`) even if the DO
  block is actually safe on both counts -- this guard has no way to prove
  that from an opaque body.
- **`migration.py` callable hooks**: arbitrary Python, not SQL text --
  never scanned; always treated as contract-shaped.
- **`DROP <object>` for object types other than TABLE and INDEX** (SEQUENCE,
  VIEW, TYPE, FUNCTION, ...): not scanned.
- **`ALTER COLUMN ... TYPE` widen-vs-narrow**: not distinguished (e.g.
  `integer` -> `bigint` flags the same as a narrowing) -- deliberate
  over-flag per the invariant above; use `contract_step` for a proven-widen
  case (see m0003's CHECK-widen pattern for the general escape-hatch shape).
- **`CREATE INDEX` (non-unique, non-concurrent) lock duration** on a
  pre-existing table: now covered by Rule 3 below (issue #219) -- flagged
  unless the migration declares `lock_step = True` with a docstring "Lock
  justification:" section naming the expected row count / build time.
  `CREATE UNIQUE INDEX` on a pre-existing table is a SEPARATE, independent
  violation (Rule 1's contract break, gated on `contract_step`) -- the two
  hazards don't imply each other (a unique index build can finish in
  milliseconds and still break the previous release's duplicate inserts; a
  slow non-unique build can hold a lock for hours without breaking anything
  about the previous release's queries), so a migration that genuinely needs
  both accepted risks declares both flags.
- **Rule 3 (lock duration, #219)**: a non-CONCURRENT `CREATE`/`CREATE UNIQUE
  INDEX` against a table an EARLIER migration created holds a SHARE lock for
  the whole build, blocking writes to that table while the previous
  release's web instance keeps serving them during the deploy overlap
  window. Gated on `lock_step` (same fold-pattern/docstring-marker shape as
  `contract_step`). An index on a table THIS SAME migration creates is
  exempt -- the table is empty at that point in the deploy, so the build is
  instant regardless of lock kind. Also covers every OTHER shape that
  implicitly builds or rebuilds an index under a full-table lock, so the
  hazard can't be routed around the `IndexStmt` check by using a different
  statement shape: `ALTER TABLE ... ADD CONSTRAINT UNIQUE`/`PRIMARY
  KEY`/`EXCLUDE` and an inline `ADD COLUMN ... UNIQUE`/`PRIMARY KEY` (exempt
  when the constraint attaches an already-built index via
  `USING INDEX <name>` instead of building a new one -- the standard
  zero-downtime pattern), `DO $$ ... $$` blocks (opaque, always flagged),
  and top-level `REINDEX`/`CLUSTER` without `CONCURRENTLY` (`REINDEX` SCHEMA/
  DATABASE/SYSTEM forms and `REINDEX INDEX`, which names an index rather
  than a table this scanner can resolve, are flagged unconditionally;
  `CLUSTER` has no `CONCURRENTLY` option at all and a bare `CLUSTER` with no
  table name is flagged unconditionally too, since it re-clusters every
  previously-clustered table in the database). `CREATE TABLE IF NOT EXISTS`
  of a table an EARLIER migration already created does NOT earn the
  same-migration exemption for anything after it -- only a genuinely new
  table (one `pre_existing_tables` doesn't already contain) does.
- **Rule 4 (autocommit escape hatch, #219)**: keeps the alternative --
  `CREATE INDEX CONCURRENTLY` on an `autocommit = True` migration, run
  outside the ledger transaction (jobcannon/db/migrate.py) -- narrow.
  Enforced as a biconditional (a CONCURRENTLY statement always requires
  `autocommit = True` and vice versa), plus: an autocommit migration's `sql`
  may contain ONLY CREATE/DROP INDEX statements (no `py` hook, no other
  DDL), and retry-idempotency is required on EVERY index statement in an
  autocommit migration, not just the CONCURRENTLY ones: every
  `CREATE INDEX ... CONCURRENTLY` must use `IF NOT EXISTS`, every plain
  `CREATE INDEX` (no `CONCURRENTLY`) must ALSO use `IF NOT EXISTS`, and every
  `DROP INDEX` must use `IF EXISTS` -- a statement earlier in the same
  autocommit migration can already have committed (autocommit runs each
  statement outside a transaction) before a LATER statement fails, so a
  retry must re-run every one of them as a safe no-op, not just the
  CONCURRENTLY build (migrate.py's `_apply_autocommit_migration` drops any
  INVALID leftover index from a previously failed CONCURRENTLY build before
  retrying, which is what makes a bare `IF NOT EXISTS` retry a safe no-op
  instead of the silent-skip-of-an-unusable-index hazard #219 raised).
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
from jobcannon.db.migrations import (
    MIGRATIONS,
    _fold_autocommit,
    _fold_contract_step,
    _fold_lock_step,
)
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
        # A2: an IF NOT EXISTS create of a table an EARLIER migration already
        # created (state.table_created_at already has it) must NOT earn the
        # same-migration exemption -- only a genuinely new table does. Before
        # this fix, new_tables.add(table) ran unconditionally, so `CREATE
        # TABLE IF NOT EXISTS <pre-existing>` shielded every later statement
        # in the same migration from this function's checks below.
        is_new_table = table not in state.table_created_at
        state.table_created_at.setdefault(table, version)
        if is_new_table:
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
# Rule 3: non-CONCURRENT CREATE INDEX lock duration (#219). Independent
# pglast walk, decoupled from _scan_migration/_scan_statement above so this
# orthogonal hazard (lock duration) can never be silently absorbed by the
# contract_step escape hatch that gates Rule 1 -- a migration can be
# lock_step without being contract_step and vice versa (see the module
# docstring's Rule 3 note on why CREATE UNIQUE INDEX needs BOTH flags if
# it's ever accepted). Same walk-in-registry-order shape as Rule 2's
# _migrations_with_backfill_signal so "pre-existing" tracks the real schema
# history exactly.
#
# Review-filed follow-ups (#219 wave-4 fix, three-refuter HIGH-tier review):
#   A1: an implicit index build hides inside ALTER TABLE too -- ADD
#       CONSTRAINT UNIQUE/PRIMARY KEY/EXCLUDE, and an inline ADD COLUMN ...
#       UNIQUE/PRIMARY KEY, each build a non-CONCURRENT index under a full
#       table lock exactly like a bare CREATE INDEX. `ADD CONSTRAINT ...
#       UNIQUE USING INDEX <name>` is the one exempt shape -- it attaches an
#       already-built (presumably CONCURRENTLY-built) index instead of
#       building a new one, so it's the standard zero-downtime pattern, not
#       a hazard.
#   A2: `CREATE TABLE IF NOT EXISTS <name-that-already-exists>` must NOT
#       register `<name>` as "new this migration" -- only a genuinely new
#       table (not already in `pre_existing_tables`) earns the same-
#       migration exemption. Before this fix an IF-NOT-EXISTS create of a
#       pre-existing table name shielded every later statement in the same
#       migration from this rule.
#   A3: a `DO $$ ... $$` block is opaque to libpg_query -- mirrors Rule 1's
#       treatment (module docstring, "Documented, deliberate coverage
#       gaps"): always flagged, fail-closed, since this scanner has no way
#       to prove the hidden body doesn't build an index.
#   A4: top-level `REINDEX`/`CLUSTER` (without CONCURRENTLY) take the same
#       kind of full-table lock as a non-CONCURRENT CREATE INDEX and
#       previously fell through _scan_statement's final `return []`
#       unflagged by either rule -- covered here as lock hazards.
# ---------------------------------------------------------------------------

# Human-readable label per hazardous contype, doubling as the membership set
# (its keys) -- AT_AddConstraint can carry any of the three (a table-level
# ADD CONSTRAINT), AT_AddColumn only the two an inline column definition can
# actually spell (`col type UNIQUE` / `col type PRIMARY KEY` -- EXCLUDE has
# no inline column-constraint syntax in Postgres, only table-level).
_CONSTRAINT_LOCK_HAZARD_LABELS = {
    enums.ConstrType.CONSTR_UNIQUE: "UNIQUE",
    enums.ConstrType.CONSTR_PRIMARY: "PRIMARY KEY",
    enums.ConstrType.CONSTR_EXCLUSION: "EXCLUDE",
}


def _alter_table_index_lock_violations(
    node: pg_ast.AlterTableStmt,
    pre_existing_tables: set[str],
    new_tables_this_migration: set[str],
) -> list[str]:
    """A1: ALTER TABLE shapes that implicitly build a non-CONCURRENT index
    (ADD CONSTRAINT UNIQUE/PRIMARY KEY/EXCLUDE, inline ADD COLUMN ...
    UNIQUE/PRIMARY KEY) on a table an EARLIER migration created."""
    table = node.relation.relname
    if table not in pre_existing_tables or table in new_tables_this_migration:
        return []  # same-migration table: empty, build is instant regardless of lock kind
    violations: list[str] = []
    for cmd in node.cmds:
        if cmd.subtype == enums.AlterTableType.AT_AddConstraint:
            constraint = cmd.def_
            label = _CONSTRAINT_LOCK_HAZARD_LABELS.get(constraint.contype) if constraint else None
            if label and not constraint.indexname:  # USING INDEX: attaches a pre-built
                # index, builds nothing new -- the standard zero-downtime pattern.
                violations.append(
                    f"{table}: ADD CONSTRAINT {label} implicitly builds a non-CONCURRENT "
                    f"index on a pre-existing table -- holds an ACCESS EXCLUSIVE lock for "
                    f"the whole build (issue #219); build the index CONCURRENTLY first and "
                    f"use ADD CONSTRAINT ... USING INDEX, or declare lock_step"
                )
        elif cmd.subtype == enums.AlterTableType.AT_AddColumn:
            coldef = cmd.def_
            for c in coldef.constraints or ():
                # EXCLUDE has no inline column-constraint syntax in Postgres
                # (only a table-level ADD CONSTRAINT), so only UNIQUE/PRIMARY
                # KEY are reachable here -- look those two up directly rather
                # than reusing the AddConstraint map's EXCLUDE entry.
                if c.contype not in (
                    enums.ConstrType.CONSTR_UNIQUE,
                    enums.ConstrType.CONSTR_PRIMARY,
                ):
                    continue
                label = _CONSTRAINT_LOCK_HAZARD_LABELS[c.contype]
                violations.append(
                    f"{table}.{coldef.colname}: ADD COLUMN ... {label} implicitly "
                    f"builds a non-CONCURRENT index on a pre-existing table -- holds "
                    f"an ACCESS EXCLUSIVE lock for the whole build (issue #219)"
                )
    return violations


def _reindex_lock_violations(
    node: pg_ast.ReindexStmt,
    pre_existing_tables: set[str],
    new_tables_this_migration: set[str],
) -> list[str]:
    """A4: REINDEX rebuilds the index (and its ACCESS EXCLUSIVE lock on the
    underlying table) unless CONCURRENTLY is given. SCHEMA/DATABASE/SYSTEM
    forms have no single table to resolve "pre-existing" against -- flagged
    unconditionally, same fail-closed posture as an unresolvable ALTER TABLE
    sub-command. REINDEX INDEX names an index, not a table -- this scanner
    has no catalog to resolve which table it belongs to, so it's flagged
    unconditionally too."""
    if any(
        isinstance(p, pg_ast.DefElem) and p.defname == "concurrently" for p in (node.params or ())
    ):
        return []
    if node.kind == enums.ReindexObjectType.REINDEX_OBJECT_TABLE:
        table = node.relation.relname
        if table in pre_existing_tables and table not in new_tables_this_migration:
            return [
                f"{table}: REINDEX TABLE without CONCURRENTLY on a pre-existing table -- "
                f"holds an ACCESS EXCLUSIVE lock for the whole rebuild (issue #219)"
            ]
        return []
    return [
        f"REINDEX {node.kind.name} without CONCURRENTLY -- holds an ACCESS EXCLUSIVE "
        f"lock per index rebuilt, and this scanner cannot resolve which table(s) that "
        f"means (issue #219)"
    ]


def _cluster_lock_violations(
    node: pg_ast.ClusterStmt,
    pre_existing_tables: set[str],
    new_tables_this_migration: set[str],
) -> list[str]:
    """A4: CLUSTER has no CONCURRENTLY option at all -- it always takes
    ACCESS EXCLUSIVE for the whole table rewrite. A bare `CLUSTER` with no
    table name re-clusters every previously-clustered table in the database,
    an even wider hazard than a single table -- flagged unconditionally."""
    if node.relation is None:
        return [
            "CLUSTER (no table given): rewrites every previously-clustered table in the "
            "database under ACCESS EXCLUSIVE, one at a time -- issue #219 lock-duration "
            "hazard, no CONCURRENTLY equivalent exists"
        ]
    table = node.relation.relname
    if table in pre_existing_tables and table not in new_tables_this_migration:
        return [
            f"{table}: CLUSTER on a pre-existing table -- holds an ACCESS EXCLUSIVE lock "
            f"for the whole rewrite (issue #219), no CONCURRENTLY equivalent exists"
        ]
    return []


def _index_lock_violations(migration: Migration, pre_existing_tables: set[str]) -> list[str]:
    new_tables_this_migration: set[str] = set()
    violations: list[str] = []
    for stmt_sql in migration.sql:
        try:
            raw_stmts = pglast.parse_sql(stmt_sql)
        except pglast.parser.ParseError:
            continue
        for raw in raw_stmts:
            node = raw.stmt
            if isinstance(node, pg_ast.CreateStmt):
                # A2: only a genuinely new table (not already pre-existing under
                # an IF NOT EXISTS create of a name an earlier migration owns)
                # earns the same-migration exemption below.
                if node.relation.relname not in pre_existing_tables:
                    new_tables_this_migration.add(node.relation.relname)
                continue
            if isinstance(node, pg_ast.DoStmt):
                # A3: opaque to this scanner -- mirror Rule 1's fail-closed
                # DoStmt handling instead of silently skipping it.
                violations.append(
                    "DO $$ ... $$ block: opaque to this scanner (libpg_query does not "
                    "parse the procedural-language body) -- may build a non-CONCURRENT "
                    "index under a full table lock, so always treated as "
                    "lock-duration-risky (issue #219)"
                )
                continue
            if isinstance(node, pg_ast.ReindexStmt):
                violations.extend(
                    _reindex_lock_violations(node, pre_existing_tables, new_tables_this_migration)
                )
                continue
            if isinstance(node, pg_ast.ClusterStmt):
                violations.extend(
                    _cluster_lock_violations(node, pre_existing_tables, new_tables_this_migration)
                )
                continue
            if isinstance(node, pg_ast.AlterTableStmt):
                violations.extend(
                    _alter_table_index_lock_violations(
                        node, pre_existing_tables, new_tables_this_migration
                    )
                )
                continue
            if not isinstance(node, pg_ast.IndexStmt) or node.concurrent:
                continue
            table = node.relation.relname
            if table in pre_existing_tables and table not in new_tables_this_migration:
                violations.append(
                    f"{table}: CREATE INDEX {node.idxname or '(unnamed)'} without "
                    f"CONCURRENTLY on a pre-existing table -- holds a SHARE lock for "
                    f"the whole build, blocking writes while the previous release "
                    f"keeps serving (issue #219)"
                )
    return violations


def _migrations_with_index_lock_signal() -> dict[int, list[str]]:
    """version -> violation list, walked across MIGRATIONS in registry order,
    mirroring Rule 2's _migrations_with_backfill_signal."""
    state = _SchemaState()
    signal: dict[int, list[str]] = {}
    for migration in MIGRATIONS:
        pre_existing = set(state.table_created_at)
        signal[migration.version] = _index_lock_violations(migration, pre_existing)
        _scan_migration(migration, state)  # advance state past this migration too
    return signal


# ---------------------------------------------------------------------------
# Rule 4: keep the autocommit escape hatch narrow (#219). `autocommit = True`
# exists for exactly one reason -- letting `CREATE INDEX CONCURRENTLY` run
# outside the ledger transaction, since Postgres refuses to run it inside one
# at all. This enforces that stays the ONLY thing the flag is used for:
#   (a) CONCURRENTLY <=> autocommit = True, both directions.
#   (b) an autocommit migration's `sql` may contain ONLY CREATE/DROP INDEX
#       statements, and no `py` hook.
#   (c) EVERY index statement in an autocommit migration must be
#       retry-idempotent, not just the CONCURRENTLY one(s): every
#       CREATE INDEX (CONCURRENTLY or not) must use IF NOT EXISTS, and every
#       DROP INDEX must use IF EXISTS -- jobcannon/db/migrate.py's
#       _apply_autocommit_migration drops any INVALID leftover index before
#       re-running an autocommit migration's statements, which is what turns
#       a bare retry into a safe no-op instead of the silent-skip hazard
#       #219 raised, but ONLY if every statement in the migration is itself
#       a safe no-op on a second run -- autocommit runs each statement
#       outside a transaction, so a statement before a later failure has
#       already committed by the time a retry starts.
# ---------------------------------------------------------------------------


def _concurrently_index_statements(
    migration: Migration,
) -> list[pg_ast.IndexStmt | pg_ast.DropStmt]:
    """CREATE INDEX CONCURRENTLY and DROP INDEX CONCURRENTLY statements --
    both are the reason rule (a) requires autocommit = True (CONCURRENTLY
    cannot run inside the per-migration ledger transaction, whether it's a
    build or a drop), so both satisfy it. Re-review LOW #1: a migration
    whose sql is ONLY ['DROP INDEX CONCURRENTLY IF EXISTS x'] has no valid
    non-autocommit representation either -- counting only IndexStmt here
    made that shape unrepresentable, flagged as "declares autocommit = True
    but has no CONCURRENTLY statement" even though it's the correct way to
    write it."""
    nodes: list[pg_ast.IndexStmt | pg_ast.DropStmt] = []
    for stmt_sql in migration.sql:
        try:
            raw_stmts = pglast.parse_sql(stmt_sql)
        except pglast.parser.ParseError:
            continue
        for raw in raw_stmts:
            node = raw.stmt
            if isinstance(node, pg_ast.IndexStmt) and node.concurrent:
                nodes.append(node)
            elif (
                isinstance(node, pg_ast.DropStmt)
                and node.removeType == enums.ObjectType.OBJECT_INDEX
                and node.concurrent
            ):
                nodes.append(node)
    return nodes


def _all_index_and_drop_index_statements(
    migration: Migration,
) -> list[pg_ast.IndexStmt | pg_ast.DropStmt]:
    """Every CREATE INDEX (concurrent or not) and DROP INDEX statement in
    this migration's `sql`, in source order -- used to enforce (c) above
    across the WHOLE autocommit migration, not just its CONCURRENTLY
    statement(s) (exec-safety review LOW #1: a bare DROP INDEX or a
    non-CONCURRENT CREATE INDEX earlier in an autocommit migration can
    already have committed, outside a transaction, before a later statement
    fails -- the retry must re-run it as a safe no-op too)."""
    nodes: list[pg_ast.IndexStmt | pg_ast.DropStmt] = []
    for stmt_sql in migration.sql:
        try:
            raw_stmts = pglast.parse_sql(stmt_sql)
        except pglast.parser.ParseError:
            continue
        for raw in raw_stmts:
            node = raw.stmt
            if isinstance(node, pg_ast.IndexStmt):
                nodes.append(node)
            elif (
                isinstance(node, pg_ast.DropStmt)
                and node.removeType == enums.ObjectType.OBJECT_INDEX
            ):
                nodes.append(node)
    return nodes


def _non_index_ddl_statements(migration: Migration) -> list[str]:
    """Every statement in this migration's `sql` that is NOT a CREATE INDEX
    or DROP INDEX -- used to enforce (b) above: is this migration's `sql`
    made up of ONLY index statements at all. This exemption stays
    unconditional (it does not itself check IF EXISTS/IF NOT EXISTS) --
    _all_index_and_drop_index_statements above is the dedicated, separate
    check for (c)'s idempotency requirement, so a DROP INDEX without
    IF EXISTS is correctly classified as "an index statement missing the
    required guard" (a (c) violation) rather than "not an index statement at
    all" (a (b) violation) -- the two failure messages point an author at
    different fixes and conflating them would be confusing. An unparseable
    statement counts as a (b) offender too (fail closed, same posture as
    _scan_migration)."""
    offenders: list[str] = []
    for stmt_sql in migration.sql:
        try:
            raw_stmts = pglast.parse_sql(stmt_sql)
        except pglast.parser.ParseError:
            offenders.append(stmt_sql)
            continue
        for raw in raw_stmts:
            node = raw.stmt
            if isinstance(node, pg_ast.IndexStmt):
                continue
            if (
                isinstance(node, pg_ast.DropStmt)
                and node.removeType == enums.ObjectType.OBJECT_INDEX
            ):
                continue
            offenders.append(stmt_sql)
    return offenders


def _autocommit_escape_hatch_violations(migration: Migration) -> list[str]:
    """Pure classification against ONE migration's already-folded flags +
    parsed SQL shape -- decoupled from the real registry so it can be
    sabotage-verified with synthetic fixtures independent of whether any
    shipped migration happens to use autocommit at all (same refuter-3
    rationale as Rule 2/Rule 3's own fixtures)."""
    violations: list[str] = []
    concurrently_stmts = _concurrently_index_statements(migration)

    if concurrently_stmts and not migration.autocommit:
        violations.append(
            f"migration {migration.version} ({migration.name}) has a CONCURRENTLY "
            f"CREATE INDEX or DROP INDEX statement but doesn't declare "
            f"autocommit = True -- CONCURRENTLY cannot run inside the "
            f"per-migration ledger transaction"
        )
    if migration.autocommit and not concurrently_stmts:
        violations.append(
            f"migration {migration.version} ({migration.name}) declares "
            f"autocommit = True but has no CONCURRENTLY statement -- autocommit "
            f"only exists to let CONCURRENTLY run outside the ledger transaction, "
            f"never set it without one"
        )
    if migration.autocommit:
        if migration.py is not None:
            violations.append(
                f"migration {migration.version} ({migration.name}) is autocommit "
                f"but defines a `py` hook -- autocommit migrations may contain "
                f"only CREATE/DROP INDEX statements"
            )
        non_index = _non_index_ddl_statements(migration)
        if non_index:
            violations.append(
                f"migration {migration.version} ({migration.name}) is autocommit "
                f"but has non-index statement(s) {non_index!r} -- autocommit "
                f"migrations may contain only CREATE/DROP INDEX statements"
            )
        for node in _all_index_and_drop_index_statements(migration):
            if isinstance(node, pg_ast.IndexStmt):
                if not node.if_not_exists:
                    concurrently_label = "CONCURRENTLY " if node.concurrent else ""
                    violations.append(
                        f"migration {migration.version} ({migration.name}): CREATE INDEX "
                        f"{concurrently_label}{node.idxname or '(unnamed)'} has no "
                        f"IF NOT EXISTS -- required for autocommit migrations so a retry "
                        f"after an earlier statement already committed is a safe no-op"
                    )
            elif not node.missing_ok:
                idx_names = ", ".join(names[-1].sval for names in node.objects)
                violations.append(
                    f"migration {migration.version} ({migration.name}): DROP INDEX "
                    f"{idx_names} has no IF EXISTS -- required for autocommit "
                    f"migrations so a retry after this statement already committed "
                    f"doesn't raise on an already-dropped index"
                )
    return violations


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


def test_no_unjustified_index_lock_ddl():
    signal = _migrations_with_index_lock_signal()
    failures = []
    for migration in MIGRATIONS:
        violations = signal.get(migration.version, [])
        if violations and not migration.lock_step:
            failures.append(
                f"migration {migration.version} ({migration.name}) has unjustified "
                f"lock-duration DDL: {violations}. Declare `lock_step = True` on the "
                f"module with a docstring 'Lock justification:' section naming the "
                f"expected row count / build time, or use CONCURRENTLY with "
                f"autocommit = True instead."
            )
    assert not failures, "\n".join(failures)


def test_lock_step_migrations_declare_a_justification():
    failures = []
    for migration, module in _all_migration_modules():
        if not _lock_step_ok(migration.lock_step, module.__doc__ or ""):
            failures.append(
                f"migration {migration.version} ({migration.name}) sets "
                f"lock_step = True but its docstring has no "
                f"'Lock justification:' section"
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


def test_no_undeclared_autocommit_escape_hatch_violations():
    failures = []
    for migration in MIGRATIONS:
        failures.extend(_autocommit_escape_hatch_violations(migration))
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


def test_lock_step_and_autocommit_fold_neither_source_silently_overwrites_the_other():
    """Same #218 review M1 shape as
    test_contract_step_fold_neither_source_silently_overwrites_the_other
    above, applied to the two new fold helpers this issue (#219) adds."""
    assert _fold_lock_step(module_attr=False, migration_kwarg=True) is True
    assert _fold_lock_step(module_attr=True, migration_kwarg=False) is True
    assert _fold_lock_step(module_attr=True, migration_kwarg=True) is True
    assert _fold_lock_step(module_attr=False, migration_kwarg=False) is False

    assert _fold_autocommit(module_attr=False, migration_kwarg=True) is True
    assert _fold_autocommit(module_attr=True, migration_kwarg=False) is True
    assert _fold_autocommit(module_attr=True, migration_kwarg=True) is True
    assert _fold_autocommit(module_attr=False, migration_kwarg=False) is False


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
    # plain (non-unique) CREATE INDEX on a pre-existing table: not a
    # CONTRACT violation (Rule 1, this scanner) -- it IS a lock-duration
    # violation (Rule 3, #219), but that's a separate, independent walk
    # (_index_lock_violations below), not this one. Covered by
    # test_index_lock_sabotage_fixtures_are_all_detected instead.
    pytest.param(["CREATE INDEX ON companies(name)"], id="plain-create-index-not-contract-shaped"),
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
# Rule 3 (lock duration, #219) sabotage fixtures, decoupled from real
# migration modules -- same refuter-3 rationale as Rule 2's fixtures below:
# after m0005's retroactive lock_step annotation the real registry only
# exercises the "already declared correctly" path, so a neutered detector
# would go uncaught by walking MIGRATIONS alone.
# ---------------------------------------------------------------------------

_INDEX_LOCK_BAD_FIXTURES = [
    pytest.param(
        ["CREATE INDEX ON companies(name)"],
        "CONCURRENTLY",
        id="plain-create-index-preexisting-table",
    ),
    pytest.param(
        ["CREATE INDEX idx_users_email ON users(email)"],
        "CONCURRENTLY",
        id="named-create-index-preexisting-table",
    ),
    pytest.param(
        ["CREATE UNIQUE INDEX ON companies(name)"],
        "CONCURRENTLY",
        id="unique-index-preexisting-table-also-lock-flagged",
    ),
    # --- A1: ALTER TABLE shapes that implicitly build a non-CONCURRENT
    # index bypassed this rule entirely before (scanner review MED #1). ---
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT companies_name_uq UNIQUE (name)"],
        "ADD CONSTRAINT UNIQUE",
        id="add-unique-constraint-preexisting-table-lock-flagged",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT companies_name_pk PRIMARY KEY (name)"],
        "ADD CONSTRAINT PRIMARY KEY",
        id="add-primary-key-constraint-preexisting-table-lock-flagged",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT companies_name_ex EXCLUDE USING gist (name WITH =)"],
        "ADD CONSTRAINT EXCLUDE",
        id="add-exclude-constraint-preexisting-table-lock-flagged",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD COLUMN newcol text UNIQUE"],
        "ADD COLUMN ... UNIQUE",
        id="add-column-inline-unique-preexisting-table-lock-flagged",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD COLUMN newcol text PRIMARY KEY"],
        "ADD COLUMN ... PRIMARY KEY",
        id="add-column-inline-primary-key-preexisting-table-lock-flagged",
    ),
    # --- A2: CREATE TABLE IF NOT EXISTS of a table an earlier migration
    # already owns must not shield a later statement in the same migration
    # (scanner review MED #2). ---
    pytest.param(
        [
            "CREATE TABLE IF NOT EXISTS companies (id bigserial PRIMARY KEY)",
            "CREATE INDEX ON companies(name)",
        ],
        "CONCURRENTLY",
        id="create-table-if-not-exists-preexisting-does-not-shield",
    ),
    # --- A3: DO $$ ... $$ blocks are opaque -- mirror Rule 1's fail-closed
    # DoStmt handling instead of silently skipping past this rule too
    # (Devin lead 2). ---
    pytest.param(
        ["DO $$ BEGIN CREATE INDEX ON companies(name); END $$;"],
        "opaque to this scanner",
        id="do-block-hides-index-build-lock-flagged",
    ),
    # --- A4: top-level REINDEX/CLUSTER without CONCURRENTLY fell through
    # unflagged before (scanner review LOW). ---
    pytest.param(
        ["REINDEX TABLE companies"], "REINDEX TABLE", id="reindex-table-preexisting-lock-flagged"
    ),
    pytest.param(
        ["REINDEX INDEX some_idx"], "REINDEX", id="reindex-index-unresolvable-lock-flagged"
    ),
    pytest.param(["REINDEX SCHEMA public"], "REINDEX", id="reindex-schema-lock-flagged"),
    pytest.param(
        ["CLUSTER companies USING companies_pkey"],
        "CLUSTER",
        id="cluster-preexisting-table-lock-flagged",
    ),
    pytest.param(["CLUSTER"], "CLUSTER", id="cluster-no-table-lock-flagged"),
]


@pytest.mark.parametrize("sql_statements,expected_substring", _INDEX_LOCK_BAD_FIXTURES)
def test_index_lock_sabotage_fixtures_are_all_detected(sql_statements, expected_substring):
    state = _SchemaState()
    for migration in MIGRATIONS:
        _scan_migration(migration, state)  # seed state with the real schema history
    pre_existing = set(state.table_created_at)
    probe = Migration(
        version=999995,
        description="index lock sabotage probe",
        sql=sql_statements,
        name="m999995_index_lock_sabotage",
    )
    violations = _index_lock_violations(probe, pre_existing)
    assert violations, f"scanner did not flag known-bad index statement(s) {sql_statements!r}"
    joined = "\n".join(violations)
    assert expected_substring in joined, (
        f"{sql_statements!r} was flagged, but not by its OWN rule -- expected "
        f"{expected_substring!r} in {violations!r}"
    )


_INDEX_LOCK_SAFE_FIXTURES = [
    pytest.param(
        ["CREATE TABLE t_new (id bigserial PRIMARY KEY)", "CREATE INDEX ON t_new(id)"],
        id="create-index-on-same-migration-table",
    ),
    pytest.param(
        ["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_companies_name ON companies(name)"],
        id="concurrently-on-preexisting-table-not-lock-flagged",
    ),
    # --- A1 safe shape: ADD CONSTRAINT ... UNIQUE USING INDEX attaches an
    # already-built index instead of building a new one -- the standard
    # zero-downtime pattern (build CONCURRENTLY, then attach). ---
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT companies_name_uq UNIQUE USING INDEX my_idx"],
        id="add-constraint-using-index-not-lock-flagged",
    ),
    # --- A1 safe shape: the SAME exemption applies on a table THIS
    # migration creates -- empty, so the build is instant either way. ---
    pytest.param(
        [
            "CREATE TABLE t_new2 (id bigserial PRIMARY KEY)",
            "ALTER TABLE t_new2 ADD CONSTRAINT t_new2_x_uq UNIQUE (id)",
        ],
        id="add-unique-constraint-on-same-migration-table-not-lock-flagged",
    ),
    # --- A2 safe shape: CREATE TABLE IF NOT EXISTS of a GENUINELY new name
    # (not already pre-existing) still earns the same-migration exemption. ---
    pytest.param(
        [
            "CREATE TABLE IF NOT EXISTS t_new3 (id bigserial PRIMARY KEY)",
            "CREATE INDEX ON t_new3(id)",
        ],
        id="create-table-if-not-exists-genuinely-new-still-exempt",
    ),
    # --- A4 safe shapes: CONCURRENTLY forms take no lock. ---
    pytest.param(
        ["REINDEX TABLE CONCURRENTLY companies"], id="reindex-table-concurrently-not-lock-flagged"
    ),
]


@pytest.mark.parametrize("sql_statements", _INDEX_LOCK_SAFE_FIXTURES)
def test_index_lock_known_safe_shapes_are_not_flagged(sql_statements):
    state = _SchemaState()
    for migration in MIGRATIONS:
        _scan_migration(migration, state)
    pre_existing = set(state.table_created_at)
    probe = Migration(
        version=999994,
        description="index lock safe probe",
        sql=sql_statements,
        name="m999994_index_lock_safe",
    )
    violations = _index_lock_violations(probe, pre_existing)
    assert not violations, f"scanner false-flagged known-safe index statement(s): {violations}"


# ---------------------------------------------------------------------------
# Rule 4 (autocommit escape hatch, #219) sabotage fixtures -- synthetic
# Migration objects directly (this rule reads the already-folded
# .autocommit/.py fields, not module attributes), independent of whether any
# shipped migration ever uses autocommit at all.
# ---------------------------------------------------------------------------

_AUTOCOMMIT_ESCAPE_HATCH_BAD_FIXTURES = [
    pytest.param(
        ["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)"],
        False,
        None,
        "doesn't declare autocommit",
        id="concurrently-without-autocommit",
    ),
    pytest.param(
        ["CREATE INDEX IF NOT EXISTS idx_x ON t(x)"],
        True,
        None,
        "no CONCURRENTLY statement",
        id="autocommit-without-concurrently",
    ),
    pytest.param(
        ["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)"],
        True,
        lambda ctx: None,
        "py` hook",
        id="autocommit-with-py-hook",
    ),
    pytest.param(
        [
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)",
            "ALTER TABLE t ADD COLUMN y text",
        ],
        True,
        None,
        "non-index statement",
        id="autocommit-with-other-ddl",
    ),
    pytest.param(
        ["CREATE INDEX CONCURRENTLY idx_x ON t(x)"],
        True,
        None,
        "no IF NOT EXISTS",
        id="autocommit-without-if-not-exists",
    ),
    # --- exec-safety review LOW #1: idempotency enforcement widened to
    # every index statement in an autocommit migration, not just the
    # CONCURRENTLY one(s) -- a bare DROP INDEX (no IF EXISTS) or a plain
    # CREATE INDEX (no IF NOT EXISTS) earlier in the same migration can
    # already have committed, outside a transaction, before a later
    # statement fails; retry must re-run it as a safe no-op too. ---
    pytest.param(
        [
            "DROP INDEX idx_old",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)",
        ],
        True,
        None,
        "no IF EXISTS",
        id="autocommit-drop-index-without-if-exists",
    ),
    pytest.param(
        [
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_a ON t(x)",
            "CREATE INDEX idx_b ON t(y)",
        ],
        True,
        None,
        "no IF NOT EXISTS",
        id="autocommit-non-concurrent-create-without-if-not-exists",
    ),
    # --- re-review LOW #1: a PURE DROP INDEX CONCURRENTLY migration (no
    # CREATE INDEX CONCURRENTLY anywhere) still requires the IF EXISTS
    # idempotency guard, same as every other index statement in an
    # autocommit migration. ---
    pytest.param(
        ["DROP INDEX CONCURRENTLY a"],
        True,
        None,
        "no IF EXISTS",
        id="autocommit-drop-concurrently-only-without-if-exists",
    ),
    # --- re-review LOW #1 regression guard: the fix only teaches rule (a)
    # that a CONCURRENT DropStmt counts -- a bare, NON-concurrent DROP INDEX
    # alone (nothing CONCURRENTLY anywhere) must still trip "no CONCURRENTLY
    # statement", since a bare DROP INDEX needs no autocommit at all. ---
    pytest.param(
        ["DROP INDEX idx_old"],
        True,
        None,
        "no CONCURRENTLY statement",
        id="autocommit-bare-drop-only-still-requires-concurrently",
    ),
]


@pytest.mark.parametrize(
    "sql_statements,autocommit,py_hook,expected_substring", _AUTOCOMMIT_ESCAPE_HATCH_BAD_FIXTURES
)
def test_autocommit_escape_hatch_sabotage_fixtures(
    sql_statements, autocommit, py_hook, expected_substring
):
    probe = Migration(
        version=999993,
        description="autocommit escape-hatch sabotage probe",
        sql=sql_statements,
        py=py_hook,
        name="m999993_autocommit_sabotage",
        autocommit=autocommit,
    )
    violations = _autocommit_escape_hatch_violations(probe)
    assert violations, f"scanner did not flag known-bad autocommit shape {sql_statements!r}"
    joined = "\n".join(violations)
    assert expected_substring in joined, (
        f"flagged, but not by its OWN rule -- expected {expected_substring!r} in {violations!r}"
    )


_AUTOCOMMIT_ESCAPE_HATCH_SAFE_FIXTURES = [
    pytest.param(
        ["CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)"],
        True,
        id="concurrently-autocommit-if-not-exists",
    ),
    pytest.param(
        [
            "DROP INDEX CONCURRENTLY IF EXISTS idx_old",
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)",
        ],
        True,
        id="drop-and-create-concurrently-index-only",
    ),
    # --- re-review LOW #1: a PURE concurrent index drop, with no CREATE
    # INDEX CONCURRENTLY anywhere in the migration, is the only valid way to
    # express a standalone concurrent index drop (DROP INDEX CONCURRENTLY
    # can't run inside a transaction either) -- must be representable. ---
    pytest.param(
        ["DROP INDEX CONCURRENTLY IF EXISTS a"],
        True,
        id="drop-index-concurrently-only-no-create",
    ),
    pytest.param(
        ["ALTER TABLE t ADD COLUMN y text"],
        False,
        id="ordinary-migration-no-concurrently-no-autocommit",
    ),
    # Non-concurrent CREATE INDEX / bare DROP INDEX are still structurally
    # allowed content in an autocommit migration's `sql` (b) -- as long as
    # they carry the idempotency guard (c). (This particular non-concurrent
    # CREATE INDEX is ALSO a Rule 3 lock-duration violation on a
    # pre-existing table -- out of scope for this Rule 4 fixture, which
    # only exercises the escape-hatch/idempotency rules against a bare
    # table name with no registry schema-state behind it.)
    pytest.param(
        [
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_x ON t(x)",
            "CREATE INDEX IF NOT EXISTS idx_y ON t(y)",
            "DROP INDEX CONCURRENTLY IF EXISTS idx_old",
            "DROP INDEX IF EXISTS idx_older",
        ],
        True,
        id="mixed-concurrent-and-non-concurrent-index-statements-all-idempotent",
    ),
]


@pytest.mark.parametrize("sql_statements,autocommit", _AUTOCOMMIT_ESCAPE_HATCH_SAFE_FIXTURES)
def test_autocommit_escape_hatch_known_safe_shapes(sql_statements, autocommit):
    probe = Migration(
        version=999992,
        description="autocommit escape-hatch safe probe",
        sql=sql_statements,
        name="m999992_autocommit_safe",
        autocommit=autocommit,
    )
    assert _autocommit_escape_hatch_violations(probe) == []


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


def _lock_step_ok(lock_step: bool, doc: str) -> bool:
    """Mirrors _contract_step_ok exactly -- marker-presence only, no prose
    linting. The "expected row count / build time" content of the
    justification is a human-review concern, not something this guard
    parses."""
    return not (lock_step and "Lock justification:" not in doc)


def test_lock_step_justification_rule_fixtures():
    assert not _lock_step_ok(True, "no justification here")
    assert not _lock_step_ok(True, "")
    assert _lock_step_ok(True, "Lock justification: empty table at migration time, see above")
    assert _lock_step_ok(False, "")


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
