from dataclasses import dataclass
from io import BytesIO


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    page: int
    text: str


def extract_text(filename: str, payload: bytes) -> list[tuple[int, str]]:
    lower = filename.lower()
    if lower.endswith(".pdf"):
        import fitz
        document = fitz.open(stream=payload, filetype="pdf")
        return [(index + 1, page.get_text()) for index, page in enumerate(document)]
    if lower.endswith(".docx"):
        from docx import Document
        document = Document(BytesIO(payload))
        return [(1, "\n".join(paragraph.text for paragraph in document.paragraphs))]
    if lower.endswith((".txt", ".md")):
        return [(1, payload.decode("utf-8", errors="replace"))]
    raise ValueError("supported file types: pdf, docx, txt, md")


def chunk_pages(document_id: str, pages: list[tuple[int, str]], size: int = 800, overlap: int = 120) -> list[Chunk]:
    if size <= overlap:
        raise ValueError("chunk size must be greater than overlap")
    chunks: list[Chunk] = []
    for page, text in pages:
        clean = " ".join(text.split())
        start = 0
        while start < len(clean):
            end = min(start + size, len(clean))
            chunks.append(Chunk(f"{document_id}:{len(chunks)}", document_id, page, clean[start:end]))
            if end == len(clean):
                break
            start = end - overlap
    return chunks

