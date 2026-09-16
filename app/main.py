from uuid import uuid4
from pathlib import Path
import re

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.config import get_settings
from app.core.observability import TraceManager, redact
from app.core.ingestion import Chunk
from app.core.retrieval import retrieve
from app.core.store import SQLiteStore
from app.core.llm import MockLLM, OpenAICompatibleLLM
from app.core.rag import answer_question, stream_text
from app.core.pipeline import default_pipeline
from app.core.backends import HybridRetriever, LocalRetriever
from app.schemas import Answer, Document, EvaluationCreate, FeedbackRequest, KnowledgeBase, KnowledgeBaseCreate, SearchRequest
from app.tasks import process_document

app = FastAPI(title="EvalRAG Enterprise", version="0.1.0")


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Attach a stable request identifier to every response for trace correlation."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


app.add_middleware(RequestIDMiddleware)
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


@app.get("/api/v1/knowledge-bases", response_model=list[KnowledgeBase])
def list_knowledge_bases(tenant_id: str) -> list[KnowledgeBase]:
    """List only knowledge bases owned by the requested tenant."""
    return store.list_knowledge_bases(tenant_id)


@app.post("/api/v1/documents", response_model=Document, status_code=201)
async def upload_document(tenant_id: str = Form(...), knowledge_base_id: str = Form(...), file: UploadFile = File(...)) -> Document:
    kb = store.get_knowledge_base(knowledge_base_id, tenant_id)
    if not kb:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    document_id = str(uuid4())
    filename = file.filename or "document.txt"
    if not re.search(r"\.(pdf|docx|txt|md)$", filename.lower()):
        raise HTTPException(status_code=400, detail="supported file types: pdf, docx, txt, md")
    upload_dir = Path("data/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w.\-]", "_", filename)
    (upload_dir / f"{document_id}_{safe_name}").write_bytes(await file.read())
    document = Document(id=document_id, filename=filename, knowledge_base_id=kb.id, chunks=0, status="pending", progress=0)
    try:
        store.save_document(document, [])
        if celery_app_available := hasattr(process_document, "delay"):
            process_document.delay(document_id)
        else:
            raise RuntimeError("Celery worker is not available")
    except Exception as exc:
        store.update_document_status(document_id, "failed")
        store.update_document_progress(document_id, 0, str(exc))
        raise HTTPException(status_code=503, detail="document task could not be queued") from exc
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


@app.get("/api/v1/knowledge-bases/{knowledge_base_id}/documents", response_model=list[Document])
def list_documents(knowledge_base_id: str, tenant_id: str) -> list[Document]:
    if not store.get_knowledge_base(knowledge_base_id, tenant_id):
        raise HTTPException(status_code=404, detail="knowledge base not found")
    return store.list_documents(knowledge_base_id, tenant_id)


@app.post("/api/v1/retrieval/search", response_model=Answer)
def search(payload: SearchRequest, request: Request) -> Answer:
    kb = store.get_knowledge_base(payload.knowledge_base_id, payload.tenant_id)
    if not kb:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    metadata = traces.metadata(tenant_id=payload.tenant_id, knowledge_base_id=kb.id, retrieval_mode=payload.retrieval_mode)
    metadata["request_id"] = request.state.request_id
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
    feedback_id = str(uuid4())
    store.save_feedback(feedback_id, payload.trace_id, payload.feedback, payload.comment, settings.rag_version, settings.prompt_version)
    return {"status": "accepted", "id": feedback_id, "trace_id": payload.trace_id}


@app.post("/api/v1/evaluations")
def create_evaluation(payload: EvaluationCreate) -> dict[str, str | int]:
    evaluation_id = str(uuid4())
    store.create_evaluation(evaluation_id, payload.dataset_name, payload.retrieval_mode, payload.top_k)
    return {"id": evaluation_id, "status": "queued", "dataset_name": payload.dataset_name, "retrieval_mode": payload.retrieval_mode, "top_k": payload.top_k}


@app.get("/api/v1/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: str) -> dict[str, str | int]:
    evaluation = store.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return evaluation


@app.get("/api/v1/evaluations/{evaluation_id}/results")
def get_evaluation_results(evaluation_id: str) -> dict:
    evaluation = store.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return {"id": evaluation_id, "status": evaluation["status"], "results": evaluation["results"]}


@app.get("/api/v1/evaluations/{evaluation_id}/compare")
def compare_evaluation(evaluation_id: str) -> dict:
    evaluation = store.get_evaluation(evaluation_id)
    if not evaluation:
        raise HTTPException(status_code=404, detail="evaluation not found")
    return {"evaluation_id": evaluation_id, "dataset_name": evaluation["dataset_name"], "current": evaluation["results"], "baseline": None, "message": "baseline comparison will be available after a second completed experiment"}


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
