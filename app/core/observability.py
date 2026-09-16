import hashlib
import re
from contextlib import nullcontext
from typing import Any

from app.config import Settings


def redact(value: str) -> str:
    value = re.sub(r"(?<!\d)(1\d{10})(?!\d)", "[PHONE]", value)
    return re.sub(r"\b\d{17}[0-9Xx]\b", "[ID]", value)


def tenant_hash(tenant_id: str) -> str:
    return hashlib.sha256(tenant_id.encode()).hexdigest()[:16]


class TraceManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def context(self, name: str, *, metadata: dict[str, Any] | None = None):
        if not self.settings.langsmith_enabled or not self.settings.langsmith_api_key:
            return nullcontext()
        return nullcontext()

    def traceable(self, name: str, metadata: dict[str, Any]):
        """Return a LangSmith decorator when enabled, otherwise an identity decorator."""
        if not self.settings.langsmith_enabled or not self.settings.langsmith_api_key:
            return lambda function: function
        try:
            from langsmith import traceable
            return traceable(name=name, metadata=metadata, process_inputs=lambda inputs: {"question": redact(str(inputs))})
        except Exception:
            # Observability is best-effort: a missing SDK or telemetry outage must not break RAG.
            return lambda function: function
    def metadata(self, *, tenant_id: str, knowledge_base_id: str, retrieval_mode: str) -> dict[str, Any]:
        return {"tenant_id_hash": tenant_hash(tenant_id), "knowledge_base_id": knowledge_base_id,
                "retrieval_mode": retrieval_mode, "rag_version": self.settings.rag_version,
                "prompt_version": self.settings.prompt_version, "llm_model": self.settings.llm_model,
                "environment": self.settings.app_env}
