"""Application service that joins retrieval, generation and citation evidence."""

from collections.abc import AsyncIterator

from app.core.backends import LocalRetriever, Retriever
from app.core.citations import validate_citations
from app.core.ingestion import Chunk
from app.core.llm import LLM


def build_context(results: list[tuple[Chunk, float]]) -> str:
    return "\n\n".join(f"[{chunk.document_id} p.{chunk.page}] {chunk.text}" for chunk, _ in results)


async def answer_question(llm: LLM, question: str, chunks: list[Chunk], top_k: int, mode: str, retriever: Retriever | None = None) -> tuple[str, list[Chunk]]:
    selected = retriever or LocalRetriever(chunks, mode)
    results = [(chunk, score) for chunk, score in await selected.search(question, top_k) if score > 0]
    return await answer_with_evidence(llm, question, results)


async def answer_with_evidence(
    llm: LLM,
    question: str,
    results: list[tuple[Chunk, float]],
) -> tuple[str, list[Chunk]]:
    evidence = [chunk for chunk, _ in results]
    answer = await llm.answer(question, build_context(results))
    if not validate_citations(answer, evidence):
        return "未找到足够依据，无法可靠回答该问题。", []
    return answer, evidence


async def stream_with_evidence(
    llm: LLM,
    question: str,
    results: list[tuple[Chunk, float]],
) -> AsyncIterator[str]:
    evidence = [chunk for chunk, _ in results]
    if not evidence:
        yield "未找到足够依据，无法可靠回答该问题。"
        return

    produced = False
    stream = llm.stream(question, build_context(results))
    try:
        async for token in stream:
            if token:
                produced = True
                yield token
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
    if not produced:
        yield "未找到足够依据，无法可靠回答该问题。"


async def stream_text(text: str, size: int = 24) -> AsyncIterator[str]:
    for start in range(0, len(text), size):
        yield text[start:start + size]
