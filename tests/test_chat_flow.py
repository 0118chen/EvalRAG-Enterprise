import asyncio

from app.core.backends import HybridRetriever, LocalRetriever
from app.core.ingestion import Chunk
from app.core.llm import MockLLM
from app.core.rag import answer_question


def test_chat_flow_uses_hybrid_retriever() -> None:
    chunks = [Chunk("c1", "policy", 3, "loan policy effective date January first"), Chunk("c2", "other", 1, "unrelated text")]
    retriever = HybridRetriever(LocalRetriever(chunks, "dense"), LocalRetriever(chunks, "sparse"))
    answer, evidence = asyncio.run(answer_question(MockLLM(), "loan policy effective date", chunks, 2, "hybrid", retriever))
    assert answer.startswith("基于检索证据")
    assert evidence[0].document_id == "policy"

