"""Guard against schema drift between the Alembic migrations and the models."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def _migrated_sqlite_url(tmp_path: Path) -> str:
    database = tmp_path / "parity.db"
    url = f"sqlite:///{database.as_posix()}"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
    )
    return url


def test_alembic_head_schema_matches_sqlalchemy_metadata(tmp_path: Path) -> None:
    """A fresh migration must produce exactly the schema the application expects."""
    inspector = inspect(create_engine(_migrated_sqlite_url(tmp_path)))

    migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
    assert migrated_tables == set(Base.metadata.tables)

    for table in sorted(migrated_tables):
        migrated_columns = {column["name"] for column in inspector.get_columns(table)}
        model_columns = {column.name for column in Base.metadata.tables[table].columns}
        assert migrated_columns == model_columns, f"column drift in {table}"

        migrated_indexes = {
            index["name"] for index in inspector.get_indexes(table) if index["name"]
        }
        model_indexes = {index.name for index in Base.metadata.tables[table].indexes}
        assert migrated_indexes == model_indexes, f"index drift in {table}"
