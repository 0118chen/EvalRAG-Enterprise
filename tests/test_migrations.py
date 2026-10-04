"""Every downgrade restores the schema its upgrade found.

A migration whose ``downgrade()`` only does part of the work is invisible until the day a
release has to be rolled back, and then the database is left in a shape no revision
describes - the next ``upgrade`` sees a half-migrated table and adds columns on top of the
leftovers. Four of this project's ten revisions had that defect, so the property is now
tested instead of assumed: for each revision, snapshot the schema, upgrade one step,
downgrade back, and compare.

The test runs against SQLite by default and against PostgreSQL when
``EVALRAG_TEST_DATABASE_URL`` is set. That database is treated as disposable: the test
drops every table in it (it ends by migrating back to head).
"""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTGRES_URL_ENV = "EVALRAG_TEST_DATABASE_URL"


@dataclass
class Migrations:
    config: Config
    url: str


def revision_order() -> list[str]:
    """The revision ids in dependency order, read from the migration files themselves."""
    revisions = []
    for path in sorted((REPO_ROOT / "alembic" / "versions").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        match = next(line for line in text.splitlines() if line.startswith("revision = "))
        revisions.append(match.split('"')[1])
    return revisions


def schema(url: str) -> dict[str, Any]:
    """Columns, indexes, unique constraints and foreign keys of every table."""
    engine = sa.create_engine(url)
    try:
        inspector = sa.inspect(engine)
        state: dict[str, Any] = {}
        for table in inspector.get_table_names():
            if table == "alembic_version":
                continue
            state[table] = (
                {
                    column["name"]: (str(column["type"]), column["nullable"])
                    for column in inspector.get_columns(table)
                },
                sorted(index["name"] for index in inspector.get_indexes(table)),
                sorted(
                    (unique.get("name"), tuple(unique["column_names"]))
                    for unique in inspector.get_unique_constraints(table)
                ),
                sorted(
                    (foreign.get("name"), tuple(foreign["constrained_columns"]))
                    for foreign in inspector.get_foreign_keys(table)
                ),
            )
        return state
    finally:
        engine.dispose()


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable description of what a downgrade failed to undo."""
    problems = []
    for table in sorted(set(before) | set(after)):
        if table not in after:
            problems.append(f"lost table {table}")
            continue
        if table not in before:
            problems.append(f"left behind table {table}")
            continue
        for position, label in enumerate(("columns", "indexes", "uniques", "foreign keys")):
            old, new = before[table][position], after[table][position]
            if old == new:
                continue
            if label == "columns":
                problems.append(
                    f"{table}.columns left={sorted(set(new) - set(old))} "
                    f"lost={sorted(set(old) - set(new))} "
                    f"changed={sorted(k for k in set(old) & set(new) if old[k] != new[k])}"
                )
            else:
                problems.append(
                    f"{table}.{label} left={sorted(set(new) - set(old))} "
                    f"lost={sorted(set(old) - set(new))}"
                )
    return problems


@pytest.fixture(params=["sqlite", "postgres"])
def migrations(request, tmp_path: Path, monkeypatch) -> Migrations:
    if request.param == "postgres":
        url = os.environ.get(POSTGRES_URL_ENV)
        if not url:
            pytest.skip(f"set {POSTGRES_URL_ENV} to a throwaway, migrated PostgreSQL database")
    else:
        if sqlite3.sqlite_version_info < (3, 35):
            pytest.skip("downgrades use ALTER TABLE ... DROP COLUMN, which needs SQLite 3.35+")
        url = f"sqlite:///{tmp_path / 'migrations.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    yield Migrations(config=config, url=url)
    # Leave the database migrated, so other tests in the same run find a usable schema.
    command.upgrade(config, "head")


def test_every_downgrade_restores_the_previous_schema(migrations: Migrations) -> None:
    order = revision_order()
    assert order, "no migrations were found"

    command.downgrade(migrations.config, "base")
    for previous, revision in zip(["base", *order[:-1]], order):
        before = schema(migrations.url)

        command.upgrade(migrations.config, revision)
        command.downgrade(migrations.config, previous)

        problems = differences(before, schema(migrations.url))
        assert problems == [], (
            f"{revision} does not restore the schema {previous} left behind: {problems}"
        )

        # Back to the top of this step: that is the "previous" state of the next pair, and
        # re-upgrading after a downgrade also proves the upgrade path is re-runnable.
        command.upgrade(migrations.config, revision)
