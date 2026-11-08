from collections.abc import Iterator
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from docintel.api.errors import PROBLEM_MEDIA_TYPE
from docintel.api.main import create_app
from docintel.domain.errors import DocIntelError, ErrorCode
from tests.conftest import fake_resources, make_settings


class TeapotError(DocIntelError):
    code = ErrorCode.HTTP_ERROR
    status_code = HTTPStatus.IM_A_TEAPOT
    title = "Teapot"


def _app() -> FastAPI:
    app = create_app(make_settings(), fake_resources())

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/teapot")
    async def teapot() -> None:
        raise TeapotError("short and stout")

    @app.get("/typed")
    async def typed(n: int) -> dict[str, int]:
        return {"n": n}

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(_app()) as c:
        yield c


def test_generates_request_id_when_absent(client: TestClient) -> None:
    rid = client.get("/health/live").headers["X-Request-Id"]
    assert len(rid) == 32


def test_echoes_valid_inbound_request_id(client: TestClient) -> None:
    response = client.get("/health/live", headers={"X-Request-Id": "gw-abc.123:x"})
    assert response.headers["X-Request-Id"] == "gw-abc.123:x"


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "semi;colon", 'quote"d'])
def test_replaces_unsafe_inbound_request_id(client: TestClient, bad: str) -> None:
    response = client.get("/health/live", headers={"X-Request-Id": bad})
    assert response.headers["X-Request-Id"] != bad


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["code"] == "NOT_FOUND"
    assert body["request_id"] == response.headers["X-Request-Id"]


def test_unhandled_exception_is_masked_500_with_request_id(client: TestClient) -> None:
    response = client.get("/boom", headers={"X-Request-Id": "req-1"})
    assert response.status_code == 500
    assert response.headers["X-Request-Id"] == "req-1"
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["request_id"] == "req-1"
    assert "secret internal detail" not in response.text


def test_domain_error_maps_to_its_status_and_code(client: TestClient) -> None:
    response = client.get("/teapot")
    assert response.status_code == 418
    body = response.json()
    assert body["type"] == "urn:docintel:error:http_error"
    assert body["detail"] == "short and stout"


def test_validation_error_is_400_with_field_errors(client: TestClient) -> None:
    response = client.get("/typed", params={"n": "abc"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["errors"][0]["loc"] == ["query", "n"]
