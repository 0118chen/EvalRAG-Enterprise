from functools import lru_cache

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
    elasticsearch_url: str = "http://localhost:9200"
    elasticsearch_api_key: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
