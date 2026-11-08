"""Streaming ``multipart/form-data`` reader for document uploads (FR-01, FR-03).

Why not FastAPI's ``UploadFile``: Starlette parses the *entire* body to disk before the endpoint
runs, so a size limit can only be enforced after the fact. Driving the multipart parser ourselves
lets every check run *while bytes arrive*:

* the byte counter aborts with 413 the moment the file exceeds the cap (never reading the rest);
* the first 8 bytes are sniffed against PDF/PNG/JPEG signatures and anything else is rejected
  with 415 immediately; the client-declared Content-Type is never trusted;
* SHA-256 is computed incrementally (it later doubles as S3's end-to-end integrity checksum);
* data spools to a ``SpooledTemporaryFile``: RAM up to ``spool_bytes``, then /tmp.

Peak memory per upload is therefore ~``spool_bytes`` + one network chunk, regardless of file size.
"""

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import PureWindowsPath

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import ClientDisconnect, Request

from docintel.domain.errors import (
    InvalidRequestError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)

FILE_FIELD = "file"
MAX_PARTS = 10
MAX_FIELD_BYTES = 64 * 1024  # all non-file fields combined
# Generous allowance on top of the file cap for boundaries, part headers and small fields; used
# by the request-level body guard, which is a coarse backstop to the exact per-file check here.
MULTIPART_OVERHEAD_BYTES = 256 * 1024

_SIGNATURES = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)
_SNIFF_BYTES = max(len(sig) for sig, _ in _SIGNATURES)
SUPPORTED_MIME_TYPES = frozenset(mime for _, mime in _SIGNATURES)


def sniff_mime_type(head: bytes) -> str | None:
    for signature, mime in _SIGNATURES:
        if head.startswith(signature):
            return mime
    return None


def sanitize_filename(raw: str | None) -> str | None:
    """Keep only a display name: no directories, no control characters. It is stored as metadata
    and never used as a filesystem path or S3 key."""
    if not raw:
        return None
    name = PureWindowsPath(raw).name  # strips both "/" and "\" directory components
    name = "".join(ch for ch in name if ch.isprintable())[:255]
    return name or None


@dataclass
class UploadedDocument:
    file: tempfile.SpooledTemporaryFile[bytes]
    filename: str | None
    declared_content_type: str | None
    mime_type: str  # sniffed, authoritative
    size_bytes: int
    sha256: str
    fields: dict[str, str]

    def close(self) -> None:
        self.file.close()


class _UploadParser:
    def __init__(self, boundary: bytes, *, max_file_bytes: int, spool_bytes: int) -> None:
        self._max_file_bytes = max_file_bytes
        self._spool = tempfile.SpooledTemporaryFile(max_size=spool_bytes, mode="w+b")  # noqa: SIM115
        self._hash = hashlib.sha256()
        self._head = b""
        self._mime: str | None = None
        self._size = 0
        self._parts = 0
        self._field_bytes = 0
        self._fields: dict[str, str] = {}
        self._headers: dict[bytes, bytes] = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._current: str | None = None  # FILE_FIELD, a field name, or None
        self._field_buf = bytearray()
        self._file_seen = False
        self._file_complete = False
        self._ended = False
        self._filename: str | None = None
        self._declared_content_type: str | None = None
        self._parser = MultipartParser(
            boundary,
            callbacks={
                "on_part_begin": self._on_part_begin,
                "on_header_field": self._on_header_field,
                "on_header_value": self._on_header_value,
                "on_header_end": self._on_header_end,
                "on_headers_finished": self._on_headers_finished,
                "on_part_data": self._on_part_data,
                "on_part_end": self._on_part_end,
                "on_end": self._on_end,
            },
        )

    # ------------------------------------------------------------------ driving
    def write(self, chunk: bytes) -> None:
        try:
            self._parser.write(chunk)
        except MultipartParseError as exc:
            raise InvalidRequestError("Malformed multipart body.") from exc

    def finish(self) -> UploadedDocument:
        try:
            self._parser.finalize()
        except MultipartParseError as exc:
            raise InvalidRequestError("Malformed multipart body.") from exc
        if not self._file_seen:
            raise InvalidRequestError(f"Missing '{FILE_FIELD}' part.")
        if not (self._file_complete and self._ended):
            raise InvalidRequestError("Truncated multipart body.")
        assert self._mime is not None  # noqa: S101 - set whenever _file_complete is True
        self._spool.seek(0)
        return UploadedDocument(
            file=self._spool,
            filename=sanitize_filename(self._filename),
            declared_content_type=self._declared_content_type,
            mime_type=self._mime,
            size_bytes=self._size,
            sha256=self._hash.hexdigest(),
            fields=dict(self._fields),
        )

    def discard(self) -> None:
        self._spool.close()

    # ------------------------------------------------------------------ parser callbacks
    def _on_part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_PARTS:
            raise InvalidRequestError(f"Too many form parts (max {MAX_PARTS}).")
        self._headers = {}

    def _on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def _on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def _on_header_end(self) -> None:
        self._headers[bytes(self._header_field).lower()] = bytes(self._header_value)
        self._header_field.clear()
        self._header_value.clear()

    def _on_headers_finished(self) -> None:
        disposition = self._headers.get(b"content-disposition")
        if disposition is None:
            raise InvalidRequestError("Form part without Content-Disposition.")
        _, params = parse_options_header(disposition)
        name = params.get(b"name", b"").decode("utf-8", "replace")
        if name == FILE_FIELD:
            if self._file_seen:
                raise InvalidRequestError(f"Exactly one '{FILE_FIELD}' part is allowed.")
            self._file_seen = True
            filename = params.get(b"filename")
            self._filename = filename.decode("utf-8", "replace") if filename else None
            declared = self._headers.get(b"content-type")
            self._declared_content_type = declared.decode("latin-1") if declared else None
        elif not name or name in self._fields:
            raise InvalidRequestError("Form fields must be named and not repeated.")
        else:
            self._field_buf.clear()
        self._current = name

    def _on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._current == FILE_FIELD:
            self._write_file(chunk)
            return
        self._field_bytes += len(chunk)
        if self._field_bytes > MAX_FIELD_BYTES:
            raise InvalidRequestError("Form fields are too large.")
        self._field_buf += chunk

    def _on_part_end(self) -> None:
        if self._current == FILE_FIELD:
            if self._size == 0:
                raise InvalidRequestError("The uploaded file is empty.")
            if self._mime is None:  # file shorter than the sniff window
                self._mime = self._sniff()
            self._file_complete = True
        elif self._current is not None:
            try:
                self._fields[self._current] = self._field_buf.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InvalidRequestError("Form fields must be UTF-8.") from exc
        self._current = None

    def _on_end(self) -> None:
        self._ended = True

    # ------------------------------------------------------------------ file handling
    def _write_file(self, chunk: bytes) -> None:
        self._size += len(chunk)
        if self._size > self._max_file_bytes:
            raise PayloadTooLargeError(
                f"The file exceeds the {self._max_file_bytes // (1024 * 1024)} MB limit."
            )
        if self._mime is None:
            self._head += chunk[: _SNIFF_BYTES - len(self._head)]
            if len(self._head) >= _SNIFF_BYTES:
                self._mime = self._sniff()
        self._hash.update(chunk)
        self._spool.write(chunk)

    def _sniff(self) -> str:
        mime = sniff_mime_type(self._head)
        if mime is None:
            raise UnsupportedMediaTypeError("Only PDF, PNG and JPEG documents are supported.")
        return mime


def _boundary(request: Request) -> bytes:
    content_type, params = parse_options_header(request.headers.get("content-type"))
    if content_type != b"multipart/form-data":
        raise InvalidRequestError("Expected a multipart/form-data request body.")
    boundary = params.get(b"boundary")
    if not boundary:
        raise InvalidRequestError("multipart/form-data request without a boundary.")
    return boundary


async def read_upload(
    request: Request, *, max_file_bytes: int, spool_bytes: int
) -> UploadedDocument:
    """Consume the request body. The caller owns the returned document and must ``close()`` it."""
    parser = _UploadParser(
        _boundary(request), max_file_bytes=max_file_bytes, spool_bytes=spool_bytes
    )
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        return parser.finish()
    except ClientDisconnect as exc:
        parser.discard()
        raise InvalidRequestError("Client disconnected during upload.") from exc
    except BaseException:
        parser.discard()
        raise
