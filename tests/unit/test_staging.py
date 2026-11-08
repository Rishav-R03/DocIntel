import base64
import hashlib
import io
from typing import Any

from docintel.ingestion.staging import DocumentStore


class RecordingS3:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def put_object(self, **kwargs: Any) -> dict[str, Any]:
        kwargs["Body"] = kwargs["Body"].read()  # capture what would be sent
        self.calls.append(kwargs)
        return {}


async def _put(kms_key_id: str | None) -> dict[str, Any]:
    data = b"%PDF-1.7 test document"
    body = io.BytesIO(data)
    body.seek(7)  # store must rewind: callers may have read the file (hashing, inspection)
    s3 = RecordingS3()
    await DocumentStore(s3, bucket="b", kms_key_id=kms_key_id).put_source(
        key="staging/k",
        body=body,
        size_bytes=len(data),
        mime_type="application/pdf",
        sha256_hex=hashlib.sha256(data).hexdigest(),
        tags={"job_id": "j-1", "schema_id": "invoice.v1"},
    )
    [call] = s3.calls
    return call


async def test_put_sends_sha256_checksum_tags_and_full_body() -> None:
    call = await _put(kms_key_id=None)
    data = b"%PDF-1.7 test document"
    assert call["Body"] == data
    # S3 recomputes this server-side and rejects the PUT on mismatch (end-to-end integrity).
    assert call["ChecksumSHA256"] == base64.b64encode(hashlib.sha256(data).digest()).decode()
    assert call["Tagging"] == "job_id=j-1&schema_id=invoice.v1"
    assert call["ContentType"] == "application/pdf"
    assert "ServerSideEncryption" not in call


async def test_put_requests_sse_kms_when_key_configured() -> None:
    call = await _put(kms_key_id="arn:aws:kms:us-east-1:1:key/abc")
    assert call["ServerSideEncryption"] == "aws:kms"
    assert call["SSEKMSKeyId"] == "arn:aws:kms:us-east-1:1:key/abc"
    assert call["BucketKeyEnabled"] is True
