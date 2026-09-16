from app.core.citations import validate_citations
from app.core.ingestion import Chunk


def test_citation_validation_requires_evidence() -> None:
    assert not validate_citations("answer", [])
    assert validate_citations("answer", [Chunk("c", "d", 1, "evidence")])

