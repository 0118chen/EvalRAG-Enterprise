"""Best-effort LangSmith dataset and experiment adapter.

The adapter keeps cloud-specific calls at the boundary so offline development
and production answering remain functional when LangSmith is disabled.
"""

import logging
from time import perf_counter
from typing import Any

from app.config import Settings
from app.core.evaluation import RetrievalExample, recall_at_k, reciprocal_rank
from app.core.observability import redact, tenant_hash

logger = logging.getLogger(__name__)


def remote_dataset_name(tenant_id: str | None, name: str) -> str:
    """Namespace a local dataset name with a per-tenant suffix.

    LangSmith datasets live in one account-wide namespace, so two tenants that
    both call their dataset "policy-eval" would otherwise create one remote
    dataset and write each other's examples into it. The suffix is a truncated
    tenant hash so the remote name does not expose the raw tenant identifier.
    """
    if not tenant_id:
        return name
    return f"{name}--{tenant_hash(tenant_id)[:12]}"



def dataset_example(
    question: str,
    knowledge_base_id: str,
    *,
    tenant_id: str | None = None,
    category: str = "general",
    expected_document_id: str | None = None,
    reference: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a redacted LangSmith example without uploading document contents."""
    metadata: dict[str, Any] = {"knowledge_base_id": knowledge_base_id, "category": category}
    if tenant_id:
        metadata["tenant_id_hash"] = tenant_hash(tenant_id)
    inputs = {"question": redact(question), "knowledge_base_id": knowledge_base_id}
    if expected_document_id:
        inputs["expected_document_id"] = expected_document_id
    example: dict[str, Any] = {"inputs": inputs, "metadata": metadata}
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

    def create_dataset(
        self,
        name: str,
        description: str = "",
        *,
        tenant_id: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        try:
            from langsmith import Client
            client = Client(api_key=self.settings.langsmith_api_key, api_url=self.settings.langsmith_endpoint)
            dataset = client.create_dataset(
                dataset_name=remote_dataset_name(tenant_id, name),
                description=description,
            )
            return str(dataset.id)
        except Exception:
            logger.warning("LangSmith dataset creation failed", exc_info=True)
            return None

    def ensure_dataset(
        self,
        name: str,
        description: str = "",
        *,
        tenant_id: str | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        remote_name = remote_dataset_name(tenant_id, name)
        try:
            from langsmith import Client

            client = Client(
                api_key=self.settings.langsmith_api_key,
                api_url=self.settings.langsmith_endpoint,
            )
            dataset = next(
                client.list_datasets(dataset_name=remote_name, limit=1),
                None,
            )
            if dataset:
                return str(dataset.id)
        except Exception:
            logger.warning("LangSmith dataset lookup failed", exc_info=True)
        return self.create_dataset(name, description, tenant_id=tenant_id)

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {
                "status": "disabled",
                "connected": False,
                "project": self.settings.langsmith_project,
                "endpoint": self.settings.langsmith_endpoint,
                "api_key_present": bool(self.settings.langsmith_api_key),
            }
        started = perf_counter()
        try:
            from langsmith import Client

            client = Client(
                api_key=self.settings.langsmith_api_key,
                api_url=self.settings.langsmith_endpoint,
            )
            project = next(
                client.list_projects(
                    name=self.settings.langsmith_project,
                    limit=1,
                ),
                None,
            )
            return {
                "status": "ok",
                "connected": True,
                "project": self.settings.langsmith_project,
                "project_found": project is not None,
                "endpoint": self.settings.langsmith_endpoint,
                "latency_ms": round((perf_counter() - started) * 1000, 2),
            }
        except Exception as exc:
            logger.warning("LangSmith health check failed", exc_info=True)
            return {
                "status": "error",
                "connected": False,
                "project": self.settings.langsmith_project,
                "endpoint": self.settings.langsmith_endpoint,
                "error": type(exc).__name__,
            }

    def create_examples(
        self,
        dataset_id: str,
        examples: list[dict[str, Any]],
    ) -> list[str]:
        if not self.enabled or not examples:
            return []
        try:
            from langsmith import Client

            client = Client(
                api_key=self.settings.langsmith_api_key,
                api_url=self.settings.langsmith_endpoint,
            )
            existing = list(client.list_examples(dataset_id=dataset_id))
            existing_keys = {
                (
                    (item.inputs or {}).get("question"),
                    (item.inputs or {}).get("expected_document_id"),
                )
                for item in existing
            }
            missing = [
                item
                for item in examples
                if (
                    item["inputs"].get("question"),
                    item["inputs"].get("expected_document_id"),
                )
                not in existing_keys
            ]
            if not missing:
                return []
            inputs = [item["inputs"] for item in missing]
            outputs = [item.get("outputs", {}) for item in missing]
            created = client.create_examples(
                dataset_id=dataset_id,
                inputs=inputs,
                outputs=outputs,
            )
            return [
                str(item.id if hasattr(item, "id") else item)
                for item in created
            ]
        except Exception:
            logger.warning("LangSmith example creation failed", exc_info=True)
            return []

    async def run_experiment(
        self,
        *,
        dataset_name: str,
        tenant_id: str | None = None,
        target,
        evaluators,
        experiment_prefix: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        try:
            from langsmith import Client, aevaluate

            client = Client(
                api_key=self.settings.langsmith_api_key,
                api_url=self.settings.langsmith_endpoint,
            )
            result = await aevaluate(
                target,
                data=remote_dataset_name(tenant_id, dataset_name),
                evaluators=evaluators,
                experiment_prefix=experiment_prefix,
                metadata=metadata,
                client=client,
            )
            experiment_name = getattr(result, "experiment_name", None)
            return {
                "experiment_name": experiment_name,
                "url": getattr(result, "url", None),
            }
        except Exception:
            logger.warning("LangSmith experiment failed", exc_info=True)
            return None

    @staticmethod
    def recall_evaluator(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, str | float]:
        example = RetrievalExample(
            inputs["question"],
            inputs["expected_document_id"],
            outputs.get("document_ids", []),
        )
        return {"key": "recall_at_5", "score": recall_at_k(example, 5)}

    @staticmethod
    def mrr_evaluator(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, str | float]:
        example = RetrievalExample(
            inputs["question"],
            inputs["expected_document_id"],
            outputs.get("document_ids", []),
        )
        return {"key": "mrr", "score": reciprocal_rank(example)}
