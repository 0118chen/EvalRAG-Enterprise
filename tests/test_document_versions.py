"""`document_version` selects a labelled slice; it must never hide a document by default.

The bug this pins: the field defaulted to the literal ``"latest"`` while the store
applied it as an equality filter, so a document uploaded with ``version="v9"`` was
invisible to a request that did not name a version - ``citations`` came back empty and
the answer was the refusal text, with nothing saying why.
"""

from fastapi.testclient import TestClient

from app.core.ingestion import Chunk
from app.schemas import Document, EvaluationCreate, KnowledgeBase, SearchRequest

TENANT = "tenant-versions"
KB = "kb-versions"
QUESTION = "本办法从哪一天开始施行？"
TEXT = "第七十四条 本办法自2021年6月1日起施行。"


def _seed(store) -> None:
    store.save_knowledge_base(
        KnowledgeBase(id=KB, tenant_id=TENANT, name="Versions", description="")
    )
    store.save_document(
        Document(
            id="doc-v9",
            filename="v9.txt",
            knowledge_base_id=KB,
            chunks=0,
            status="ready",
            version="v9",
        ),
        [],
    )
    store.replace_chunks("doc-v9", KB, [Chunk("doc-v9:0", "doc-v9", 1, TEXT, "v9")])


def _search(client: TestClient, **extra: object) -> dict:
    payload: dict[str, object] = {
        "tenant_id": TENANT,
        "knowledge_base_id": KB,
        "question": QUESTION,
        "retrieval_mode": "sparse",
        "top_k": 5,
    }
    payload.update(extra)
    response = client.post("/api/v1/retrieval/search", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_a_document_uploaded_under_a_named_version_is_found_by_default(
    client: TestClient,
) -> None:
    _seed(client.app.state.container.store)

    body = _search(client)

    assert [item["document_id"] for item in body["citations"]] == ["doc-v9"]
    assert body["retrieval"]["document_version"] is None


def test_naming_the_version_still_filters_to_that_label(client: TestClient) -> None:
    _seed(client.app.state.container.store)

    # "latest" is a label a document gets when the uploader did not name one, not a
    # synonym for "newest": here it must select nothing.
    assert _search(client, document_version="latest")["citations"] == []
    assert _search(client, document_version="v9")["citations"] != []


def test_a_blank_version_is_treated_as_no_filter(client: TestClient) -> None:
    _seed(client.app.state.container.store)

    for blank in ("", "   "):
        body = _search(client, document_version=blank)
        assert body["citations"], blank
        assert body["retrieval"]["document_version"] is None


def test_the_schema_defaults_to_no_filter_and_still_validates_a_named_version() -> None:
    base = {"tenant_id": "t", "knowledge_base_id": "k", "question": "q"}

    assert SearchRequest(**base).document_version is None
    assert SearchRequest(**base, document_version="").document_version is None
    assert SearchRequest(**base, document_version="   ").document_version is None
    assert SearchRequest(**base, document_version="v9").document_version == "v9"
    assert EvaluationCreate(
        tenant_id="t", knowledge_base_id="k", dataset_name="d"
    ).document_version is None
