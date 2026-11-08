import hashlib
import tracemalloc
from collections.abc import Iterator

import pytest

from docintel.domain.errors import (
    InvalidRequestError,
    PayloadTooLargeError,
    UnsupportedMediaTypeError,
)
from docintel.ingestion.upload import read_upload, sanitize_filename, sniff_mime_type
from tests.helpers import StreamingRequest, make_image, make_pdf, multipart_chunks

MiB = 1024 * 1024


async def _read(chunks: Iterator[bytes], *, max_file_bytes: int = 25 * MiB, **kw: object):  # type: ignore[no-untyped-def]
    req = StreamingRequest(chunks, **kw)  # type: ignore[arg-type]
    upload = await read_upload(req.request, max_file_bytes=max_file_bytes, spool_bytes=MiB)
    return upload, req


def _file(content: bytes, filename: str = "doc.pdf", ctype: str | None = "application/pdf"):  # type: ignore[no-untyped-def]
    return (filename, ctype, [content])


async def test_reads_pdf_with_fields_hash_and_content() -> None:
    pdf = make_pdf(2)
    upload, _ = await _read(
        multipart_chunks(fields={"schema_id": "invoice.v1"}, file=_file(pdf, "inv.pdf"))
    )
    try:
        assert upload.mime_type == "application/pdf"
        assert upload.size_bytes == len(pdf)
        assert upload.sha256 == hashlib.sha256(pdf).hexdigest()
        assert upload.fields == {"schema_id": "invoice.v1"}
        assert upload.filename == "inv.pdf"
        assert upload.file.read() == pdf
    finally:
        upload.close()


@pytest.mark.parametrize(
    ("content", "mime"),
    [(make_image("PNG"), "image/png"), (make_image("JPEG"), "image/jpeg")],
)
async def test_sniffs_images(content: bytes, mime: str) -> None:
    upload, _ = await _read(multipart_chunks(file=_file(content, "x", None)))
    assert upload.mime_type == mime
    upload.close()


async def test_declared_content_type_is_not_trusted() -> None:
    png = make_image("PNG")
    upload, _ = await _read(multipart_chunks(file=_file(png, "fake.pdf", "application/pdf")))
    assert upload.mime_type == "image/png"
    assert upload.declared_content_type == "application/pdf"
    upload.close()


async def test_exe_renamed_to_pdf_is_rejected_after_first_chunk() -> None:
    exe = b"MZ\x90\x00" + b"\x00" * 60
    chunks = multipart_chunks(file=("setup.pdf", "application/pdf", [exe] + [b"\x00" * MiB] * 50))
    with pytest.raises(UnsupportedMediaTypeError):
        await _read(chunks)


async def test_unsupported_type_stops_reading_the_body() -> None:
    req = StreamingRequest(
        multipart_chunks(file=("a.gif", None, [b"GIF89a.." + b"\x00" * 100] + [b"\x00" * MiB] * 50))
    )
    with pytest.raises(UnsupportedMediaTypeError):
        await read_upload(req.request, max_file_bytes=25 * MiB, spool_bytes=MiB)
    assert req.bytes_read < 2 * MiB  # 50 MB offered, ~nothing consumed


async def test_file_exactly_at_limit_is_accepted() -> None:
    content = b"%PDF-" + b"x" * 995
    upload, _ = await _read(multipart_chunks(file=_file(content)), max_file_bytes=1000)
    assert upload.size_bytes == 1000
    upload.close()


async def test_file_one_byte_over_limit_is_rejected() -> None:
    content = b"%PDF-" + b"x" * 996
    with pytest.raises(PayloadTooLargeError):
        await _read(multipart_chunks(file=_file(content)), max_file_bytes=1000)


async def test_oversized_stream_stops_at_the_cap_with_bounded_memory() -> None:
    """Exit criterion: 100 MB offered without Content-Length -> 413 after ~25 MB read, and peak
    Python memory stays around the spool threshold, not the file size."""

    def huge() -> Iterator[bytes]:
        yield b"%PDF-1.7\n"
        for _ in range(100):
            yield b"\x00" * MiB

    req = StreamingRequest(multipart_chunks(file=("big.pdf", "application/pdf", huge())))
    tracemalloc.start()
    try:
        with pytest.raises(PayloadTooLargeError):
            await read_upload(req.request, max_file_bytes=25 * MiB, spool_bytes=MiB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert req.bytes_read <= 26 * MiB
    assert peak < 6 * MiB, f"peak {peak / MiB:.1f} MiB"


@pytest.mark.parametrize(
    ("chunks", "message"),
    [
        (multipart_chunks(fields={"schema_id": "x"}), "Missing 'file'"),
        (multipart_chunks(file=_file(b"")), "empty"),
        (
            iter([*multipart_chunks(file=_file(make_pdf(1)))][:-2]),  # drop closing boundary
            "Truncated",
        ),
        (
            iter(
                [
                    *list(multipart_chunks(file=_file(make_pdf(1))))[:-1],
                    *multipart_chunks(file=_file(make_pdf(1))),
                ]
            ),
            "Exactly one",
        ),
        (
            multipart_chunks(fields={f"f{i}": "v" for i in range(11)}),
            "Too many form parts",
        ),
        (multipart_chunks(fields={"options": "x" * (65 * 1024)}), "too large"),
    ],
    ids=["no-file", "empty", "truncated", "two-files", "too-many-parts", "huge-field"],
)
async def test_malformed_uploads_are_400(chunks: Iterator[bytes], message: str) -> None:
    with pytest.raises(InvalidRequestError, match=message):
        await _read(chunks)


async def test_requires_multipart_content_type() -> None:
    with pytest.raises(InvalidRequestError, match="multipart/form-data"):
        await _read(iter([b"{}"]), content_type="application/json")


def test_sniff_signatures() -> None:
    assert sniff_mime_type(b"%PDF-1.4") == "application/pdf"
    assert sniff_mime_type(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert sniff_mime_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_mime_type(b"GIF89a") is None
    assert sniff_mime_type(b" %PDF-") is None  # strict: signature must be at offset 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("invoice.pdf", "invoice.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\scan.png", "scan.png"),
        ("evil\r\nname.pdf", "evilname.pdf"),
        ("", None),
        (None, None),
    ],
)
def test_sanitize_filename(raw: str | None, expected: str | None) -> None:
    assert sanitize_filename(raw) == expected
