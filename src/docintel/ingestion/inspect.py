"""Document inspection before staging: page count, encryption, image dimensions.

Synchronous and CPU-bound (pypdf/Pillow), so callers run it via ``asyncio.to_thread``. Images are
only *opened* (header parse) and verified, never fully decoded, so decompression bombs are caught
by their declared dimensions before any pixel buffer is allocated.
"""

from dataclasses import dataclass
from typing import IO

from PIL import Image
from pypdf import PdfReader

from docintel.domain.errors import (
    DocIntelError,
    DocumentUnreadableError,
    ImageTooLargeError,
    PageLimitExceededError,
    PdfEncryptedError,
)

_PIL_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG"}


@dataclass(frozen=True)
class DocumentInfo:
    page_count: int
    width: int | None = None
    height: int | None = None


def inspect_document(
    file: IO[bytes], mime_type: str, *, max_pages: int, max_image_pixels: int
) -> DocumentInfo:
    file.seek(0)
    try:
        if mime_type == "application/pdf":
            return _inspect_pdf(file, max_pages)
        return _inspect_image(file, mime_type, max_image_pixels)
    finally:
        file.seek(0)


def _inspect_pdf(file: IO[bytes], max_pages: int) -> DocumentInfo:
    try:
        reader = PdfReader(file, strict=False)
        if reader.is_encrypted:
            # v1 rejects every encrypted PDF, including owner-password-only ones that open
            # without a password: we can't vouch that the model receives readable content.
            raise PdfEncryptedError("Password-protected or encrypted PDFs are not supported.")
        pages = len(reader.pages)
    except DocIntelError:
        raise
    except Exception as exc:  # pypdf raises many exception types on malformed input
        raise DocumentUnreadableError("The PDF could not be parsed.") from exc
    if pages == 0:
        raise DocumentUnreadableError("The PDF has no pages.")
    if pages > max_pages:
        raise PageLimitExceededError(f"The PDF has {pages} pages; the limit is {max_pages}.")
    return DocumentInfo(page_count=pages)


def _inspect_image(file: IO[bytes], mime_type: str, max_pixels: int) -> DocumentInfo:
    try:
        with Image.open(file, formats=[_PIL_FORMATS[mime_type]]) as image:
            width, height = image.size
            if width * height > max_pixels:
                raise ImageTooLargeError(
                    f"The image is {width}x{height} pixels; the limit is {max_pixels:,} pixels."
                )
            image.verify()  # structural/CRC checks without decoding pixel data
    except DocIntelError:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageTooLargeError("The image dimensions are too large.") from exc
    except Exception as exc:
        raise DocumentUnreadableError("The image could not be decoded.") from exc
    return DocumentInfo(page_count=1, width=width, height=height)
