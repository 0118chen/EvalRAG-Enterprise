"""The OCR seam: which backend is built, and how its failures are reported."""

import tempfile
from pathlib import Path

import pymupdf
import pytest

from app.config import Settings
from app.core.ocr import OcrUnavailableError, TesseractBackend, create_ocr_backend


def _scratch_is_writable() -> bool:
    """OCR renders each page to a PNG before calling the engine, so it needs scratch space.

    When the environment cannot provide a writable temporary directory (a locked-down
    sandbox, a read-only /tmp), that is an environment property, not a defect in the
    backend - failing here would look like a product bug and hide the real cause.
    """
    try:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "probe").write_bytes(b"probe")
    except OSError:
        return False
    return True


requires_scratch_space = pytest.mark.skipif(
    not _scratch_is_writable(),
    reason="no writable temporary directory in this environment",
)


def _blank_page_pdf() -> bytes:
    """An image-only page: what a scan looks like to the extractor."""
    document = pymupdf.open()
    page = document.new_page()
    page.draw_rect(pymupdf.Rect(40, 40, 400, 700), color=(0, 0, 0), width=2)
    payload = document.tobytes()
    document.close()
    return payload


def test_no_backend_is_built_by_default() -> None:
    assert create_ocr_backend(Settings()) is None
    assert create_ocr_backend(Settings(ocr_backend="none")) is None
    assert create_ocr_backend(Settings(ocr_backend="  NONE ")) is None


def test_tesseract_backend_takes_its_settings_from_configuration() -> None:
    backend = create_ocr_backend(
        Settings(
            ocr_backend="tesseract",
            ocr_command="tesseract-custom",
            ocr_language="chi_sim",
            ocr_max_pages=3,
        )
    )

    assert isinstance(backend, TesseractBackend)
    assert backend.command == "tesseract-custom"
    assert backend.language == "chi_sim"
    assert backend.max_pages == 3


def test_an_unknown_backend_name_is_a_configuration_error() -> None:
    with pytest.raises(ValueError, match="unknown OCR backend"):
        create_ocr_backend(Settings(ocr_backend="magic-ocr"))


@requires_scratch_space
def test_a_missing_binary_is_reported_as_unavailable_not_as_a_crash() -> None:
    """The document is fine and the deployment is broken: those need different
    handling, so the backend raises something the worker can classify.

    This also pins that a failing scratch-directory cleanup cannot replace that
    exception: the diagnostic is what routes the document to `needs_ocr` instead of a
    generic failure, so it must survive whatever happens on the way out.
    """
    backend = TesseractBackend(command="tesseract-that-is-not-installed")

    with pytest.raises(OcrUnavailableError, match="not found"):
        backend.extract("scan.pdf", _blank_page_pdf())


def test_the_tesseract_backend_declines_input_it_cannot_render() -> None:
    with pytest.raises(OcrUnavailableError, match="PDF pages only"):
        TesseractBackend().extract("photo.jpg", b"not a pdf")
