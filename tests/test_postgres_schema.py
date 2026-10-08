"""Revision 0011 (CHECK, JSONB, cascading foreign keys) without a PostgreSQL server.

The interesting logic - "what is missing from this database?" - is a pure function of a
schema inspector, so these tests drive it with a fake inspector shaped like revision 0010
and like a migrated database. The real DDL runs in CI, where a PostgreSQL service container
is available; here we pin the statements it is handed.
"""

from __future__ import annotations

from app.db import postgres_schema

CASCADE_TARGETS = {
    ("documents", ("knowledge_base_id",)),
    ("chunks", ("document_id",)),
    ("chunks", ("knowledge_base_id",)),
    ("evaluation_datasets", ("knowledge_base_id",)),
    ("evaluation_examples", ("dataset_id",)),
}
SET_NULL_TARGETS = {
    ("evaluations", ("knowledge_base_id",)),
    ("evaluations", ("dataset_id",)),
}
JSONB_TARGETS = {
    ("evaluations", "parameters_json"),
    ("evaluations", "results_json"),
    ("evaluation_examples", "expected_evidence_json"),
}
OWNED_TABLES = {
    "documents",
    "chunks",
    "evaluation_datasets",
    "evaluation_examples",
    "evaluations",
}


class FakeInspector:
    """The four inspector calls the migration makes, answered from a fixed shape."""

    def __init__(self, tables: dict[str, dict], *, existing: set[str] | None = None) -> None:
        self._tables = tables
        # Which tables the database actually has. Revision 0011 runs before 0012 creates
        # ``index_outbox``, so the shapes below deliberately report the 0010-era set.
        self._existing = set(tables) if existing is None else set(existing)

    def get_table_names(self) -> list[str]:
        return sorted(self._existing)

    def get_columns(self, table: str) -> list[dict]:
        return [
            {"name": name, "type": type_}
            for name, type_ in self._tables.get(table, {}).get("columns", {}).items()
        ]

    def get_check_constraints(self, table: str) -> list[dict]:
        return [{"name": name} for name in self._tables.get(table, {}).get("checks", ())]

    def get_foreign_keys(self, table: str) -> list[dict]:
        return [
            {
                "name": foreign_key["name"],
                "constrained_columns": list(foreign_key["columns"]),
                "options": foreign_key["options"],
            }
            for foreign_key in self._tables.get(table, {}).get("foreign_keys", ())
        ]


def _tables() -> dict[str, dict]:
    tables: dict[str, dict] = {}
    for table, column in postgres_schema.jsonb_columns():
        tables.setdefault(table, {"columns": {}, "checks": set(), "foreign_keys": []})
        tables[table]["columns"][column] = "TEXT"
    for table, _name, _condition in postgres_schema.desired_check_constraints():
        tables.setdefault(table, {"columns": {}, "checks": set(), "foreign_keys": []})
    for spec in postgres_schema.desired_foreign_keys():
        tables.setdefault(spec.table, {"columns": {}, "checks": set(), "foreign_keys": []})
    return tables


def schema_before_0011() -> FakeInspector:
    """What revisions 0001-0010 leave behind: TEXT columns, unnamed rules, no cascades."""
    tables = _tables()
    for spec in postgres_schema.desired_foreign_keys():
        tables[spec.table]["foreign_keys"].append(
            {"name": spec.legacy_name, "columns": spec.columns, "options": {}}
        )
    return FakeInspector(tables, existing=OWNED_TABLES)


def schema_after_0011() -> FakeInspector:
    """The same database once revision 0011 has run."""
    tables = _tables()
    for table, column in postgres_schema.jsonb_columns():
        tables[table]["columns"][column] = "JSONB"
    for table, name, _condition in postgres_schema.desired_check_constraints():
        tables[table]["checks"].add(name)
    for spec in postgres_schema.desired_foreign_keys():
        tables[spec.table]["foreign_keys"].append(
            {
                "name": spec.name,
                "columns": spec.columns,
                "options": {"ondelete": spec.ondelete},
            }
        )
    return FakeInspector(tables, existing=OWNED_TABLES)


# --- what the models declare ------------------------------------------------------------------


def test_status_columns_are_constrained_to_the_documented_values() -> None:
    checks = {
        (table, name): condition
        for table, name, condition in postgres_schema.desired_check_constraints()
    }
    assert set(checks) == {
        ("documents", "ck_documents_status"),
        ("evaluations", "ck_evaluations_status"),
        ("index_outbox", "ck_index_outbox_status"),
        ("index_outbox", "ck_index_outbox_operation"),
    }
    assert checks[("documents", "ck_documents_status")] == (
        "status IN ('pending', 'processing', 'ready', 'failed', 'needs_ocr')"
    )
    assert checks[("evaluations", "ck_evaluations_status")] == (
        "status IN ('queued', 'running', 'completed', 'failed')"
    )
    assert checks[("index_outbox", "ck_index_outbox_status")] == (
        "status IN ('pending', 'processing', 'done', 'failed')"
    )
    assert checks[("index_outbox", "ck_index_outbox_operation")] == (
        "operation IN ('upsert', 'delete')"
    )


def test_json_columns_are_the_three_documents_grow_into() -> None:
    assert set(postgres_schema.jsonb_columns()) == JSONB_TARGETS


def test_deleting_a_knowledge_base_cascades_to_its_content_but_not_its_evaluations() -> None:
    """The point of the whole revision: content dies with the corpus, measurements survive."""
    cascades = {
        (spec.table, spec.columns)
        for spec in postgres_schema.desired_foreign_keys()
        if spec.ondelete == "CASCADE"
    }
    set_nulls = {
        (spec.table, spec.columns)
        for spec in postgres_schema.desired_foreign_keys()
        if spec.ondelete == "SET NULL"
    }
    assert cascades == CASCADE_TARGETS
    assert set_nulls == SET_NULL_TARGETS
    assert cascades | set_nulls == {
        (spec.table, spec.columns) for spec in postgres_schema.desired_foreign_keys()
    }


def test_constraint_names_are_stable_so_a_downgrade_can_find_them() -> None:
    for spec in postgres_schema.desired_foreign_keys():
        assert spec.name == f"fk_{spec.table}_{'_'.join(spec.columns)}"
        assert spec.legacy_name == f"{spec.table}_{'_'.join(spec.columns)}_fkey"
    assert len({spec.name for spec in postgres_schema.desired_foreign_keys()}) == len(
        postgres_schema.desired_foreign_keys()
    )


# --- the statements revision 0011 executes ----------------------------------------------------


def test_upgrade_adds_the_check_constraints() -> None:
    statements = postgres_schema.upgrade_statements(schema_before_0011())
    assert (
        "ALTER TABLE documents ADD CONSTRAINT ck_documents_status CHECK "
        "(status IN ('pending', 'processing', 'ready', 'failed', 'needs_ocr'))"
    ) in statements
    assert (
        "ALTER TABLE evaluations ADD CONSTRAINT ck_evaluations_status CHECK "
        "(status IN ('queued', 'running', 'completed', 'failed'))"
    ) in statements


def test_upgrade_converts_the_json_columns_to_jsonb() -> None:
    statements = postgres_schema.upgrade_statements(schema_before_0011())
    for table, column in sorted(JSONB_TARGETS):
        assert (
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb "
            f"USING NULLIF({column}, '')::jsonb"
        ) in statements


def test_upgrade_replaces_the_foreign_keys_with_cascading_ones() -> None:
    statements = postgres_schema.upgrade_statements(schema_before_0011())
    assert (
        "ALTER TABLE documents DROP CONSTRAINT documents_knowledge_base_id_fkey"
    ) in statements
    assert (
        "ALTER TABLE documents ADD CONSTRAINT fk_documents_knowledge_base_id FOREIGN KEY "
        "(knowledge_base_id) REFERENCES knowledge_bases (id) ON DELETE CASCADE"
    ) in statements
    assert (
        "ALTER TABLE evaluations ADD CONSTRAINT fk_evaluations_dataset_id FOREIGN KEY "
        "(dataset_id) REFERENCES evaluation_datasets (id) ON DELETE SET NULL"
    ) in statements
    specs = postgres_schema.desired_foreign_keys()
    assert sum(" FOREIGN KEY " in statement for statement in statements) == len(specs)


def test_upgrade_drops_every_old_constraint_exactly_once() -> None:
    statements = postgres_schema.upgrade_statements(schema_before_0011())
    dropped = [
        statement.split("DROP CONSTRAINT ")[1]
        for statement in statements
        if "DROP CONSTRAINT " in statement
    ]
    assert sorted(dropped) == sorted(
        spec.legacy_name for spec in postgres_schema.desired_foreign_keys()
    )


def test_upgrade_changes_nothing_when_the_database_already_matches() -> None:
    assert postgres_schema.upgrade_statements(schema_after_0011()) == []


def test_upgrade_touches_only_the_tables_it_owns() -> None:
    statements = postgres_schema.upgrade_statements(schema_before_0011())
    assert {statement.split()[2] for statement in statements} == OWNED_TABLES


def test_downgrade_puts_the_previous_shape_back() -> None:
    statements = postgres_schema.downgrade_statements(schema_after_0011())
    for table, name, _condition in postgres_schema.desired_check_constraints():
        if table not in OWNED_TABLES:
            # ``index_outbox`` arrives with revision 0012, which is undone before this one.
            continue
        assert f"ALTER TABLE {table} DROP CONSTRAINT {name}" in statements
    for table, column in sorted(JSONB_TARGETS):
        assert (
            f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text USING {column}::text"
        ) in statements
    for spec in postgres_schema.desired_foreign_keys():
        assert f"ALTER TABLE {spec.table} DROP CONSTRAINT {spec.name}" in statements
        assert (
            f"ALTER TABLE {spec.table} ADD CONSTRAINT {spec.legacy_name} FOREIGN KEY "
            f"({', '.join(spec.columns)}) REFERENCES {spec.target} "
            f"({', '.join(spec.target_columns)})"
        ) in statements
    assert not any("ON DELETE" in statement for statement in statements)


def test_downgrade_is_idempotent_before_the_upgrade_ran() -> None:
    assert postgres_schema.downgrade_statements(schema_before_0011()) == []


def test_statements_never_target_a_table_a_later_revision_creates() -> None:
    """``index_outbox`` is declared by the models but only exists from revision 0012 on.

    Revision 0011 runs first, so it must not try to constrain (or un-constrain) a table that
    is not there yet - that would break ``alembic upgrade head`` on every fresh database.
    """
    assert ("index_outbox", "ck_index_outbox_status") in {
        (table, name) for table, name, _condition in postgres_schema.desired_check_constraints()
    }
    assert not any(
        "index_outbox" in statement
        for statement in postgres_schema.upgrade_statements(schema_before_0011())
    )
    assert not any(
        "index_outbox" in statement
        for statement in postgres_schema.downgrade_statements(schema_after_0011())
    )


def test_a_table_that_exists_is_still_migrated() -> None:
    """The guard is about existence, not about skipping new tables for good."""
    inspector = FakeInspector(_tables(), existing=OWNED_TABLES | {"index_outbox"})
    conditions = {
        name: condition
        for table, name, condition in postgres_schema.desired_check_constraints()
        if table == "index_outbox"
    }
    statements = postgres_schema.upgrade_statements(inspector)
    for name, condition in conditions.items():
        assert f"ALTER TABLE index_outbox ADD CONSTRAINT {name} CHECK ({condition})" in statements
