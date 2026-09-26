import asyncio

from fastapi.testclient import TestClient

from app.api.routes import evaluations as evaluation_routes
from app.config import Settings
from app.container import build_container
from app.core.evaluation_runner import EvaluationRunner
from app.core.ingestion import Chunk
from app.main import create_app
from app.schemas import Document, KnowledgeBase


def test_evaluation_api_runs_and_compares(tmp_path, monkeypatch) -> None:
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
        evaluation_inline_fallback=True,
    )
    app = create_app(settings)
    container = build_container(settings)
    app.state.container = container
    container.store.save_knowledge_base(
        KnowledgeBase(id="kb", tenant_id="tenant", name="Policy", description="")
    )
    container.store.save_document(
        Document(
            id="doc",
            filename="policy.txt",
            knowledge_base_id="kb",
            chunks=0,
            status="ready",
            version="v1",
        ),
        [],
    )
    container.store.replace_chunks(
        "doc",
        "kb",
        [Chunk("chunk", "doc", 1, "policy effective date", "v1")],
    )

    def run_inline(evaluation_id: str) -> None:
        runner = EvaluationRunner(
            settings,
            container.store,
            container.retrieval,
            container.traces,
        )
        asyncio.run(runner.run(evaluation_id))

    monkeypatch.setattr(evaluation_routes, "process_evaluation", run_inline)
    client = TestClient(app)
    dataset_response = client.post(
        "/api/v1/evaluation-datasets",
        json={
            "tenant_id": "tenant",
            "knowledge_base_id": "kb",
            "name": "golden",
            "description": "",
            "examples": [
                {
                    "question": "policy effective date",
                    "expected_document_id": "doc",
                    "expected_page": 1,
                }
            ],
        },
    )
    assert dataset_response.status_code == 201
    evaluation_response = client.post(
        "/api/v1/evaluations",
        json={
            "tenant_id": "tenant",
            "knowledge_base_id": "kb",
            "dataset_name": "golden",
            "retrieval_mode": "hybrid",
            "top_k": 3,
            "document_version": "v1",
        },
    )
    assert evaluation_response.status_code == 202
    evaluation_id = evaluation_response.json()["id"]
    completed = client.get(f"/api/v1/evaluations/{evaluation_id}").json()
    assert completed["status"] == "completed"
    assert completed["results"]["metrics"]["recall_at_3"] == 1.0
    compare = client.get(
        f"/api/v1/evaluations/{evaluation_id}/compare"
    ).json()
    assert compare["current"]["recall_at_3"] == 1.0
