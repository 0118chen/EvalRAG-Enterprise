"""Serialize concurrent schema migrations with a PostgreSQL advisory lock.

Every replica of the API and every worker process runs ``alembic upgrade head`` when it
boots.  On PostgreSQL that is safe only if the replicas do not race: two processes that
read ``alembic_version`` at the same time both decide to apply revision *N*, and the loser
fails on an already-created table (or, worse, half-applies a revision whose ``upgrade``
is not idempotent).  Alembic itself has no cross-process lock, so the lock lives here:

* PostgreSQL runs get a session-level ``pg_advisory_lock`` on a *dedicated connection*.
  The lock connection is deliberately separate from the connection that runs the
  migrations, so the lock cannot be released early by a commit or a rollback of the
  migration transaction, and it cannot leak into the application's pool either.
* Every other dialect (SQLite in tests and the local stack) is a single-writer database
  reached over a file, so there is no cross-process race to serialize and the helper is a
  no-op.  That keeps ``alembic upgrade head`` on SQLite exactly as it was.
* ``EVALRAG_MIGRATION_LOCK=0`` disables the lock (documented escape hatch for a database
  whose role is not allowed to take advisory locks).  Values ``0/false/no/off`` disable
  it; anything else - including unset - keeps it on.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Engine

#: Fixed 63-bit key, unique to this project's migrations.  It is a constant (rather than a
#: hash of the database name) so that an operator can recognise it in ``pg_locks`` and so
#: that two different services sharing one database still serialize against each other.
MIGRATION_LOCK_KEY = 5_710_214_013_041_212

#: Set to ``0`` / ``false`` / ``no`` / ``off`` to run migrations without the lock.
LOCK_ENV_VAR = "EVALRAG_MIGRATION_LOCK"

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})
_LOCK_SQL = "SELECT pg_advisory_lock(:key)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(:key)"


def lock_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the advisory lock is enabled for this process."""
    value = (env if env is not None else os.environ).get(LOCK_ENV_VAR, "")
    return value.strip().lower() not in _DISABLED_VALUES


@contextmanager
def migration_lock(engine: Engine) -> Iterator[bool]:
    """Hold the migration advisory lock for the duration of the block.

    Yields ``True`` when the lock was actually taken and ``False`` when the block ran
    unlocked (non-PostgreSQL dialect, or the lock disabled by configuration).
    """
    if not lock_enabled() or engine.dialect.name != "postgresql":
        yield False
        return

    with engine.connect() as lock_connection:
        lock_connection.execute(text(_LOCK_SQL), {"key": MIGRATION_LOCK_KEY})
        try:
            yield True
        finally:
            lock_connection.execute(text(_UNLOCK_SQL), {"key": MIGRATION_LOCK_KEY})
