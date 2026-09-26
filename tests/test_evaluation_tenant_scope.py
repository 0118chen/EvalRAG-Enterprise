"""A baseline evaluation must belong to the same tenant that references it."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.api.routes.evaluations as evaluations_route
from app.config import Settings
from app.core.cache import MemoryTTLCache
from app.core.evaluation_runner import EvaluationRunner
from app.core.observability import TraceManager
from app.core.query_rewrite import IdentityQueryRewriter
from app.core.reranking import LexicalReranker
from app.core.retrieval_service import RetrievalService
from app.core.store import SQLiteStore
from app.main import create_app
from app.schemas import EvaluationDataset, KnowledgeBase


def _isolated_client(tmp_path, monkeypatch) -> tuple[object, TestClient, list[str]]:
    monkeypatch.chdir(tmp_path)
    app = create_app(
        Settings(
            database_url=f"sqlite:///{(tmp_path / 'eval-tenant.db').as_posix()}",
            llm_provider="mock",
            langsmith_enabled=False,
            langsmith_api_key=None,
        )
    )
    queued: list[str] = []
    monkeypatch.setattr(
        evaluations_route,
        "process_evaluation",
        SimpleNamespace(delay=lambda evaluation_id: queued.append(evaluation_id)),
    )
    return app, TestClient(app), queued


def _seed(client: TestClient, tenant: str, dataset_name: str) -> str:
    """Create a knowledge base and a dataset, returning the knowledge base id."""
    knowledge_base = client.post(
        "/api/v1/knowledge-bases",
        params={"tenant_id": tenant},
        json={"name": f"{tenant}-kb"},
    ).json()
    created = client.post(
        "/api/v1/evaluation-datasets",
        json={
            "tenant_id": tenant,
            "knowledge_base_id": knowledge_base["id"],
            "name": dataset_name,
            "examples": [
                {
                    "question": "农户贷款应遵循哪些原则？",
                    "expected_document_id": "doc-1",
                }
            ],
        },
    )
    assert created.status_code == 201
    return knowledge_base["id"]


def _evaluation_payload(knowledge_base_id: str, dataset_name: str, **extra) -> dict:
    return {
        "tenant_id": extra.pop("tenant_id"),
        "knowledge_base_id": knowledge_base_id,
        "dataset_name": dataset_name,
        **extra,
    }


def test_create_evaluation_rejects_a_baseline_from_another_tenant(
    tmp_path, monkeypatch
) -> None:
    _app, client, queued = _isolated_client(tmp_path, monkeypatch)
    knowledge_base_a = _seed(client, "tenant-a", "golden-a")
    knowledge_base_b = _seed(client, "tenant-b", "golden-b")

    foreign = client.post(
        "/api/v1/evaluations",
        json=_evaluation_payload(
            knowledge_base_b, "golden-b", tenant_id="tenant-b"
        ),
    )
    assert foreign.status_code == 202
    foreign_id = foreign.json()["id"]
    assert queued == [foreign_id]

    response = client.post(
        "/api/v1/evaluations",
        json=_evaluation_payload(
            knowledge_base_a,
            "golden-a",
            tenant_id="tenant-a",
            baseline_evaluation_id=foreign_id,
        ),
    )

    assert response.status_code == 404
    assert client.get("/api/v1/evaluations", params={"tenant_id": "tenant-a"}).json() == []


def test_runner_ignores_a_baseline_that_belongs_to_another_tenant(tmp_path) -> None:
    settings = Settings()
    store = SQLiteStore(str(tmp_path / "runner-tenant.db"))
    store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant-a", name="Policy", description="")
    )
    store.save_evaluation_dataset(
        EvaluationDataset(
            id="dataset",
            tenant_id="tenant-a",
            knowledge_base_id="kb",
            name="golden",
            description="",
            examples=[],
        )
    )
    runner = EvaluationRunner(
        settings,
        store,
        RetrievalService(
            settings,
            TraceManager(settings),
            MemoryTTLCache(),
            IdentityQueryRewriter(),
            LexicalReranker(),
        ),
        TraceManager(settings),
    )
    store.create_evaluation(
        "foreign",
        "golden",
        "hybrid",
        5,
        tenant_id="tenant-b",
        knowledge_base_id="kb",
        dataset_id="dataset",
        parameters={},
    )
    store.update_evaluation("foreign", "completed", {"metrics": {"recall_at_5": 0.5}})

    assert (
        runner._baseline_diff(
            {"baseline_evaluation_id": "foreign", "tenant_id": "tenant-a"},
            {"recall_at_5": 0.9},
        )
        is None
    )

    store.create_evaluation(
        "own",
        "golden",
        "hybrid",
        5,
        tenant_id="tenant-a",
        knowledge_base_id="kb",
        dataset_id="dataset",
        parameters={},
    )
    store.update_evaluation("own", "completed", {"metrics": {"recall_at_5": 0.5}})
    diff = runner._baseline_diff(
        {"baseline_evaluation_id": "own", "tenant_id": "tenant-a"},
        {"recall_at_5": 0.9},
    )

    assert diff == {"recall_at_5": pytest.approx(0.4)}
