import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import authenticate, get_container, get_llm, resolve_tenant_id
from app.core.concurrency import run_blocking
from app.core.rag import stream_with_evidence
from app.schemas import Answer, Citation, RetrievalDiagnostics, SearchRequest

router = APIRouter(prefix="/api/v1", tags=["retrieval"])


async def _retrieve(
    payload: SearchRequest,
    request: Request,
):
    container = get_container(request)
    tenant_id = resolve_tenant_id(request, payload.tenant_id)
    knowledge_base = await run_blocking(
        container.store.get_knowledge_base,
        payload.knowledge_base_id,
        tenant_id,
    )
    if not knowledge_base:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    chunks = await run_blocking(
        container.store.get_chunks,
        knowledge_base.id,
        payload.document_version,
    )
    retrieval = await container.retrieval.search(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base.id,
        question=payload.question,
        chunks=chunks,
        top_k=payload.top_k,
        mode=payload.retrieval_mode,
        document_version=payload.document_version,
        rerank=payload.rerank,
        query_rewrite=payload.query_rewrite,
    )
    return container, knowledge_base, retrieval


def _citations(retrieval) -> list[Citation]:
    return [
        Citation(
            document_id=chunk.document_id,
            page=chunk.page,
            score=score,
            text=chunk.text,
            version=chunk.version,
        )
        for chunk, score in retrieval.results
    ]


@router.post("/retrieval/search", response_model=Answer)
async def search(
    payload: SearchRequest,
    request: Request,
    _auth: str | None = Depends(authenticate),
) -> Answer:
    container = get_container(request)
    with container.traces.span(
        "rag.request",
        run_type="chain",
        metadata=container.traces.metadata(
            tenant_id=payload.tenant_id,
            knowledge_base_id=payload.knowledge_base_id,
            retrieval_mode=payload.retrieval_mode,
            request_id=request.state.request_id,
        ),
        inputs={"question": payload.question, "top_k": payload.top_k},
    ) as rag_span:
        _container, _knowledge_base, retrieval = await _retrieve(payload, request)
        citations = _citations(retrieval)
        diagnostics = RetrievalDiagnostics(
            cache_hit=retrieval.cache_hit,
            rewritten_queries=retrieval.rewritten_queries,
            candidate_count=retrieval.candidate_count,
            reranked=retrieval.reranked,
            document_version=retrieval.document_version,
        )
        if not citations:
            answer_text = "未找到足够依据，无法可靠回答该问题。"
        else:
            answer_text = (
                f"已检索到 {len(citations)} 条相关证据，"
                f"trace 标识为 {rag_span.trace_id}。请查看引用来源。"
            )
        rag_span.update_metadata(
            trace_id=retrieval.trace_id,
            cache_hit=retrieval.cache_hit,
            candidate_count=retrieval.candidate_count,
        )
        rag_span.set_outputs({"citation_count": len(citations)})
        return Answer(
            answer=answer_text,
            citations=citations,
            trace_id=rag_span.trace_id,
            retrieval=diagnostics,
        )


@router.post("/chat/stream")
async def chat_stream(
    payload: SearchRequest,
    request: Request,
    _auth: str | None = Depends(authenticate),
) -> StreamingResponse:
    container = get_container(request)
    request_trace_id = str(uuid4())

    async def events():
        with container.traces.span(
            "rag.request",
            run_type="chain",
            metadata=container.traces.metadata(
                tenant_id=payload.tenant_id,
                knowledge_base_id=payload.knowledge_base_id,
                retrieval_mode=payload.retrieval_mode,
                request_id=request.state.request_id,
            ),
            inputs={"question": payload.question, "top_k": payload.top_k},
            trace_id=request_trace_id,
        ) as rag_span:
            try:
                _container, _knowledge_base, retrieval = await _retrieve(payload, request)
                citations = _citations(retrieval)
                rag_span.update_metadata(
                    trace_id=rag_span.trace_id,
                    cache_hit=retrieval.cache_hit,
                    candidate_count=retrieval.candidate_count,
                )
                yield "event: retrieval\n"
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "trace_id": retrieval.trace_id,
                            "cache_hit": retrieval.cache_hit,
                            "rewritten_queries": retrieval.rewritten_queries,
                            "candidate_count": retrieval.candidate_count,
                            "reranked": retrieval.reranked,
                            "document_version": retrieval.document_version,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield "event: citations\n"
                yield (
                    "data: "
                    + json.dumps(
                        [item.model_dump() for item in citations],
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )

                answer_parts: list[str] = []
                evidence_count = len(retrieval.results)
                generation_failed = False
                with container.traces.span(
                    "generation.answer",
                    run_type="llm",
                    metadata=container.traces.metadata(
                        tenant_id=payload.tenant_id,
                        knowledge_base_id=payload.knowledge_base_id,
                        retrieval_mode=payload.retrieval_mode,
                        request_id=request.state.request_id,
                        trace_id=rag_span.trace_id,
                    ),
                    inputs={
                        "question": payload.question,
                        "citation_count": len(citations),
                    },
                ) as generation_span:
                    generation_stream = stream_with_evidence(
                        get_llm(request),
                        payload.question,
                        retrieval.results,
                    )
                    try:
                        async for token in generation_stream:
                            answer_parts.append(token)
                            yield "event: token\n"
                            yield f"data: {json.dumps(token, ensure_ascii=False)}\n\n"
                    except (GeneratorExit, asyncio.CancelledError):
                        generation_span.set_error("stream cancelled")
                        rag_span.set_error("stream cancelled")
                        raise
                    except Exception:  # noqa: BLE001 - provider adapters may raise any error
                        generation_span.set_error("generation failed")
                        rag_span.set_error("generation failed")
                        generation_failed = True
                    finally:
                        await generation_stream.aclose()
                        generation_span.set_outputs(
                            {
                                "answer_length": len("".join(answer_parts)),
                                "evidence_count": evidence_count,
                            }
                        )

                rag_span.set_outputs(
                    {
                        "citation_count": len(citations),
                        "answer_length": len("".join(answer_parts)),
                    }
                )
                if generation_failed:
                    yield "event: error\n"
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "code": "generation_failed",
                                "message": "模型生成失败，请稍后重试。",
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    yield "data: [DONE]\n\n"
                    return

                yield "event: trace\n"
                yield (
                    "data: "
                    + json.dumps(
                        {"trace_id": rag_span.trace_id, "trace_url": None},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"
            except (GeneratorExit, asyncio.CancelledError):
                rag_span.set_error("stream cancelled")
                raise

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Trace-ID": request_trace_id},
    )
