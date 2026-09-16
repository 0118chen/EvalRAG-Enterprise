from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from app.config import get_settings
from app.core.observability import TraceManager, redact
from app.core.ingestion import Chunk, chunk_pages, extract_text
from app.core.retrieval import retrieve
from app.schemas import Answer, Document, EvaluationCreate, FeedbackRequest, KnowledgeBase, KnowledgeBaseCreate, SearchRequest

app = FastAPI(title="EvalRAG Enterprise", version="0.1.0")
settings = get_settings()
traces = TraceManager(settings)
knowledge_bases: dict[str, KnowledgeBase] = {}
documents: dict[str, Document] = {}
chunks_by_kb: dict[str, list[Chunk]] = {}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


@app.post("/api/v1/knowledge-bases", response_model=KnowledgeBase, status_code=201)
def create_knowledge_base(payload: KnowledgeBaseCreate, tenant_id: str) -> KnowledgeBase:
    kb = KnowledgeBase(id=str(uuid4()), tenant_id=tenant_id, **payload.model_dump())
    knowledge_bases[kb.id] = kb
    return kb


@app.post("/api/v1/documents", response_model=Document, status_code=201)
async def upload_document(tenant_id: str = Form(...), knowledge_base_id: str = Form(...), file: UploadFile = File(...)) -> Document:
    kb = knowledge_bases.get(knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    try:
        pages = extract_text(file.filename or "document.txt", await file.read())
        document_id = str(uuid4())
        chunks = chunk_pages(document_id, pages)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    document = Document(id=document_id, filename=file.filename or "document.txt", knowledge_base_id=kb.id, chunks=len(chunks))
    documents[document_id] = document
    chunks_by_kb.setdefault(kb.id, []).extend(chunks)
    return document


@app.post("/api/v1/retrieval/search", response_model=Answer)
def search(payload: SearchRequest) -> Answer:
    kb = knowledge_bases.get(payload.knowledge_base_id)
    if not kb or kb.tenant_id != payload.tenant_id:
        raise HTTPException(status_code=404, detail="knowledge base not found")
    _ = traces.metadata(tenant_id=payload.tenant_id, knowledge_base_id=kb.id, retrieval_mode=payload.retrieval_mode)
    results = retrieve(payload.question, chunks_by_kb.get(kb.id, []), payload.top_k, payload.retrieval_mode)
    citations = [{"document_id": chunk.document_id, "page": chunk.page, "score": score, "text": chunk.text} for chunk, score in results if score > 0]
    if not citations:
        return Answer(answer="未找到足够依据，无法可靠回答该问题。", citations=[], trace_id=str(uuid4()))
    return Answer(answer=f"基于知识库“{kb.name}”的相关资料，问题为：{redact(payload.question)}。请参考以下来源。",
                  citations=citations, trace_id=str(uuid4()))


@app.post("/api/v1/feedback")
def feedback(payload: FeedbackRequest) -> dict[str, str]:
    return {"status": "accepted", "trace_id": payload.trace_id}


@app.post("/api/v1/evaluations")
def create_evaluation(payload: EvaluationCreate) -> dict[str, str | int]:
    return {"status": "queued", "dataset_name": payload.dataset_name, "retrieval_mode": payload.retrieval_mode, "top_k": payload.top_k}
