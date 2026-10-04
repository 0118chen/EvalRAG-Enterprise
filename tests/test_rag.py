import asyncio

from app.core.ingestion import Chunk
from app.core.llm import MockLLM
from app.core.rag import (
    INSUFFICIENT_EVIDENCE,
    answer_question,
    answer_with_evidence,
    build_context,
)

EVIDENCE = [(Chunk("c", "doc-1", 1, "loan policy effective date January first"), 0.9)]


class _FixedAnswerLLM:
    """Returns exactly the text a test wants to check, with no retrieval of its own."""

    def __init__(self, answer: str) -> None:
        self.answer_text = answer

    async def answer(self, question: str, context: str) -> str:
        return self.answer_text


def test_context_contains_page_reference() -> None:
    assert "doc-1 p.2" in build_context([(Chunk("c", "doc-1", 2, "evidence"), 1.0)])


def test_answer_question_returns_only_supported_evidence() -> None:
    chunks = [Chunk("c", "doc-1", 1, "loan policy effective date January first")]
    answer, evidence = asyncio.run(answer_question(MockLLM(), "loan policy effective date", chunks, 3, "hybrid"))
    assert answer and evidence


def test_an_answer_with_no_evidence_at_all_is_refused() -> None:
    answer, evidence = asyncio.run(answer_with_evidence(MockLLM(), "q", []))

    assert answer == INSUFFICIENT_EVIDENCE
    assert evidence == []


def test_an_answer_citing_a_document_we_never_retrieved_is_refused() -> None:
    # This is the failure the citation check exists for: the answer looks authoritative
    # and cites something that was never in the context.
    llm = _FixedAnswerLLM("依据 [doc-9 p.1] 的规定，期限为三十年。")

    answer, evidence = asyncio.run(answer_with_evidence(llm, "q", EVIDENCE))

    assert answer == INSUFFICIENT_EVIDENCE
    assert evidence == []


def test_an_answer_citing_a_page_we_never_retrieved_is_refused() -> None:
    llm = _FixedAnswerLLM("依据 [doc-1 p.7] 的规定，期限为三十年。")

    answer, _evidence = asyncio.run(answer_with_evidence(llm, "q", EVIDENCE))

    assert answer == INSUFFICIENT_EVIDENCE


def test_a_grounded_citation_is_returned_unchanged() -> None:
    text = "依据 [doc-1 p.1] 的规定，期限为三十年。"
    llm = _FixedAnswerLLM(text)

    answer, evidence = asyncio.run(answer_with_evidence(llm, "q", EVIDENCE))

    assert answer == text
    assert evidence == [EVIDENCE[0][0]]


def test_requiring_a_citation_is_opt_in() -> None:
    llm = _FixedAnswerLLM("期限为三十年。")

    # Default: no citation is not an integrity failure, so mock and local providers work.
    answer, _evidence = asyncio.run(answer_with_evidence(llm, "q", EVIDENCE))
    assert answer == "期限为三十年。"

    # Opted in: an answer that cites nothing cannot be verified, so it is refused.
    answer, evidence = asyncio.run(
        answer_with_evidence(llm, "q", EVIDENCE, require_citation=True)
    )
    assert answer == INSUFFICIENT_EVIDENCE
    assert evidence == []
