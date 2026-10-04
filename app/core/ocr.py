"""OCR for uploads the pipeline cannot read: pluggable, and off by default.

Tesseract/PaddleOCR need a system binary and a much larger image, so this ships
as an interface plus one backend rather than a hard dependency. The document
status says which side of the line a file is on: without a backend a scan ends up
`needs_ocr` (a human or an OCR-capable worker has to look at it) instead of
`ready` with nothing to retrieve.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from app.config import Settings


class OcrUnavailableError(RuntimeError):
    """The configured OCR engine is missing or failed: an environment problem."""


class OcrBackend(Protocol):
    def extract(self, filename: str, payload: bytes) -> list[tuple[int, str]]:
        """Return (page number, text) pairs; empty text for a page is allowed."""
        ...


class TesseractBackend:
    """OCR through the `tesseract` binary.

    Pages are rendered with pymupdf (already a dependency) and the text is read
    from stdout, so the only extra requirement is the binary plus a language pack
    (`tesseract-ocr-chi-sim` for Chinese).
    """

    def __init__(
        self,
        command: str = "tesseract",
        language: str = "chi_sim+eng",
        max_pages: int = 20,
        dpi: int = 200,
        timeout_seconds: int = 120,
    ) -> None:
        self.command = command
        self.language = language
        self.max_pages = max_pages
        self.dpi = dpi
        self.timeout_seconds = timeout_seconds

    def extract(self, filename: str, payload: bytes) -> list[tuple[int, str]]:
        import pymupdf

        if not filename.lower().endswith(".pdf"):
            raise OcrUnavailableError(f"{filename}: the tesseract backend renders PDF pages only")
        document = pymupdf.open(stream=payload, filetype="pdf")
        pages: list[tuple[int, str]] = []
        # mkdtemp + rmtree(ignore_errors=True) rather than TemporaryDirectory(ignore_cleanup_errors=True):
        # that flag does not cover this case. TemporaryDirectory cleans up through
        # shutil.rmtree(onexc=...) and, on a PermissionError, its handler calls chmod to make
        # the directory removable - a chmod that is itself denied raises straight out of
        # __exit__, replacing the OcrUnavailableError the worker classifies on. A scratch
        # PNG left behind is a much smaller problem than reporting the wrong reason, and
        # the difference matters: OcrUnavailableError routes the document to `needs_ocr`
        # (a human, or a deployment with an OCR engine) instead of a generic failure.
        directory = tempfile.mkdtemp(prefix="evalrag-ocr-")
        try:
            image = Path(directory) / "page.png"
            # Indexed access rather than `for page in document`: pymupdf's Document
            # iterates through the legacy __getitem__ protocol, which its stubs do not
            # declare, so the explicit page count keeps the loop type-checkable.
            for number in range(1, min(document.page_count, self.max_pages) + 1):
                page = document[number - 1]
                page.get_pixmap(dpi=self.dpi).save(image)
                try:
                    # Fixed argument list, no shell: no user input reaches the command.
                    result = subprocess.run(
                        [self.command, str(image), "stdout", "-l", self.language],
                        capture_output=True,
                        check=False,
                        timeout=self.timeout_seconds,
                    )
                except FileNotFoundError as exc:
                    raise OcrUnavailableError(f"tesseract binary not found: {self.command}") from exc
                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", "replace").strip()
                    raise OcrUnavailableError(f"tesseract exited {result.returncode}: {stderr[:200]}")
                pages.append((number, result.stdout.decode("utf-8", "replace")))
        finally:
            shutil.rmtree(directory, ignore_errors=True)
            document.close()
        return pages


def create_ocr_backend(settings: Settings) -> OcrBackend | None:
    """None means "this deployment reads text layers only"."""
    name = (settings.ocr_backend or "none").strip().lower()
    if name == "none":
        return None
    if name == "tesseract":
        return TesseractBackend(
            command=settings.ocr_command,
            language=settings.ocr_language,
            max_pages=settings.ocr_max_pages,
        )
    raise ValueError(f"unknown OCR backend: {settings.ocr_backend}")
