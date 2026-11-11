"""End-to-end Phase 2: HTTP upload -> S3 (moto) -> Postgres -> SQS (moto)."""

import hashlib
import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from docintel.api.main import create_app
from docintel.settings import Settings
from tests.helpers import make_image, make_pdf
from tests.integration.conftest import Aws, app_settings, fetch

pytestmark = pytest.mark.integration


@pytest.fixture
def client(db_settings: Settings, aws: Aws) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings(db_settings, aws))) as c:
        yield c


def _submit(
    client: TestClient,
    content: bytes,
    *,
    filename: str = "invoice.pdf",
    ctype: str = "application/pdf",
    schema_id: str | None = "invoice.v1",
    headers: dict[str, str] | None = None,
):  # type: ignore[no-untyped-def]
    data = {"schema_id": schema_id} if schema_id is not None else {}
    return client.post(
        "/api/v1/extract",
        files={"file": (filename, content, ctype)},
        data=data,
        headers=headers or {},
    )


def test_submit_stages_persists_and_enqueues(
    client: TestClient, aws: Aws, db_settings: Settings
) -> None:
    pdf = make_pdf(3)
    sha = hashlib.sha256(pdf).hexdigest()
    response = _submit(client, pdf, headers={"X-Request-Id": "req-e2e-1", "X-Client-Id": "acme"})

    # --- HTTP contract
    assert response.status_code == 202, response.text
    body = response.json()
    job_id = body["job_id"]
    assert body["status"] == "STAGED"
    assert body["status_url"] == f"/api/v1/jobs/{job_id}"
    assert response.headers["location"] == body["status_url"]
    assert uuid.UUID(job_id).version == 7

    # --- Postgres: job row + ordered events
    [job] = fetch(db_settings, "SELECT * FROM jobs WHERE id = $1", uuid.UUID(job_id))
    assert job["status"] == "STAGED"
    assert job["mode"] == "ASYNC"
    assert (job["page_count"], job["size_bytes"], job["payload_sha256"]) == (3, len(pdf), sha)
    assert (job["client_id"], job["request_id"], job["file_name"]) == (
        "acme",
        "req-e2e-1",
        "invoice.pdf",
    )
    stages = [
        r["stage"]
        for r in fetch(
            db_settings,
            "SELECT stage FROM job_events WHERE job_id = $1 ORDER BY id",
            uuid.UUID(job_id),
        )
    ]
    assert stages == ["UPLOADED", "S3_STAGED"]

    # --- S3: object, KMS, checksum, tags, metadata
    assert aws.objects() == [job["s3_key"]]
    assert job["s3_key"].startswith("staging/")
    assert job["s3_key"].endswith(f"/{job_id}/source.pdf")
    # moto 5.1.4 ignores ChecksumSHA256 entirely (doesn't store or validate it), so checksum
    # correctness is asserted on the request in tests/unit/test_staging.py instead.
    head = aws.s3.head_object(Bucket=aws.bucket, Key=job["s3_key"])
    assert head["ContentType"] == "application/pdf"
    assert head["ContentLength"] == len(pdf)
    assert head["ServerSideEncryption"] == "aws:kms"
    assert aws.kms_key_id in head["SSEKMSKeyId"]
    assert head["Metadata"] == {"sha256": sha}
    assert aws.s3.get_object(Bucket=aws.bucket, Key=job["s3_key"])["Body"].read() == pdf
    tags = aws.s3.get_object_tagging(Bucket=aws.bucket, Key=job["s3_key"])["TagSet"]
    assert {"Key": "job_id", "Value": job_id} in tags

    # --- SQS: versioned message + correlation attribute
    [message] = aws.messages()
    assert json.loads(message["Body"]) == {
        "type": "job.submitted",
        "v": 1,
        "job_id": job_id,
        "schema_id": "invoice.v1",
    }
    assert message["MessageAttributes"]["request_id"]["StringValue"] == "req-e2e-1"

    # --- GET status, scoped to the submitting client
    status = client.get(body["status_url"], headers={"X-Client-Id": "acme"})
    assert status.status_code == 200
    assert status.json()["status"] == "STAGED"
    assert status.json()["page_count"] == 3
    assert client.get(body["status_url"], headers={"X-Client-Id": "other"}).status_code == 404
    assert client.get(body["status_url"]).status_code == 404


def test_png_declared_as_pdf_is_stored_as_png(client: TestClient, aws: Aws) -> None:
    response = _submit(client, make_image("PNG"), filename="scan.pdf", ctype="application/pdf")
    assert response.status_code == 202
    [key] = aws.objects()
    assert key.endswith("/source.png")
    assert aws.s3.head_object(Bucket=aws.bucket, Key=key)["ContentType"] == "image/png"


@pytest.mark.parametrize(
    ("content", "schema_id", "status", "code"),
    [
        (b"MZ\x90\x00" + b"\x00" * 100, "invoice.v1", 415, "UNSUPPORTED_MEDIA_TYPE"),
        (make_pdf(1, password="pw"), "invoice.v1", 422, "PDF_ENCRYPTED"),
        (make_pdf(51), "invoice.v1", 422, "PAGE_LIMIT_EXCEEDED"),
        (b"%PDF-1.7 not really", "invoice.v1", 422, "DOCUMENT_UNREADABLE"),
        (make_pdf(1), "passport.v9", 422, "SCHEMA_NOT_FOUND"),
        (make_pdf(1), None, 400, "INVALID_REQUEST"),
    ],
    ids=["exe-as-pdf", "encrypted", "51-pages", "unreadable", "unknown-schema", "no-schema"],
)
def test_rejected_uploads_leave_no_trace(
    client: TestClient, aws: Aws, content: bytes, schema_id: str | None, status: int, code: str
) -> None:
    response = _submit(client, content, schema_id=schema_id)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert aws.objects() == []
    assert aws.messages() == []


def test_unknown_form_field_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/extract",
        files={"file": ("a.pdf", make_pdf(1), "application/pdf")},
        data={"schema_id": "invoice.v1", "callback_url": "http://x"},
    )
    assert response.status_code == 400
    assert "callback_url" in response.json()["detail"]


def test_queue_failure_marks_job_failed(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws, sqs_jobs_url=f"{aws.endpoint}/123456789012/missing")
    with TestClient(create_app(settings)) as client:
        response = _submit(client, make_pdf(1), headers={"X-Request-Id": "req-queue-down"})
    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.headers["retry-after"] == "5"
    [job] = fetch(db_settings, "SELECT * FROM jobs WHERE request_id = 'req-queue-down'")
    assert (job["status"], job["error_code"]) == ("FAILED", "ENQUEUE_FAILED")
    stages = [
        r["stage"]
        for r in fetch(
            db_settings, "SELECT stage FROM job_events WHERE job_id = $1 ORDER BY id", job["id"]
        )
    ]
    assert stages == ["UPLOADED", "S3_STAGED", "FAILED"]


def test_database_down_returns_503_and_removes_staged_object(
    db_settings: Settings, aws: Aws
) -> None:
    settings = app_settings(db_settings, aws, db_port=1, db_connect_timeout_s=1)
    with TestClient(create_app(settings)) as client:
        response = _submit(client, make_pdf(1))
    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert aws.objects() == []  # compensating delete ran
    assert aws.messages() == []


def test_storage_down_returns_503_and_persists_nothing(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws, s3_bucket="bucket-that-does-not-exist")
    with TestClient(create_app(settings)) as client:
        response = _submit(client, make_pdf(1), headers={"X-Request-Id": "req-s3-down"})
    assert response.status_code == 503
    assert fetch(db_settings, "SELECT 1 FROM jobs WHERE request_id = 'req-s3-down'") == []
    assert aws.messages() == []


def test_get_job_errors(client: TestClient) -> None:
    unknown = client.get(f"/api/v1/jobs/{uuid.uuid4()}")
    assert (unknown.status_code, unknown.json()["code"]) == (404, "JOB_NOT_FOUND")
    assert client.get("/api/v1/jobs/not-a-uuid").status_code == 400
