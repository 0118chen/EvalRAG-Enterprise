import asyncio

from app.core.ingestion import Chunk
from app.core.llm import MockLLM
from app.core.rag import answer_question, build_context


def test_context_contains_page_reference() -> None:
    assert "doc-1 p.2" in build_context([(Chunk("c", "doc-1", 2, "evidence"), 1.0)])


def test_answer_question_returns_only_supported_evidence() -> None:
    chunks = [Chunk("c", "doc-1", 1, "loan policy effective date January first")]
    answer, evidence = asyncio.run(answer_question(MockLLM(), "loan policy effective date", chunks, 3, "hybrid"))
    assert answer and evidence
