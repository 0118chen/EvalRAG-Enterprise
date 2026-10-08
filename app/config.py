from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    langsmith_enabled: bool = False
    langsmith_tracing: bool = True
    langsmith_api_key: str | None = None
    langsmith_project: str = "evalrag-development"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    llm_provider: str = "mock"
    llm_model: str = "mock"
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str | None = None
    rag_version: str = "v0.1.0"
    prompt_version: str = "policy_qa_v1"
    milvus_uri: str = "http://localhost:19530"
    milvus_token: str | None = None
    milvus_collection: str = "evalrag_chunks"
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_api_key: str | None = None
    elasticsearch_index: str = "evalrag_chunks"
    dense_retrieval_backend: str = "local"
    sparse_retrieval_backend: str = "local"
    external_retrieval_fallback: bool = True
    embedding_provider: str = "hash"
    embedding_model: str = "text-embedding-3-small"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str | None = None
    embedding_dimensions: int = 32
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = "sqlite:///data/evalrag.db"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    auth_enabled: bool = False
    api_keys: dict[str, str] = Field(default_factory=dict)
    # The browser exchanges the long-lived API key for a short-lived session token at
    # POST /api/v1/auth/session and then sends `Authorization: Bearer <token>`. One hour is
    # short enough that a stolen token goes stale on its own, and long enough that a normal
    # working session is not interrupted; the session can also be revoked explicitly.
    session_ttl_seconds: int = 3600
    # Recording "last used" on every single request would double the writes a read-only
    # page makes, so it is refreshed at most this often (and only when the value is
    # actually older, which keeps a busy session from writing at all).
    session_touch_seconds: int = 60
    rate_limit_enabled: bool = False
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    rate_limit_backend: str = "memory"
    cache_enabled: bool = True
    cache_backend: str = "memory"
    cache_ttl_seconds: int = 120
    query_rewrite_enabled: bool = True
    rerank_enabled: bool = True
    rerank_backend: str = "lexical"
    # A citation naming a document or page we never retrieved is always refused. Demanding
    # that an answer cite *something* is a separate, stricter policy, and it is off by
    # default because mock and some local providers legitimately emit no citation at all.
    citation_required: bool = False
    # TypeSafe is the semantic second stage: one typed Noul judgment per (query, candidate)
    # pair, scored by a model that reads both. Off unless a key is configured, because it is
    # a paid third-party call and it sends the candidate text to them (see docs §11).
    typesafe_api_key: str | None = None
    typesafe_base_url: str = "https://api.typesafe.ai"
    typesafe_model: str = "jev-latest"
    typesafe_concurrency: int = 8
    retrieval_candidate_multiplier: int = 4
    # Retrieval experiments cover tens to hundreds of questions, each of which may hit a
    # paid embedding backend, a reranker and (with answer evaluation) two LLM calls. Bounded
    # concurrency turns an hour-long sequential run into minutes without hammering anyone's
    # rate limit; the per-example timeout keeps one stalled call from holding the whole run,
    # and every finished example is checkpointed so a crashed run resumes instead of
    # starting over. The bound is a per-example gate, not a global one: the Typesafe
    # reranker keeps its own (TYPESAFE_CONCURRENCY).
    evaluation_concurrency: int = 4
    evaluation_example_timeout_seconds: float = 120.0
    evaluation_inline_fallback: bool = False
    max_upload_mb: int = 50
    # Uploads used to be written to a local directory that the API and the worker both had to
    # mount. That pins the whole deployment to one machine: a second worker on another host
    # cannot see what the API wrote, and a container restart in the middle of an upload loses
    # the bytes while the row still says "queued". "local" stays the default so a laptop needs
    # no extra service, but "s3" (MinIO, AWS S3, anything S3-compatible) puts the object
    # store behind a contract both sides use, and lets the API hand the browser a short-lived
    # URL instead of streaming the file through itself.
    object_store: str = "local"
    object_store_local_dir: str = "data/uploads"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "evalrag"
    s3_region: str = "us-east-1"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    # MinIO and most self-hosted gateways only answer path-style requests
    # (``http://host/bucket/key``); AWS accepts both, so the safe default is path style.
    s3_path_style: bool = True
    # A presigned URL is opened by the *browser*, so the host inside it must be reachable from
    # the client - which is usually not the host the API talks to (``http://minio:9000`` inside
    # a compose network, ``https://objects.example.com`` outside it). Set this when the two
    # differ; leave it empty to sign with the same endpoint the API itself uses.
    s3_public_endpoint_url: str | None = None
    # A presigned URL is a bearer token for one object: anyone holding it can read that file
    # until it expires. Five minutes is long enough for a click and short enough that a URL
    # leaked through a proxy log or a shared screen is worthless.
    s3_presign_seconds: int = 300
    ocr_backend: str = "none"
    ocr_command: str = "tesseract"
    ocr_language: str = "chi_sim+eng"
    ocr_max_pages: int = 20
    health_checks_public: bool = False
    health_admin_token: str | None = None
    # Readiness probes a Redis ping, the Celery broker and the queue backlog; each of those
    # is a network round trip to a dependency that may be down, so the readiness endpoint
    # needs its own bound. Without one a hung broker turns `/health/ready` into a hang and
    # the orchestrator restarts a pod that was only waiting on a probe.
    readiness_timeout_seconds: float = 2.0
    # A backlog at or below this is normal (a consumer between polls); above it the queue is
    # not being drained, which is worth reporting as degraded even though the broker answers.
    queue_backlog_threshold: int = 0
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
