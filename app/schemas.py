from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

RetrievalMode = Literal["dense", "sparse", "hybrid"]
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class KnowledgeBase(KnowledgeBaseCreate):
    id: str
    tenant_id: str


class SearchRequest(BaseModel):
    tenant_id: str
    knowledge_base_id: str
    question: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    retrieval_mode: RetrievalMode = "hybrid"
    document_version: str | None = Field(
        default="latest",
        max_length=64,
        pattern=VERSION_PATTERN,
    )
    rerank: bool | None = None
    query_rewrite: bool | None = None


class Citation(BaseModel):
    document_id: str
    page: int
    score: float
    text: str
    version: str = Field(default="latest", pattern=VERSION_PATTERN)


class RetrievalDiagnostics(BaseModel):
    cache_hit: bool = False
    rewritten_queries: list[str] = Field(default_factory=list)
    candidate_count: int = 0
    reranked: bool = False
    document_version: str | None = None


class Answer(BaseModel):
    answer: str
    citations: list[Citation]
    trace_id: str | None = None
    retrieval: RetrievalDiagnostics | None = None


class FeedbackRequest(BaseModel):
    tenant_id: str = Field(default="", max_length=128)
    trace_id: str
    feedback: str = Field(pattern="^(correct|incorrect|citation_error|no_answer|other)$")
    comment: str = Field(default="", max_length=1000)


class EvaluationCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    knowledge_base_id: str
    dataset_name: str
    retrieval_mode: RetrievalMode = "hybrid"
    top_k: int = Field(default=5, ge=1, le=20)
    document_version: str | None = Field(
        default="latest",
        max_length=64,
        pattern=VERSION_PATTERN,
    )
    rerank: bool | None = None
    query_rewrite: bool | None = None
    baseline_evaluation_id: str | None = None
    experiment_name: str | None = Field(default=None, max_length=200)
    answer_evaluation: bool = False


class EvaluationExampleCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    expected_answer: str | None = Field(default=None, max_length=4000)
    expected_document_id: str = Field(min_length=1, max_length=128)
    expected_page: int | None = Field(default=None, ge=1)
    category: str = Field(default="general", max_length=100)


class EvaluationDatasetCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    knowledge_base_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    examples: list[EvaluationExampleCreate] = Field(min_length=1)


class EvaluationExample(EvaluationExampleCreate):
    id: str
    dataset_id: str


class EvaluationDataset(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str
    name: str
    description: str
    examples: list[EvaluationExample] = Field(default_factory=list)
    created_at: datetime | None = None


class EvaluationJob(BaseModel):
    id: str
    tenant_id: str
    knowledge_base_id: str | None = None
    dataset_id: str | None = None
    dataset_name: str
    retrieval_mode: RetrievalMode
    top_k: int
    document_version: str | None = None
    experiment_name: str | None = None
    baseline_evaluation_id: str | None = None
    parameters: dict = Field(default_factory=dict)
    status: str
    results: dict | None = None
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class Document(BaseModel):
    id: str
    filename: str
    knowledge_base_id: str
    chunks: int
    status: str = "ready"
    progress: int = Field(default=100, ge=0, le=100)
    error_message: str | None = None
    version: str = Field(default="latest", pattern=VERSION_PATTERN)
