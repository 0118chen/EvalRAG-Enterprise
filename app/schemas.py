from pydantic import BaseModel, Field


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
    retrieval_mode: str = Field(default="hybrid", pattern="^(dense|sparse|hybrid)$")


class Citation(BaseModel):
    document_id: str
    page: int
    score: float
    text: str


class Answer(BaseModel):
    answer: str
    citations: list[Citation]
    trace_id: str | None = None


class FeedbackRequest(BaseModel):
    trace_id: str
    feedback: str = Field(pattern="^(correct|incorrect|citation_error|no_answer|other)$")
    comment: str = Field(default="", max_length=1000)


class EvaluationCreate(BaseModel):
    dataset_name: str
    retrieval_mode: str = Field(default="hybrid", pattern="^(dense|sparse|hybrid)$")
    top_k: int = Field(default=5, ge=1, le=20)


class Document(BaseModel):
    id: str
    filename: str
    knowledge_base_id: str
    chunks: int
