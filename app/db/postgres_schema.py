"""PostgreSQL DDL that brings an existing database up to what the models declare.

The models are the single source of truth: the CHECK constraints, the JSONB columns and the
cascading foreign keys are read out of ``Base.metadata`` here and turned into plain DDL.
That keeps revision 0011 from drifting away from ``app/db/models.py`` - add an ``ondelete``
or a CHECK to a model and the migration picks it up.

Why a separate module instead of putting this inside the revision: the interesting part is
"what is missing from this database?", which is a pure function of a schema inspector.  As a
function of an inspector it is testable without a PostgreSQL server (this sandbox has none;
CI runs one), and the revision itself shrinks to two loops.

SQLite cannot add a CHECK constraint or change a foreign key on an existing table, so the
revision is a no-op there.  Fresh SQLite databases get all of this from
``Base.metadata.create_all()``, which ``SQLAlchemyStore`` already calls on startup.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import JSON, CheckConstraint, ForeignKeyConstraint
from sqlalchemy.dialects import postgresql

from app.db.models import Base

PG_DIALECT = "postgresql"


@dataclass(frozen=True)
class ForeignKeySpec:
    """One cascading foreign key, and the name the old revisions gave it."""

    table: str
    columns: tuple[str, ...]
    target: str
    target_columns: tuple[str, ...]
    ondelete: str
    name: str
    legacy_name: str


def desired_foreign_keys() -> list[ForeignKeySpec]:
    """Foreign keys with an ``ondelete`` rule, as declared by the models."""
    specs: list[ForeignKeySpec] = []
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint) or not constraint.ondelete:
                continue
            element = next(iter(constraint.elements))
            columns = tuple(column.name for column in constraint.columns)
            plain = "_".join(columns)
            specs.append(
                ForeignKeySpec(
                    table=table.name,
                    columns=columns,
                    target=element.column.table.name,
                    target_columns=(element.column.name,),
                    ondelete=constraint.ondelete.upper(),
                    name=f"fk_{table.name}_{plain}",
                    # What PostgreSQL named the constraint when revisions 0001-0010 created
                    # it inline (``<table>_<column>_fkey``); a downgrade restores that name.
                    legacy_name=f"{table.name}_{plain}_fkey",
                )
            )
    return specs


def desired_check_constraints() -> list[tuple[str, str, str]]:
    """``(table, name, condition)`` for every named CHECK constraint in the models."""
    checks: list[tuple[str, str, str]] = []
    for table in Base.metadata.sorted_tables:
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str):
                checks.append((table.name, constraint.name, str(constraint.sqltext)))
    return checks


def jsonb_columns() -> list[tuple[str, str]]:
    """``(table, column)`` pairs the models store as JSONB on PostgreSQL."""
    dialect = postgresql.dialect()
    pairs: list[tuple[str, str]] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if not isinstance(column.type, JSON):
                continue
            # `dialect_impl` hands back the driver's JSONB subclass (psycopg's `_PGJSONB`),
            # so this has to be an isinstance test rather than a class-name comparison.
            if isinstance(column.type.dialect_impl(dialect), postgresql.JSONB):
                pairs.append((table.name, column.name))
    return pairs


def _existing_tables(inspector) -> set[str]:
    """Tables this database actually has.

    The models describe the *final* schema, but revision 0011 runs before revision 0012 has
    created ``index_outbox``.  Without this guard the upgrade would try to constrain (or the
    downgrade to un-constrain) a table that is not there yet, which is a hard failure on the
    way to ``head`` - exactly the run that has to work.
    """
    return set(inspector.get_table_names())


def _column_types(inspector, table: str) -> dict[str, str]:
    return {column["name"]: str(column["type"]).upper() for column in inspector.get_columns(table)}


def _check_names(inspector, table: str) -> set[str]:
    return {
        check["name"]
        for check in inspector.get_check_constraints(table)
        if check.get("name")
    }


def _ondelete(foreign_key: dict) -> str:
    return str((foreign_key.get("options") or {}).get("ondelete") or "").upper()


def upgrade_statements(inspector) -> list[str]:
    """DDL that adds everything the models declare but this database is missing.

    Idempotent: a database that already matches the models produces an empty list.
    """
    statements: list[str] = []
    tables = _existing_tables(inspector)

    for table, name, condition in desired_check_constraints():
        if table not in tables:
            continue
        if name not in _check_names(inspector, table):
            statements.append(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})")

    for table, column in jsonb_columns():
        if table not in tables:
            continue
        if "JSONB" not in _column_types(inspector, table).get(column, ""):
            # NULLIF keeps rows whose text value is empty from aborting the migration.
            statements.append(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb "
                f"USING NULLIF({column}, '')::jsonb"
            )

    for spec in desired_foreign_keys():
        if spec.table not in tables:
            continue
        current = [
            foreign_key
            for foreign_key in inspector.get_foreign_keys(spec.table)
            if tuple(foreign_key["constrained_columns"]) == spec.columns
        ]
        if current and all(_ondelete(foreign_key) == spec.ondelete for foreign_key in current):
            continue
        for foreign_key in current:
            statements.append(
                f"ALTER TABLE {spec.table} DROP CONSTRAINT {foreign_key['name']}"
            )
        statements.append(
            f"ALTER TABLE {spec.table} ADD CONSTRAINT {spec.name} FOREIGN KEY "
            f"({', '.join(spec.columns)}) REFERENCES {spec.target} "
            f"({', '.join(spec.target_columns)}) ON DELETE {spec.ondelete}"
        )

    return statements


def downgrade_statements(inspector) -> list[str]:
    """Undo :func:`upgrade_statements`, restoring exactly what revision 0010 left behind."""
    statements: list[str] = []
    tables = _existing_tables(inspector)

    for table, name, _condition in desired_check_constraints():
        if table not in tables:
            continue
        if name in _check_names(inspector, table):
            statements.append(f"ALTER TABLE {table} DROP CONSTRAINT {name}")

    for table, column in jsonb_columns():
        if table not in tables:
            continue
        if "JSONB" in _column_types(inspector, table).get(column, ""):
            statements.append(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text"
            )

    for spec in desired_foreign_keys():
        if spec.table not in tables:
            continue
        names = {
            foreign_key["name"] for foreign_key in inspector.get_foreign_keys(spec.table)
        }
        if spec.name not in names:
            continue
        statements.append(f"ALTER TABLE {spec.table} DROP CONSTRAINT {spec.name}")
        statements.append(
            f"ALTER TABLE {spec.table} ADD CONSTRAINT {spec.legacy_name} FOREIGN KEY "
            f"({', '.join(spec.columns)}) REFERENCES {spec.target} "
            f"({', '.join(spec.target_columns)})"
        )

    return statements
