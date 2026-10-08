"""Status CHECK constraints, JSONB columns and cascading foreign keys (PostgreSQL).

Before this revision the schema had three shapes the application could not rely on:

* ``documents.status`` / ``evaluations.status`` accepted any string, so a typo produced a
  row that no query for a real status would ever find again;
* ``parameters_json`` / ``results_json`` / ``expected_evidence_json`` were TEXT, so
  PostgreSQL could not validate them and could not index into them;
* the foreign keys had no ``ON DELETE`` rule, so deleting a knowledge base either failed or
  left documents, chunks and datasets behind.

The decisions live in :mod:`app.db.postgres_schema` (pure functions of a schema inspector,
which keeps them testable without a PostgreSQL server); this revision only executes them.
SQLite is deliberately untouched: it cannot add a CHECK constraint to an existing table, and
a fresh local database gets all three from ``Base.metadata.create_all()``.
"""

import sqlalchemy as sa

from alembic import op
from app.db import postgres_schema

revision = "0011_postgres_constraints"
down_revision = "0010_evaluation_evidence_mode"
branch_labels = None
depends_on = None


def _statements(build) -> list[str]:
    bind = op.get_bind()
    if bind.dialect.name != postgres_schema.PG_DIALECT:
        return []
    return build(sa.inspect(bind))


def upgrade() -> None:
    for statement in _statements(postgres_schema.upgrade_statements):
        op.execute(sa.text(statement))


def downgrade() -> None:
    for statement in _statements(postgres_schema.downgrade_statements):
        op.execute(sa.text(statement))
