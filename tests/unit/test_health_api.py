from fastapi.testclient import TestClient

from docintel.api.main import create_app
from tests.conftest import FakeCheck, fake_resources, make_settings


def test_live_is_ok() -> None:
    with TestClient(create_app(make_settings(), fake_resources())) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_ok_when_all_dependencies_healthy() -> None:
    app = create_app(make_settings(), fake_resources(FakeCheck("database"), FakeCheck("s3")))
    with TestClient(app) as client:
        body = client.get("/health/ready").json()
    assert body["status"] == "ok"
    assert body["version"] == "test"
    assert body["checks"]["database"]["status"] == "ok"
    assert body["checks"]["s3"]["status"] == "ok"


def test_ready_reports_degraded_but_stays_200_when_dependency_fails() -> None:
    app = create_app(
        make_settings(),
        fake_resources(FakeCheck("database", error=ConnectionError("db down")), FakeCheck("s3")),
    )
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == {
        "status": "unavailable",
        "latency_ms": body["checks"]["database"]["latency_ms"],
        "error": "ConnectionError",  # type only; never the message
    }


def test_ready_times_out_slow_dependency() -> None:
    app = create_app(
        make_settings(health_check_timeout_s=0.05),
        fake_resources(FakeCheck("database", delay_s=1.0)),
    )
    with TestClient(app) as client:
        body = client.get("/health/ready").json()
    assert body["checks"]["database"]["error"] == "Timeout"
    assert body["checks"]["database"]["latency_ms"] < 500
