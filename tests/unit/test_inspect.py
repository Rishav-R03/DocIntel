import io

import pytest

from docintel.domain.errors import (
    DocumentUnreadableError,
    ImageTooLargeError,
    PageLimitExceededError,
    PdfEncryptedError,
)
from docintel.ingestion.inspect import inspect_document
from tests.helpers import make_image, make_pdf


def _inspect(content: bytes, mime: str, *, max_pages: int = 50, max_pixels: int = 50_000_000):  # type: ignore[no-untyped-def]
    return inspect_document(
        io.BytesIO(content), mime, max_pages=max_pages, max_image_pixels=max_pixels
    )


def test_pdf_page_count() -> None:
    assert _inspect(make_pdf(3), "application/pdf").page_count == 3


def test_encrypted_pdf_rejected() -> None:
    with pytest.raises(PdfEncryptedError):
        _inspect(make_pdf(1, password="secret"), "application/pdf")


def test_page_limit() -> None:
    _inspect(make_pdf(5), "application/pdf", max_pages=5)
    with pytest.raises(PageLimitExceededError, match="6 pages; the limit is 5"):
        _inspect(make_pdf(6), "application/pdf", max_pages=5)


@pytest.mark.parametrize("content", [b"%PDF-1.7\ngarbage", b"%PDF-"], ids=["garbage", "header"])
def test_unparseable_pdf(content: bytes) -> None:
    with pytest.raises(DocumentUnreadableError):
        _inspect(content, "application/pdf")


@pytest.mark.parametrize(("fmt", "mime"), [("PNG", "image/png"), ("JPEG", "image/jpeg")])
def test_image_dimensions(fmt: str, mime: str) -> None:
    info = _inspect(make_image(fmt, (64, 48)), mime)
    assert (info.page_count, info.width, info.height) == (1, 64, 48)


def test_image_pixel_limit_checked_from_header() -> None:
    with pytest.raises(ImageTooLargeError, match="64x48"):
        _inspect(make_image("PNG", (64, 48)), "image/png", max_pixels=64 * 48 - 1)


def test_truncated_png_unreadable() -> None:
    png = make_image("PNG")
    with pytest.raises(DocumentUnreadableError):
        _inspect(png[:40], "image/png")


def test_file_position_is_reset_for_the_next_reader() -> None:
    buf = io.BytesIO(make_pdf(1))
    inspect_document(buf, "application/pdf", max_pages=5, max_image_pixels=1)
    assert buf.tell() == 0
