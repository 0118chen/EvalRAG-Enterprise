"""Small SQLite persistence layer used by the local MVP.

The interface is deliberately narrow so it can later be replaced by PostgreSQL
without changing the API or retrieval code.
"""

import sqlite3
from pathlib import Path
from threading import Lock

from app.core.ingestion import Chunk
from app.schemas import Document, KnowledgeBase


class SQLiteStore:
    def __init__(self, path: str = "data/evalrag.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge_bases (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, knowledge_base_id TEXT NOT NULL,
                chunks INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ready', FOREIGN KEY(knowledge_base_id) REFERENCES knowledge_bases(id)
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, page INTEGER NOT NULL,
                text TEXT NOT NULL, knowledge_base_id TEXT NOT NULL
            );
            """)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
            if "status" not in columns:
                connection.execute("ALTER TABLE documents ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def save_knowledge_base(self, kb: KnowledgeBase) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("INSERT INTO knowledge_bases VALUES (?, ?, ?, ?)", (kb.id, kb.tenant_id, kb.name, kb.description))

    def get_knowledge_base(self, kb_id: str, tenant_id: str) -> KnowledgeBase | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM knowledge_bases WHERE id=? AND tenant_id=?", (kb_id, tenant_id)).fetchone()
        return KnowledgeBase(**dict(row)) if row else None

    def save_document(self, document: Document, chunks: list[Chunk]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("INSERT INTO documents VALUES (?, ?, ?, ?, ?)", (document.id, document.filename, document.knowledge_base_id, document.chunks, document.status))
            connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)", [(c.id, c.document_id, c.page, c.text, document.knowledge_base_id) for c in chunks])

    def update_document_status(self, document_id: str, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("UPDATE documents SET status=? WHERE id=?", (status, document_id))

    def delete_document(self, document_id: str, tenant_id: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute("""SELECT d.id FROM documents d JOIN knowledge_bases k ON k.id=d.knowledge_base_id
                                        WHERE d.id=? AND k.tenant_id=?""", (document_id, tenant_id)).fetchone()
            if not row:
                return False
            connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))
            connection.execute("DELETE FROM documents WHERE id=?", (document_id,))
            return True

    def get_chunks(self, kb_id: str) -> list[Chunk]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, document_id, page, text FROM chunks WHERE knowledge_base_id=?", (kb_id,)).fetchall()
        return [Chunk(**dict(row)) for row in rows]

    def get_document(self, document_id: str, tenant_id: str) -> Document | None:
        with self._connect() as connection:
            row = connection.execute("""SELECT d.* FROM documents d JOIN knowledge_bases k ON k.id=d.knowledge_base_id
                                        WHERE d.id=? AND k.tenant_id=?""", (document_id, tenant_id)).fetchone()
        return Document(**dict(row)) if row else None
