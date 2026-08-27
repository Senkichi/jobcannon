"""Static analysis guard over jobcannon.db.migrations.MIGRATIONS (issue #199):
fails the moment a future migration ships contract-shaped DDL against a
table/column an EARLIER migration created, or documents a "Deploy order:
... AFTER" backfill ordering that Render's pre-deploy step now inverts.

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

No DB, no sqlglot/pglast: each migration's `sql` list is already a list of
discrete statement strings (jobcannon.db.migrations.types.Migration.sql),
so this walks them with a small hand-rolled, paren/quote-depth-aware
tokenizer -- a plain non-nesting regex can't safely carve out a `CHECK (x
IN (...))` body -- plus a handful of anchored regexes for statement shape.

Conservative by construction: an identifier or statement shape this
scanner cannot positively prove is same-migration-new is treated as
contract-shaped (over-flag, never silently pass). The cost of a false
positive is one `contract_step = True` annotation; the cost of a false
negative is a broken zero-downtime deploy.
"""

from __future__ import annotations

import dataclasses
import importlib
import re

import pytest

from jobcannon.db.migrations import MIGRATIONS
from jobcannon.db.migrations.types import Migration

# ---------------------------------------------------------------------------
# Tokenizing helpers: paren-depth + single-quote-aware, so a nested
# `CHECK (x IN ('a','b'))` body never confuses "is this comma/paren at the
# top level of the clause I'm splitting" with "is it inside a nested
# expression or a string literal."
# ---------------------------------------------------------------------------


def _matching_paren(text: str, open_idx: int) -> int:
    """Index of the ')' matching the '(' at open_idx, honoring nesting and
    single-quoted string literals (so a literal containing ')' can never be
    mistaken for the end of the expression)."""
    depth = 0
    in_string = False
    i = open_idx
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":  # doubled '' escape
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError(f"unbalanced parens from index {open_idx}: {text!r}")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split on `sep` only at paren-depth 0 and outside string literals --
    used both for CREATE TABLE's column list and for a multi-clause
    ALTER TABLE (`ADD COLUMN x, ADD CONSTRAINT ...`)."""
    parts: list[str] = []
    depth = 0
    in_string = False
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Deliberately small: only needs to cover the token vocabulary this repo's
# migrations actually use inside CHECK expressions. An unknown token is
# treated as a possible column reference (see _referenced_identifiers),
# which is the conservative/fail-safe direction, not a correctness bug.
_SQL_KEYWORDS = frozenset(
    {"IN", "IS", "NULL", "NOT", "OR", "AND", "TRUE", "FALSE", "LIKE", "BETWEEN", "EXISTS"}
)


def _referenced_identifiers(expr: str) -> set[str]:
    """Column-ish identifiers inside a CHECK/UNIQUE expression: strip string
    literals first (so a quoted enum value like 'pending' never
    masquerades as a column reference), then every remaining bareword that
    isn't a known SQL keyword."""
    stripped = _STRING_LITERAL_RE.sub(" ", expr)
    return {tok for tok in _IDENTIFIER_RE.findall(stripped) if tok.upper() not in _SQL_KEYWORDS}


# ---------------------------------------------------------------------------
# Schema-state tracking: walked across MIGRATIONS in registry (version)
# order so "pre-existing" can mean exactly what docs/deploy-runbook.md's
# discipline means -- "a table/column an EARLIER migration created" -- with
# no hand-maintained snapshot of the schema.
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _SchemaState:
    # table -> version of the migration whose CREATE TABLE introduced it.
    table_created_at: dict[str, int] = dataclasses.field(default_factory=dict)
    # (table, column) -> version of the migration that introduced it, via
    # either a CREATE TABLE column list or an ADD COLUMN clause.
    column_created_at: dict[tuple[str, str], int] = dataclasses.field(default_factory=dict)


_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\(", re.IGNORECASE
)
_CONSTRAINT_LEAD_KEYWORDS = {"UNIQUE", "CHECK", "PRIMARY", "FOREIGN", "CONSTRAINT", "EXCLUDE"}
_DROP_TABLE_RE = re.compile(r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_ALTER_TABLE_RE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+", re.IGNORECASE)

# --- Per-clause contract-DDL patterns (ONE place, one comment per pattern
# naming the overlap-window failure it prevents). Every pattern here maps
# 1:1 onto a bullet in issue #199's proposal. ---
_ADD_COLUMN_RE = re.compile(
    r"^ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s+(.*)$", re.IGNORECASE | re.DOTALL
)
_DROP_COLUMN_RE = re.compile(r"^DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_ALTER_COLUMN_TYPE_RE = re.compile(
    r"^ALTER\s+COLUMN\s+(\w+)\s+(?:SET\s+DATA\s+)?TYPE\b", re.IGNORECASE
)
_ALTER_COLUMN_SET_NOT_NULL_RE = re.compile(
    r"^ALTER\s+COLUMN\s+(\w+)\s+SET\s+NOT\s+NULL\b", re.IGNORECASE
)
_ADD_CONSTRAINT_RE = re.compile(r"^ADD\s+CONSTRAINT\s+(\w+)\s+(CHECK|UNIQUE)\b", re.IGNORECASE)
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)


def _parse_create_table(stmt: str) -> tuple[str, list[str]] | None:
    """(table, [column names]) for a CREATE TABLE statement, or None. Table-
    level constraint lines (UNIQUE(...), CHECK(...), PRIMARY KEY(...), ...)
    are recognized by their leading keyword and excluded -- everything else
    at the top level of the column list is a column definition, and its
    first token is the column name."""
    m = _CREATE_TABLE_RE.search(stmt)
    if not m:
        return None
    table = m.group(1)
    open_idx = m.end() - 1
    close_idx = _matching_paren(stmt, open_idx)
    body = stmt[open_idx + 1 : close_idx]
    columns = []
    for segment in _split_top_level(body):
        seg = segment.strip()
        if not seg:
            continue
        tok_m = _IDENTIFIER_RE.match(seg)
        if not tok_m:
            continue
        if tok_m.group(0).upper() in _CONSTRAINT_LEAD_KEYWORDS:
            continue
        columns.append(tok_m.group(0))
    return table, columns


def _is_new_this_migration(
    table: str, col: str, version: int, state: _SchemaState, new_tables: set[str]
) -> bool:
    if table in new_tables:
        return True
    return state.column_created_at.get((table, col)) == version


def _scan_alter_clause(
    table: str,
    clause: str,
    version: int,
    state: _SchemaState,
    new_tables: set[str],
) -> list[str]:
    violations: list[str] = []

    m = _ADD_COLUMN_RE.match(clause)
    if m:
        col, definition = m.group(1), m.group(2)
        state.column_created_at.setdefault((table, col), version)
        # A brand-new column on a table that ALSO didn't exist before this
        # migration can never break the previous release -- nothing ever
        # queried a table that didn't exist yet. Only a NOT NULL column
        # added to a pre-existing table, with no DEFAULT to backfill
        # existing rows, blocks the previous release's un-migrated INSERTs.
        if (
            table not in new_tables
            and _NOT_NULL_RE.search(definition)
            and not _DEFAULT_RE.search(definition)
        ):
            violations.append(
                f"{table}.{col}: ADD COLUMN ... NOT NULL without DEFAULT (clause: {clause!r})"
            )
        return violations

    m = _DROP_COLUMN_RE.match(clause)
    if m:
        col = m.group(1)
        if not _is_new_this_migration(table, col, version, state, new_tables):
            violations.append(f"{table}.{col}: DROP COLUMN (clause: {clause!r})")
        return violations

    m = _ALTER_COLUMN_TYPE_RE.match(clause)
    if m:
        col = m.group(1)
        if not _is_new_this_migration(table, col, version, state, new_tables):
            violations.append(f"{table}.{col}: ALTER COLUMN ... TYPE (clause: {clause!r})")
        return violations

    m = _ALTER_COLUMN_SET_NOT_NULL_RE.match(clause)
    if m:
        col = m.group(1)
        if not _is_new_this_migration(table, col, version, state, new_tables):
            violations.append(f"{table}.{col}: ALTER COLUMN ... SET NOT NULL (clause: {clause!r})")
        return violations

    m = _ADD_CONSTRAINT_RE.match(clause)
    if m:
        constraint_name, kind = m.group(1), m.group(2).upper()
        paren_start = clause.index("(", m.end(2))
        paren_end = _matching_paren(clause, paren_start)
        body = clause[paren_start + 1 : paren_end]
        referenced = _referenced_identifiers(body)
        # Any referenced identifier that resolves to a KNOWN column not
        # added in THIS migration is a reference to a pre-existing column.
        # Any identifier that doesn't resolve to any known column at all is
        # treated the same conservative way -- it might be a column this
        # scanner failed to track, and a false "safe" here is exactly the
        # failure mode this guard exists to prevent.
        risky = {
            col
            for col in referenced
            if (table, col) not in state.column_created_at
            or not _is_new_this_migration(table, col, version, state, new_tables)
        }
        if risky:
            violations.append(
                f"{table}: ADD CONSTRAINT {constraint_name} {kind} references "
                f"pre-existing/unresolved column(s) {sorted(risky)} (clause: {clause!r})"
            )
        return violations

    return violations


def _scan_migration(migration: Migration, state: _SchemaState) -> list[str]:
    """Contract-shaped-DDL violations for ONE migration, given the schema
    state established by every EARLIER migration. Mutates `state` with
    whatever this migration itself creates, so the caller can feed the same
    state object through MIGRATIONS in order. Knows nothing about the
    contract_step allow-list -- callers apply that on top of this."""
    violations: list[str] = []
    new_tables: set[str] = set()

    for stmt in migration.sql:
        parsed = _parse_create_table(stmt)
        if parsed is not None:
            table, columns = parsed
            # A CREATE TABLE (and every column/inline CHECK inside it,
            # however it's shaped) can never be contract-shaped -- no
            # previous release ever queried a table that didn't exist yet.
            state.table_created_at.setdefault(table, migration.version)
            new_tables.add(table)
            for col in columns:
                state.column_created_at.setdefault((table, col), migration.version)
            continue

        m = _DROP_TABLE_RE.search(stmt)
        if m:
            table = m.group(1)
            # Conservative by construction (see module docstring): a table
            # this migration didn't itself create -- whether we can prove
            # it's pre-existing (tracked in state.table_created_at from an
            # earlier migration) or simply never tracked at all (e.g.
            # created by a .py callable, or some CREATE TABLE shape this
            # regex-based scanner doesn't match) -- is always flagged.
            # Defaulting the unknown case to "safe" would silently pass
            # exactly the DROP this guard exists to catch.
            if table not in new_tables:
                violations.append(f"DROP TABLE {table} (pre-existing table)")
            continue

        stripped_stmt = stmt.strip()
        m = _ALTER_TABLE_RE.match(stripped_stmt)
        if not m:
            continue  # UPDATE / CREATE INDEX / CREATE EXTENSION / ... -- out of scope
        table = m.group(1)
        # Match against stripped_stmt, so slice stripped_stmt too -- m.end()
        # is an offset into the stripped string; indexing the original
        # (unstripped) stmt with it would silently corrupt clause_text by a
        # leading-whitespace-length offset whenever a future migration's SQL
        # string carries leading whitespace (none of today's do, but nothing
        # stops one from being written that way).
        clause_text = stripped_stmt[m.end() :]
        for clause in _split_top_level(clause_text):
            clause = clause.strip()
            if not clause:
                continue
            violations.extend(
                _scan_alter_clause(table, clause, migration.version, state, new_tables)
            )

    return violations


# ---------------------------------------------------------------------------
# "Deploy order: ... AFTER" docstring pattern (rule 2 of #199).
# ---------------------------------------------------------------------------

_DEPLOY_ORDER_AFTER_RE = re.compile(r"deploy order:.{0,200}\bafter\b", re.IGNORECASE)


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
        if migration.contract_step and "Contract justification:" not in (module.__doc__ or ""):
            failures.append(
                f"migration {migration.version} ({migration.name}) sets "
                f"contract_step = True but its docstring has no "
                f"'Contract justification:' section"
            )
    assert not failures, "\n".join(failures)


def test_no_undeclared_inverted_deploy_order():
    failures = []
    for migration, module in _all_migration_modules():
        doc = module.__doc__ or ""
        normalized = re.sub(r"\s+", " ", doc)
        if not _DEPLOY_ORDER_AFTER_RE.search(normalized):
            continue
        if not getattr(module, "inverted_order_safe", False):
            failures.append(
                f"migration {migration.version} ({migration.name}) docstring reads "
                f"'Deploy order: ... AFTER' but pre-deploy migrations now always run "
                f"BEFORE the new release's code (docs/deploy-runbook.md §3) -- "
                f"declare `inverted_order_safe = True` with a docstring explanation "
                f"of why the backfill is idempotent under that inverted ordering, or "
                f"fix the migration to not depend on writer-first ordering"
            )
        elif "idempotent" not in doc.lower():
            failures.append(
                f"migration {migration.version} ({migration.name}) sets "
                f"inverted_order_safe = True but its docstring never explains "
                f"idempotency (no 'idempotent' text found)"
            )
    assert not failures, "\n".join(failures)


# ---------------------------------------------------------------------------
# Sabotage-verify: known-bad synthetic statements fed through the SAME
# detector, seeded with the real registry's schema history so "pre-existing"
# reflects the actual shipped shape. This is test data (a fixture list), not
# a hand-maintained production allow/deny-list.
# ---------------------------------------------------------------------------

_KNOWN_BAD_FIXTURES = [
    pytest.param(["ALTER TABLE users DROP COLUMN email"], id="drop-column-preexisting"),
    pytest.param(["DROP TABLE users"], id="drop-table-preexisting"),
    pytest.param(["ALTER TABLE users ALTER COLUMN email TYPE varchar(50)"], id="alter-column-type"),
    pytest.param(
        ["ALTER TABLE users ADD COLUMN foo boolean NOT NULL"],
        id="add-column-not-null-without-default",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT foo_check CHECK (ats_probe_status IN ('a','b'))"],
        id="add-check-constraint-preexisting-column",
    ),
    pytest.param(
        ["ALTER TABLE companies ADD CONSTRAINT foo_uq UNIQUE (name)"],
        id="add-unique-constraint-preexisting-column",
    ),
    pytest.param(
        ["ALTER TABLE users ALTER COLUMN email SET NOT NULL"],
        id="alter-column-set-not-null-preexisting",
    ),
    # Regression for the "unknown table defaults to safe" bug: a DROP TABLE
    # of a table this scanner never saw a CREATE TABLE for (not tracked as
    # pre-existing, but also not created by the probe itself) must still be
    # flagged -- "can't prove it's new" has to mean "assume pre-existing",
    # never the reverse.
    pytest.param(["DROP TABLE never_created_by_any_migration"], id="drop-table-untracked"),
    # Regression for the strip()/index offset bug: ALTER TABLE_RE matches
    # against stmt.strip(), so slicing clause_text out of the *unstripped*
    # stmt at that match offset corrupts it by the leading-whitespace
    # length -- which silently broke the anchored `^DROP COLUMN` match
    # below and made a genuinely dangerous DROP COLUMN invisible to the
    # scanner. This statement is identical to drop-column-preexisting
    # except for the leading whitespace.
    pytest.param(
        ["  ALTER TABLE users DROP COLUMN email"], id="drop-column-preexisting-leading-whitespace"
    ),
]


@pytest.mark.parametrize("sql_statements", _KNOWN_BAD_FIXTURES)
def test_sabotage_fixtures_are_all_detected(sql_statements):
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


def test_registry_has_no_hand_maintained_stand_in():
    """Sanity check that this guard is actually exercising the real
    registry, not an empty/stale import -- a guard with nothing to walk
    would pass every test above vacuously."""
    assert len(MIGRATIONS) >= 9
    assert {m.version for m in MIGRATIONS} >= {1, 2, 3, 4, 5, 6, 8, 9, 10}
