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
    rate_limit_enabled: bool = False
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    rate_limit_backend: str = "memory"
    cache_enabled: bool = True
    cache_backend: str = "memory"
    cache_ttl_seconds: int = 120
    query_rewrite_enabled: bool = True
    rerank_enabled: bool = True
    retrieval_candidate_multiplier: int = 4
    evaluation_inline_fallback: bool = False
    max_upload_mb: int = 50
    ocr_backend: str = "none"
    ocr_command: str = "tesseract"
    ocr_language: str = "chi_sim+eng"
    ocr_max_pages: int = 20
    health_checks_public: bool = False
    health_admin_token: str | None = None
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
