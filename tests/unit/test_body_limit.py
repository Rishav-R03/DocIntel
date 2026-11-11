from collections.abc import Iterator

from fastapi import Request
from fastapi.testclient import TestClient

from docintel.api.main import create_app
from docintel.ingestion.upload import MULTIPART_OVERHEAD_BYTES
from tests.conftest import fake_resources, make_settings

MAX_UPLOAD = 1000
LIMIT = MAX_UPLOAD + MULTIPART_OVERHEAD_BYTES


def _client() -> tuple[TestClient, list[int]]:
    app = create_app(make_settings(max_upload_bytes=MAX_UPLOAD), fake_resources())
    seen: list[int] = []

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        seen.append(len(body))
        return {"bytes": len(body)}

    return TestClient(app), seen


def test_declared_length_over_limit_rejected_without_calling_endpoint() -> None:
    client, seen = _client()
    with client:
        response = client.post("/echo", content=b"x" * (LIMIT + 1))
    assert response.status_code == 413
    assert response.json()["code"] == "PAYLOAD_TOO_LARGE"
    assert response.headers["connection"] == "close"
    assert "x-request-id" in response.headers
    assert seen == []


def test_chunked_body_over_limit_rejected_mid_stream() -> None:
    def chunks() -> Iterator[bytes]:  # generator body -> chunked transfer, no Content-Length
        for _ in range(LIMIT // 1000 + 5):
            yield b"x" * 1000

    client, seen = _client()
    with client:
        response = client.post("/echo", content=chunks())
    assert response.status_code == 413
    assert seen == []


def test_body_within_limit_passes() -> None:
    client, seen = _client()
    with client:
        response = client.post("/echo", content=b"x" * LIMIT)
    assert response.status_code == 200
    assert seen == [LIMIT]


def test_invalid_content_length_is_400() -> None:
    client, _ = _client()
    with client:
        response = client.post("/echo", content=b"x", headers={"content-length": "abc"})
    assert response.status_code == 400
