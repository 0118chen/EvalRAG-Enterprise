"""Best-effort LangSmith dataset and experiment adapter.

The adapter keeps cloud-specific calls at the boundary so offline development
and production answering remain functional when LangSmith is disabled.
"""

from typing import Any

from app.config import Settings
from app.core.observability import redact, tenant_hash


def dataset_example(question: str, knowledge_base_id: str, *, tenant_id: str | None = None,
                    category: str = "general", reference: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a redacted LangSmith example without uploading document contents."""
    metadata: dict[str, Any] = {"knowledge_base_id": knowledge_base_id, "category": category}
    if tenant_id:
        metadata["tenant_id_hash"] = tenant_hash(tenant_id)
    example: dict[str, Any] = {"inputs": {"question": redact(question), "knowledge_base_id": knowledge_base_id}, "metadata": metadata}
    if reference:
        example["outputs"] = reference
    return example


class LangSmithEvaluationAdapter:
    """Create datasets through LangSmith when configured, otherwise return None."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.langsmith_enabled and self.settings.langsmith_api_key)

    def create_dataset(self, name: str, description: str = "") -> str | None:
        if not self.enabled:
            return None
        try:
            from langsmith import Client
            client = Client(api_key=self.settings.langsmith_api_key, api_url=self.settings.langsmith_endpoint)
            dataset = client.create_dataset(dataset_name=name, description=description)
            return str(dataset.id)
        except Exception:
            return None
