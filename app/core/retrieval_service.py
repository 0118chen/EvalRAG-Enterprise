"""Unified query rewrite, hybrid retrieval, reranking and cache pipeline."""

import asyncio
import hashlib
from dataclasses import dataclass

from app.config import Settings
from app.core.backends import Retriever, create_retriever
from app.core.cache import Cache, cache_key
from app.core.ingestion import Chunk
from app.core.observability import TraceManager
from app.core.query_rewrite import QueryRewriter
from app.core.reranking import Reranker
from app.core.retrieval import reciprocal_rank_fusion


@dataclass(frozen=True)
class RetrievalResult:
    results: list[tuple[Chunk, float]]
    trace_id: str
    cache_hit: bool
    rewritten_queries: list[str]
    candidate_count: int
    reranked: bool
    document_version: str | None


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        traces: TraceManager,
        cache: Cache,
        query_rewriter: QueryRewriter,
        reranker: Reranker,
    ) -> None:
        self.settings = settings
        self.traces = traces
        self.cache = cache
        self.query_rewriter = query_rewriter
        self.reranker = reranker

    async def search(
        self,
        *,
        tenant_id: str,
        knowledge_base_id: str,
        question: str,
        chunks: list[Chunk],
        top_k: int,
        mode: str,
        document_version: str | None = "latest",
        rerank: bool | None = None,
        query_rewrite: bool | None = None,
    ) -> RetrievalResult:
        use_rerank = self.settings.rerank_enabled if rerank is None else rerank
        use_rewrite = (
            self.settings.query_rewrite_enabled
            if query_rewrite is None
            else query_rewrite
        )
        metadata = self.traces.metadata(
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            retrieval_mode=mode,
        )
        metadata.update(
            {
                "document_version": document_version,
                "rerank": use_rerank,
                "query_rewrite": use_rewrite,
            }
        )
        with self.traces.span(
            "retrieval",
            run_type="retriever",
            metadata=metadata,
            inputs={"question": question, "top_k": top_k},
        ) as retrieval_span:
            corpus_hash = hashlib.sha256()
            for chunk in chunks:
                corpus_hash.update(chunk.id.encode("utf-8"))
                corpus_hash.update(chunk.version.encode("utf-8"))
                corpus_hash.update(
                    hashlib.sha256(chunk.text.encode("utf-8")).digest()
                )
            key = cache_key(
                "retrieval",
                {
                    "tenant_id": tenant_id,
                    "knowledge_base_id": knowledge_base_id,
                    "question": question,
                    "top_k": top_k,
                    "mode": mode,
                    "document_version": document_version,
                    "rerank": use_rerank,
                    "query_rewrite": use_rewrite,
                    "rag_version": self.settings.rag_version,
                    "embedding_provider": self.settings.embedding_provider,
                    "embedding_model": self.settings.embedding_model,
                    "embedding_dimensions": self.settings.embedding_dimensions,
                    "dense_backend": self.settings.dense_retrieval_backend,
                    "sparse_backend": self.settings.sparse_retrieval_backend,
                    "milvus_collection": self.settings.milvus_collection,
                    "elasticsearch_index": self.settings.elasticsearch_index,
                    "corpus_fingerprint": corpus_hash.hexdigest(),
                },
            )
            cached = await self.cache.get(key)
            if cached:
                by_id = {chunk.id: chunk for chunk in chunks}
                results = [
                    (by_id[item["chunk_id"]], float(item["score"]))
                    for item in cached.get("items", [])
                    if item["chunk_id"] in by_id
                ]
                retrieval_span.update_metadata(cache_hit=True, candidate_count=len(results))
                retrieval_span.set_outputs({"chunk_ids": [chunk.id for chunk, _ in results]})
                return RetrievalResult(
                    results=results,
                    trace_id=retrieval_span.trace_id,
                    cache_hit=True,
                    rewritten_queries=cached.get("rewritten_queries", [question]),
                    candidate_count=len(results),
                    reranked=bool(cached.get("reranked", use_rerank)),
                    document_version=document_version,
                )

            if use_rewrite:
                with self.traces.span(
                    "query_rewrite",
                    run_type="chain",
                    metadata=metadata,
                    inputs={"question": question},
                ) as rewrite_span:
                    rewritten_queries = self.query_rewriter.rewrite(question).queries
                    rewrite_span.set_outputs({"queries": rewritten_queries})
            else:
                rewritten_queries = [" ".join(question.split())]

            candidate_k = max(
                top_k,
                top_k * self.settings.retrieval_candidate_multiplier,
            )
            retriever = self._build_retriever(
                chunks,
                mode,
                knowledge_base_id,
                document_version,
            )
            with self.traces.span(
                "retrieval.rank",
                run_type="retriever",
                metadata={**metadata, "candidate_k": candidate_k},
                inputs={"queries": rewritten_queries},
            ) as rank_span:
                ranked_lists = await asyncio.gather(
                    *[retriever.search(query, candidate_k) for query in rewritten_queries]
                )
                fused = (
                    ranked_lists[0][:candidate_k]
                    if len(ranked_lists) == 1
                    else reciprocal_rank_fusion(*ranked_lists, top_k=candidate_k)
                )
                rank_span.update_metadata(candidate_count=len(fused))

            if use_rerank:
                with self.traces.span(
                    "retrieval.rerank",
                    run_type="chain",
                    metadata=metadata,
                    inputs={"candidate_count": len(fused)},
                ) as rerank_span:
                    results = await self.reranker.rank(question, fused, top_k)
                    rerank_span.set_outputs(
                        {"chunk_ids": [chunk.id for chunk, _ in results]}
                    )
            else:
                results = fused[:top_k]

            clean_results = [
                (chunk, score) for chunk, score in results if score > 0
            ]
            await self.cache.set(
                key,
                {
                    "items": [
                        {"chunk_id": chunk.id, "score": score}
                        for chunk, score in clean_results
                    ],
                    "rewritten_queries": rewritten_queries,
                    "reranked": use_rerank,
                },
            )
            retrieval_span.update_metadata(
                cache_hit=False,
                candidate_count=len(fused),
            )
            retrieval_span.set_outputs(
                {"chunk_ids": [chunk.id for chunk, _ in clean_results]}
            )
            return RetrievalResult(
                results=clean_results,
                trace_id=retrieval_span.trace_id,
                cache_hit=False,
                rewritten_queries=rewritten_queries,
                candidate_count=len(fused),
                reranked=use_rerank,
                document_version=document_version,
            )

    def _build_retriever(
        self,
        chunks: list[Chunk],
        mode: str,
        knowledge_base_id: str,
        document_version: str | None,
    ) -> Retriever:
        return create_retriever(
            self.settings,
            chunks,
            mode,
            knowledge_base_id,
            document_version,
        )
