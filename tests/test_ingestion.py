from app.core.ingestion import chunk_pages, extract_text


def test_text_ingestion_and_overlap() -> None:
    pages = extract_text("policy.txt", "生效日期为2026年1月1日。".encode())
    chunks = chunk_pages("doc-1", pages, size=10, overlap=2)
    assert chunks and chunks[0].page == 1

