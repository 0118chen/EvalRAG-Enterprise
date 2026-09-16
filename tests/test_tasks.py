from app.tasks import process_document


def test_document_task_contract() -> None:
    result = process_document.run("doc-1") if hasattr(process_document, "run") else process_document("doc-1")
    assert result == {"document_id": "doc-1", "status": "accepted"}
