"""Application service that joins retrieval, generation and citation evidence."""

from collections.abc import AsyncIterator

from app.core.citations import validate_citations
from app.core.ingestion import Chunk
from app.core.llm import LLM
from app.core.retrieval import retrieve


def build_context(results: list[tuple[Chunk, float]]) -> str:
    return "\n\n".join(f"[{chunk.document_id} p.{chunk.page}] {chunk.text}" for chunk, _ in results)


async def answer_question(llm: LLM, question: str, chunks: list[Chunk], top_k: int, mode: str) -> tuple[str, list[Chunk]]:
    results = [(chunk, score) for chunk, score in retrieve(question, chunks, top_k, mode) if score > 0]
    evidence = [chunk for chunk, _ in results]
    answer = await llm.answer(question, build_context(results))
    if not validate_citations(answer, evidence):
        return "未找到足够依据，无法可靠回答该问题。", []
    return answer, evidence


async def stream_text(text: str, size: int = 24) -> AsyncIterator[str]:
    for start in range(0, len(text), size):
        yield text[start:start + size]

