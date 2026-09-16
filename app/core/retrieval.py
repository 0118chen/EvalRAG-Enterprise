import re
from collections import Counter

from app.core.ingestion import Chunk


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff]+", text.lower())


def bm25_score(query: str, text: str) -> float:
    query_terms = set(_tokens(query))
    document_terms = Counter(_tokens(text))
    return float(sum(document_terms[term] for term in query_terms))


def retrieve(query: str, chunks: list[Chunk], top_k: int = 5, mode: str = "hybrid") -> list[tuple[Chunk, float]]:
    scored = [(chunk, bm25_score(query, chunk.text)) for chunk in chunks]
    if mode == "dense":
        scored = [(chunk, float(len(set(_tokens(query)) & set(_tokens(chunk.text))))) for chunk in chunks]
    return sorted(scored, key=lambda item: item[1], reverse=True)[:top_k]

