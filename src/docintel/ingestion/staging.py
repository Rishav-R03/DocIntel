"""S3 staging for source documents (FR-02)."""

import base64
from collections.abc import Mapping
from datetime import datetime
from typing import IO, Any
from urllib.parse import urlencode
from uuid import UUID

_EXTENSIONS = {"application/pdf": "pdf", "image/png": "png", "image/jpeg": "jpg"}


def source_key(job_id: UUID, mime_type: str, at: datetime) -> str:
    """``staging/YYYY/MM/DD/<job_id>/source.<ext>``: date prefix for ops browsing and lifecycle
    reasoning, job id for uniqueness. The client's filename never becomes part of a key."""
    return f"staging/{at:%Y/%m/%d}/{job_id}/source.{_EXTENSIONS[mime_type]}"


class DocumentStore:
    def __init__(self, s3: Any, *, bucket: str, kms_key_id: str | None) -> None:
        self._s3 = s3
        self._bucket = bucket
        self._kms_key_id = kms_key_id

    async def put_source(
        self,
        *,
        key: str,
        body: IO[bytes],
        size_bytes: int,
        mime_type: str,
        sha256_hex: str,
        tags: Mapping[str, str],
    ) -> None:
        """Single PUT (documents are <= 25 MB; multipart only pays off for much larger objects).

        ``ChecksumSHA256`` reuses the hash computed during upload: S3 recomputes it server-side
        and rejects the write on mismatch, so corruption between our process and S3 can't land.
        """
        body.seek(0)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentLength": size_bytes,
            "ContentType": mime_type,
            "ChecksumSHA256": base64.b64encode(bytes.fromhex(sha256_hex)).decode("ascii"),
            "Metadata": {"sha256": sha256_hex},
            "Tagging": urlencode(tags),
        }
        if self._kms_key_id:
            kwargs |= {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
                "BucketKeyEnabled": True,
            }
        await self._s3.put_object(**kwargs)

    async def get(self, key: str) -> bytes:
        response = await self._s3.get_object(Bucket=self._bucket, Key=key)
        async with response["Body"] as stream:
            data: bytes = await stream.read()
        return data

    async def delete(self, key: str) -> None:
        await self._s3.delete_object(Bucket=self._bucket, Key=key)
