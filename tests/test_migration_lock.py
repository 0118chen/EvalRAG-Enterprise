"""The migration advisory lock, and the fact that ``env.py`` actually uses it.

Concurrent ``alembic upgrade head`` from several replicas is the failure mode this lock
removes; on PostgreSQL it is a ``pg_advisory_lock`` held on a dedicated connection.  The
SQLite branch must stay a no-op (single-writer file database - and the rest of the suite
migrates constantly).  The unit tests below drive the helper with a recording fake engine,
and the PostgreSQL test proves the blocking behaviour for real when
``EVALRAG_TEST_DATABASE_URL`` points at a throwaway database.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest
import sqlalchemy as sa
from alembic.config import Config

from alembic import command
from app.db.migration_lock import (
    LOCK_ENV_VAR,
    MIGRATION_LOCK_KEY,
    lock_enabled,
    migration_lock,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTGRES_URL_ENV = "EVALRAG_TEST_DATABASE_URL"


class _RecordingConnection:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    def execute(self, statement, parameters=None):
        sql = str(statement)
        if "pg_advisory_lock" in sql:
            self._log.append(("lock", (parameters or {}).get("key")))
        elif "pg_advisory_unlock" in sql:
            self._log.append(("unlock", (parameters or {}).get("key")))
        else:
            self._log.append(("sql", sql))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeEngine:
    """An engine that looks PostgreSQL to the helper and records what it is asked to run."""

    def __init__(self, dialect: str = "postgresql") -> None:
        self.dialect = SimpleNamespace(name=dialect)
        self.log: list[tuple[str, object]] = []
        self.connects = 0

    def connect(self) -> _RecordingConnection:
        self.connects += 1
        return _RecordingConnection(self.log)


def test_lock_key_is_a_positive_63_bit_integer() -> None:
    # PostgreSQL advisory locks take a signed 64-bit key; keeping it below 2**63 means the
    # same constant works for both the single-bigint and the two-int form of the call.
    assert isinstance(MIGRATION_LOCK_KEY, int)
    assert 0 < MIGRATION_LOCK_KEY < 2**63


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "", "  ", "enabled"])
def test_lock_is_enabled_unless_explicitly_disabled(value: str) -> None:
    assert lock_enabled({LOCK_ENV_VAR: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", " off "])
def test_lock_can_be_disabled_by_configuration(value: str) -> None:
    assert lock_enabled({LOCK_ENV_VAR: value}) is False


def test_postgres_engine_locks_before_the_migration_and_unlocks_after() -> None:
    engine = _FakeEngine()
    with migration_lock(engine) as locked:  # type: ignore[arg-type]
        assert locked is True
        engine.log.append(("body", None))
    assert engine.log == [
        ("lock", MIGRATION_LOCK_KEY),
        ("body", None),
        ("unlock", MIGRATION_LOCK_KEY),
    ]


def test_unlock_happens_even_when_the_migration_raises() -> None:
    engine = _FakeEngine()
    with pytest.raises(RuntimeError, match="boom"), migration_lock(engine):  # type: ignore[arg-type]
        raise RuntimeError("boom")
    # A failed migration must not leave the lock held: the next replica would hang forever.
    assert engine.log == [("lock", MIGRATION_LOCK_KEY), ("unlock", MIGRATION_LOCK_KEY)]


def test_non_postgres_dialects_do_not_connect_at_all(tmp_path: Path) -> None:
    database = tmp_path / "unused.db"
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with migration_lock(engine) as locked:
            assert locked is False
    finally:
        engine.dispose()
    # Not even a connection: opening one would create the file.
    assert not database.exists()


def test_disabled_lock_does_not_connect_to_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOCK_ENV_VAR, "0")
    engine = _FakeEngine()
    with migration_lock(engine) as locked:  # type: ignore[arg-type]
        assert locked is False
    assert engine.connects == 0
    assert engine.log == []


def test_env_module_wires_the_lock_around_the_migration_run() -> None:
    # The helper is only worth anything if env.py calls it, and that wiring cannot be
    # exercised locally (the SQLite branch is a no-op by design), so it is asserted on the
    # source.  tests/test_migration_lock.py::test_a_second_migrator_waits... proves the
    # runtime behaviour on a real PostgreSQL database in CI.
    source = (REPO_ROOT / "alembic" / "env.py").read_text(encoding="utf-8")
    assert "from app.db.migration_lock import migration_lock" in source
    assert "migration_lock(connectable)" in source


def test_a_second_migrator_waits_for_the_advisory_lock() -> None:
    url = os.environ.get(POSTGRES_URL_ENV)
    if not url:
        pytest.skip(f"set {POSTGRES_URL_ENV} to a throwaway PostgreSQL database")

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))

    holder = sa.create_engine(url)
    with holder.connect() as lock_connection:
        lock_connection.execute(
            sa.text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}
        )
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(command.upgrade, config, "head")
                with pytest.raises(FutureTimeout):
                    # While the lock is held the second migrator must not get through:
                    # this is the whole point of the revision.
                    pending.result(timeout=2)
        finally:
            lock_connection.execute(
                sa.text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY}
            )
        # Once the lock is released the migration completes normally.
        pending.result(timeout=60)
    holder.dispose()
