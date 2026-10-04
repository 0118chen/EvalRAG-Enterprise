from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RetrievalMode = Literal["dense", "sparse", "hybrid"]
# "all": every labelled hop/page has to be retrieved. "any": the labels are interchangeable
# alternatives, so retrieving one of them is a full success.
EvidenceMode = Literal["all", "any"]
VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"

# `document_version` selects which labelled slice of a knowledge base to search.
# None (or a blank string) means "no version filter", i.e. every version. It must not
# default to the literal "latest": "latest" is merely the *label* a document gets when
# the uploader did not name a version, so defaulting to it silently hides every document
# that was uploaded with an explicit version such as `v9`.
NO_VERSION_FILTER: None = None


def _blank_version_means_no_filter(value: object) -> object:
    if isinstance(value, str) and not value.strip():
        return None
    return value


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
    # None = search every version. A named version filters to that label only.
    document_version: str | None = Field(
        default=NO_VERSION_FILTER,
        max_length=64,
        pattern=VERSION_PATTERN,
    )
    rerank: bool | None = None
    query_rewrite: bool | None = None

    @field_validator("document_version", mode="before")
    @classmethod
    def _blank_version_is_all_versions(cls, value: object) -> object:
        return _blank_version_means_no_filter(value)


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
    # None = score against every version, matching the retrieval API's default.
    document_version: str | None = Field(
        default=NO_VERSION_FILTER,
        max_length=64,
        pattern=VERSION_PATTERN,
    )
    rerank: bool | None = None
    query_rewrite: bool | None = None
    baseline_evaluation_id: str | None = None
    experiment_name: str | None = Field(default=None, max_length=200)
    answer_evaluation: bool = False

    @field_validator("document_version", mode="before")
    @classmethod
    def _blank_version_is_all_versions(cls, value: object) -> object:
        return _blank_version_means_no_filter(value)


class EvidenceSpan(BaseModel):
    """One hop of the ground truth: where the answer lives and the sentence that shows it."""

    document_id: str = Field(min_length=1, max_length=128)
    page: int | None = Field(default=None, ge=1)
    quote: str | None = Field(default=None, max_length=4000)


class EvaluationExampleCreate(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    expected_answer: str | None = Field(default=None, max_length=4000)
    # Optional so an unanswerable example can have no document at all; the validator
    # below makes the two consistent.
    expected_document_id: str | None = Field(default=None, max_length=128)
    expected_page: int | None = Field(default=None, ge=1)
    evidence_quote: str | None = Field(default=None, max_length=4000)
    category: str = Field(default="general", max_length=100)
    # A question the corpus cannot answer: scored on whether the system declines, not on recall.
    should_refuse: bool = False
    # Multi-hop questions list every hop here, each with its own quote.
    expected_evidence: list[EvidenceSpan] = Field(default_factory=list)
    # "all" = every hop above is needed to answer; "any" = they are equivalent
    # alternatives (the same definition written into three regulations).
    evidence_mode: EvidenceMode = "all"

    @model_validator(mode="after")
    def _validate_labels(self) -> "EvaluationExampleCreate":
        if self.should_refuse:
            if self.expected_document_id or self.expected_evidence:
                raise ValueError(
                    "a should_refuse example must not carry expected evidence: "
                    "there is nothing in the corpus to point at"
                )
            return self
        if not self.expected_document_id and not self.expected_evidence:
            raise ValueError("expected_document_id or expected_evidence is required")
        first = self.expected_evidence[0].document_id if self.expected_evidence else None
        if first and self.expected_document_id and first != self.expected_document_id:
            raise ValueError(
                "expected_document_id must match the first expected_evidence hop, "
                f"got {self.expected_document_id!r} and {first!r}"
            )
        return self


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
