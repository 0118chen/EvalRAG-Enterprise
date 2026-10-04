"""Application service that joins retrieval, generation and citation evidence."""

from collections.abc import AsyncGenerator, AsyncIterator

from app.core.backends import LocalRetriever, Retriever
from app.core.citations import CitationReport, validate_citations
from app.core.ingestion import Chunk
from app.core.llm import LLM

# One refusal string for every path that cannot back an answer with evidence, so
# the API, the stream and the evaluation runner cannot drift apart.
INSUFFICIENT_EVIDENCE = "未找到足够依据，无法可靠回答该问题。"


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
    *,
    require_citation: bool = False,
) -> tuple[str, list[Chunk]]:
    """Answer from evidence, or refuse.

    Two different failures end in the same refusal, and both matter: an answer with
    no evidence at all, and an answer that cites a document or page we never
    retrieved. The second one is the more dangerous of the two because it looks
    authoritative - ``citation_report`` is what tells them apart in the trace.
    """
    evidence = [chunk for chunk, _ in results]
    if not evidence:
        return INSUFFICIENT_EVIDENCE, []
    answer = await llm.answer(question, build_context(results))
    report = validate_citations(answer, evidence)
    if not report.ok or (require_citation and not report.cited):
        return INSUFFICIENT_EVIDENCE, []
    return answer, evidence


def evidence_report(answer: str, results: list[tuple[Chunk, float]]) -> CitationReport:
    """Check an already-generated answer; the streaming path cannot retract tokens."""
    return validate_citations(answer, [chunk for chunk, _ in results])


async def stream_with_evidence(
    llm: LLM,
    question: str,
    results: list[tuple[Chunk, float]],
) -> AsyncGenerator[str, None]:
    evidence = [chunk for chunk, _ in results]
    if not evidence:
        yield INSUFFICIENT_EVIDENCE
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
        yield INSUFFICIENT_EVIDENCE


async def stream_text(text: str, size: int = 24) -> AsyncIterator[str]:
    for start in range(0, len(text), size):
        yield text[start:start + size]
