"""Test document generators and multipart/ASGI request builders."""

import io
from collections.abc import Iterable, Iterator

from PIL import Image
from pypdf import PdfWriter
from starlette.requests import Request

BOUNDARY = "docintelTestBoundary7MA4YWxk"
MULTIPART_CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def make_pdf(pages: int = 3, *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)  # A4 in points
    if password is not None:
        writer.encrypt(password, algorithm="RC4-128")  # RC4 needs no optional crypto dependency
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_image(fmt: str = "PNG", size: tuple[int, int] = (64, 48)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


def multipart_chunks(
    *,
    fields: dict[str, str] | None = None,
    file: tuple[str, str | None, Iterable[bytes]] | None = None,
    file_field: str = "file",
) -> Iterator[bytes]:
    """Yield a multipart body lazily, so tests can stream far more data than they allocate.

    ``file`` is ``(filename, declared content type, content chunks)``.
    """
    for name, value in (fields or {}).items():
        yield (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    if file is not None:
        filename, content_type, chunks = file
        header = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{file_field}"'
        header += f'; filename="{filename}"\r\n'
        if content_type:
            header += f"Content-Type: {content_type}\r\n"
        yield (header + "\r\n").encode()
        yield from chunks
        yield b"\r\n"
    yield f"--{BOUNDARY}--\r\n".encode()


def multipart_body(**kwargs: object) -> bytes:
    return b"".join(multipart_chunks(**kwargs))  # type: ignore[arg-type]


class StreamingRequest:
    """An ASGI request whose body comes from an iterator; records how much was actually read."""

    def __init__(self, chunks: Iterable[bytes], content_type: str = MULTIPART_CONTENT_TYPE):
        self._chunks = iter(chunks)
        self.bytes_read = 0
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/extract",
            "query_string": b"",
            "headers": [(b"content-type", content_type.encode())],
        }
        self.request = Request(scope, self._receive)

    async def _receive(self) -> dict[str, object]:
        chunk = next(self._chunks, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        self.bytes_read += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}
