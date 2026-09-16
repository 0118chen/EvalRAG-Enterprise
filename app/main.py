from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.core.observability import TraceManager, redact
from app.core.ingestion import Chunk, chunk_pages, extract_text
from app.core.retrieval import retrieve
from app.core.store import SQLiteStore
from app.core.llm import MockLLM, OpenAICompatibleLLM
from app.core.rag import answer_question, stream_text
from app.core.pipeline import default_pipeline
from app.core.backends import HybridRetriever, LocalRetriever
from app.schemas import Answer, Document, EvaluationCreate, FeedbackRequest, KnowledgeBase, KnowledgeBaseCreate, SearchRequest

app = FastAPI(title="EvalRAG Enterprise", version="0.1.0")
settings = get_settings()
traces = TraceManager(settings)
store = SQLiteStore()
pipeline = default_pipeline()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


@app.post("/api/v1/knowledge-bases", response_model=KnowledgeBase, status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreate, tenant_id: str) -> KnowledgeBase:
    kb = KnowledgeBase(id=str(uuid4()), tenant_id=tenant_id, **payload.model_dump())
    store.save_knowledge_base(kb)
    return kb


@app.post("/api/v1/documents", response_model=Document, status_code=201)
async def upload_document(tenant_id: str = Form(...), knowledge_base_id: str = Form(...), file: UploadFile = File(...)) -> Document:
    kb = store.get_knowledge_base(knowledge_base_id, tenant_id)
    if not kb:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    document_id = str(uuid4())
    try:
        pages = extract_text(file.filename or "document.txt", await file.read())
        chunks = chunk_pages(document_id, pages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    document = Document(id=document_id, filename=file.filename or "document.txt", knowledge_base_id=kb.id, chunks=len(chunks), status="pending", progress=10)
    try:
        store.save_document(document, [])
        store.update_document_progress(document_id, 50)
        await pipeline.index(chunks)
        store.update_document_status(document_id, "ready")
        store.update_document_progress(document_id, 100)
        document = document.model_copy(update={"status": "ready", "progress": 100})
        with store._lock, store._connect() as connection:
            connection.executemany("INSERT INTO chunks VALUES (?, ?, ?, ?, ?)", [(c.id, c.document_id, c.page, c.text, kb.id) for c in chunks])
    except Exception as exc:
        try:
            store.update_document_status(document_id, "failed")
            store.update_document_progress(document_id, 0, str(exc))
        except Exception:
            store.save_document(document.model_copy(update={"status": "failed", "chunks": 0}), [])
        raise HTTPException(status_code=503, detail="document indexing failed") from exc
    return document


@app.delete("/api/v1/documents/{document_id}", status_code=204)
def delete_document(document_id: str, tenant_id: str) -> None:
    if not store.delete_document(document_id, tenant_id):
        raise HTTPException(status_code=404, detail="document not found")


@app.get("/api/v1/documents/{document_id}", response_model=Document)
def get_document(document_id: str, tenant_id: str) -> Document:
    document = store.get_document(document_id, tenant_id)
    if not document:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@app.post("/api/v1/retrieval/search", response_model=Answer)
def search(payload: SearchRequest) -> Answer:
    kb = store.get_knowledge_base(payload.knowledge_base_id, payload.tenant_id)
    if not kb:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    metadata = traces.metadata(tenant_id=payload.tenant_id, knowledge_base_id=kb.id, retrieval_mode=payload.retrieval_mode)
    results = retrieve(payload.question, store.get_chunks(kb.id), payload.top_k, payload.retrieval_mode)
    citations = [{"document_id": chunk.document_id, "page": chunk.page, "score": score, "text": chunk.text} for chunk, score in results if score > 0]
    if not citations:
        return Answer(answer="未找到足够依据，无法可靠回答该问题。", citations=[], trace_id=str(uuid4()))
    trace_id = str(uuid4())
    metadata["citation_count"] = len(citations)
    metadata["top_k"] = payload.top_k
    # The decorator is a no-op when LangSmith is disabled, preserving local/offline operation.
    @traces.traceable("rag_request", metadata)
    def assemble() -> Answer:
        return Answer(answer=f"基于知识库“{kb.name}”的相关资料，问题为：{redact(payload.question)}。请参考以下来源。",
                      citations=citations, trace_id=trace_id)
    return assemble()


@app.post("/api/v1/feedback")
def feedback(payload: FeedbackRequest) -> dict[str, str]:
    return {"status": "accepted", "trace_id": payload.trace_id}


@app.post("/api/v1/evaluations")
def create_evaluation(payload: EvaluationCreate) -> dict[str, str | int]:
    return {"status": "queued", "dataset_name": payload.dataset_name, "retrieval_mode": payload.retrieval_mode, "top_k": payload.top_k}


def get_llm():
    if settings.llm_provider == "mock":
        return MockLLM()
    if not settings.llm_api_key:
        raise HTTPException(status_code=503, detail="LLM provider is not configured")
    return OpenAICompatibleLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)


@app.post("/api/v1/chat/stream")
async def chat_stream(payload: SearchRequest) -> StreamingResponse:
    kb = store.get_knowledge_base(payload.knowledge_base_id, payload.tenant_id)
    if not kb:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    chunks = store.get_chunks(kb.id)
    retriever = HybridRetriever(LocalRetriever(chunks, "dense"), LocalRetriever(chunks, "sparse")) if payload.retrieval_mode == "hybrid" else LocalRetriever(chunks, payload.retrieval_mode)
    answer, evidence = await answer_question(get_llm(), payload.question, chunks, payload.top_k, payload.retrieval_mode, retriever)

    async def events():
        for token in stream_text(answer):
            yield f"data: {token}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
