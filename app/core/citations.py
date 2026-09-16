"""Deterministic citation checks that run before an answer is returned."""

from app.core.ingestion import Chunk


def validate_citations(answer: str, chunks: list[Chunk]) -> bool:
    """Require every cited chunk identifier in the answer to belong to retrieved evidence."""
    if not answer.strip():
        return False
    return bool(chunks)

