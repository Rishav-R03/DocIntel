"""End-to-end Phase 4: async submit -> SQS -> worker handler -> Postgres (+NOTIFY) -> SSE.

The handler runs for real against Postgres and moto S3; only Gemini is scripted.
"""

import asyncio
import json
import threading
import time
import uuid
from typing import Any

import aioboto3
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import NullPool

from docintel.api.main import create_app
from docintel.ingestion.staging import DocumentStore
from docintel.persistence.db import create_engine, create_session_factory
from docintel.settings import Settings
from docintel.worker.extraction_handler import ExtractionHandler
from docintel.worker.handler import RetryLaterError
from tests.fakes import ScriptedGemini, call_error, invoice_payload, pipeline_with, response
from tests.helpers import make_pdf
from tests.integration.conftest import Aws, app_settings, fetch

pytestmark = pytest.mark.integration


def _submit(settings: Settings, request_id: str) -> tuple[uuid.UUID, dict[str, Any]]:
    with TestClient(create_app(settings)) as client:
        res = client.post(
            "/api/v1/extract",
            files={"file": ("inv.pdf", make_pdf(2), "application/pdf")},
            data={"schema_id": "invoice.v1"},
            headers={"X-Request-Id": request_id},
        )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["stream_url"] == f"/api/v1/jobs/{body['job_id']}/stream"
    return uuid.UUID(body["job_id"]), body


def _message(aws: Aws, job_id: uuid.UUID) -> dict[str, Any]:
    [msg] = [m for m in aws.messages() if json.loads(m["Body"])["job_id"] == str(job_id)]
    return msg


def _handle(
    settings: Settings,
    aws: Aws,
    message: dict[str, Any],
    gemini: ScriptedGemini | None,
    *,
    receive_count: int = 1,
) -> None:
    async def go() -> None:
        engine = create_engine(settings, role="test", poolclass=NullPool)
        try:
            session = aioboto3.Session(region_name="us-east-1")
            async with session.client("s3", endpoint_url=aws.endpoint) as s3:
                handler = ExtractionHandler(
                    settings=settings,
                    session_factory=create_session_factory(engine),
                    store=DocumentStore(s3, bucket=aws.bucket, kms_key_id=None),
                    pipeline=pipeline_with(gemini) if gemini else None,
                )
                attrs = {"ApproximateReceiveCount": str(receive_count)}
                await handler.handle({**message, "Attributes": attrs})
        finally:
            await engine.dispose()

    asyncio.run(go())


def _job(db: Settings, job_id: uuid.UUID) -> dict[str, Any]:
    [row] = fetch(db, "SELECT * FROM jobs WHERE id = $1", job_id)
    return dict(row)


def _stages(db: Settings, job_id: uuid.UUID) -> list[str]:
    rows = fetch(db, "SELECT stage FROM job_events WHERE job_id = $1 ORDER BY id", job_id)
    return [r["stage"] for r in rows]


def _count(db: Settings, table: str, job_id: uuid.UUID) -> int:
    return len(fetch(db, f"SELECT 1 FROM {table} WHERE job_id = $1", job_id))  # noqa: S608


# ----------------------------------------------------------------------------- happy path
def test_worker_completes_job_and_get_returns_result(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws)
    job_id, _ = _submit(settings, "req-async-ok")
    _handle(settings, aws, _message(aws, job_id), ScriptedGemini(response(invoice_payload())))

    job = _job(db_settings, job_id)
    assert (job["status"], job["attempt_count"]) == ("COMPLETED", 1)
    assert job["duration_ms"] is not None
    assert _stages(db_settings, job_id) == [
        "UPLOADED",
        "S3_STAGED",
        "GEMINI_PROCESSING",
        "VALIDATED",
        "COMPLETED",
    ]
    assert _count(db_settings, "extraction_results", job_id) == 1
    assert _count(db_settings, "llm_call_audit", job_id) == 1
    assert aws.objects() == []  # source deleted on COMPLETED

    with TestClient(create_app(settings)) as client:
        body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert body["status"] == "COMPLETED"
    result = body["result"]
    assert result["data"]["total_amount"]["value"] == "1180.00"
    assert result["model"] == "gemini-3.8-flash"
    assert result["usage"]["input_tokens"] == 2000
    assert float(result["usage"]["estimated_cost_usd"]) > 0


def test_duplicate_delivery_is_processed_once(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws)
    job_id, _ = _submit(settings, "req-async-dup")
    message = _message(aws, job_id)
    _handle(settings, aws, message, ScriptedGemini(response(invoice_payload())))
    gemini = ScriptedGemini()  # would raise if called
    _handle(settings, aws, message, gemini)  # same message again: nothing to claim
    assert gemini.calls == []
    assert _count(db_settings, "extraction_results", job_id) == 1


# ----------------------------------------------------------------------------- failures
def test_transient_failure_requeues_then_fails_on_last_receive(
    db_settings: Settings, aws: Aws
) -> None:
    settings = app_settings(db_settings, aws, worker_max_receives=3)
    job_id, _ = _submit(settings, "req-async-503")
    message = _message(aws, job_id)

    with pytest.raises(RetryLaterError) as retry:
        _handle(settings, aws, message, ScriptedGemini(*(call_error(503) for _ in range(3))))
    assert retry.value.delay_s == 30
    assert _job(db_settings, job_id)["status"] == "STAGED"  # back in the queue
    assert _stages(db_settings, job_id)[-1] == "RETRY_SCHEDULED"

    _handle(
        settings,
        aws,
        message,
        ScriptedGemini(*(call_error(503) for _ in range(3))),
        receive_count=3,
    )  # last allowed receive: no raise, job FAILED
    job = _job(db_settings, job_id)
    assert (job["status"], job["error_code"], job["attempt_count"]) == (
        "FAILED",
        "UPSTREAM_UNAVAILABLE",
        2,
    )
    assert _stages(db_settings, job_id)[-1] == "FAILED"
    assert _count(db_settings, "llm_call_audit", job_id) == 6  # every attempt of both runs
    assert len(aws.objects()) == 1  # FAILED keeps the source for a re-drive


def test_bad_model_output_fails_permanently(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws)
    job_id, _ = _submit(settings, "req-async-bad")
    _handle(
        settings, aws, _message(aws, job_id), ScriptedGemini(response("not json"), response("{}"))
    )  # no RetryLater: ack
    job = _job(db_settings, job_id)
    assert (job["status"], job["error_code"]) == ("FAILED", "SCHEMA_VALIDATION_FAILED")


def test_redelivery_reclaims_job_orphaned_by_a_dead_worker(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws)
    job_id, _ = _submit(settings, "req-async-crash")
    message = _message(aws, job_id)
    fetch(db_settings, "UPDATE jobs SET status = 'PROCESSING' WHERE id = $1", job_id)  # crashed

    first = ScriptedGemini()
    _handle(settings, aws, message, first, receive_count=1)  # not a redelivery: hands off
    assert first.calls == []
    _handle(settings, aws, message, ScriptedGemini(response(invoice_payload())), receive_count=2)
    assert _job(db_settings, job_id)["status"] == "COMPLETED"


def test_missing_source_document_fails_permanently(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws)
    job_id, _ = _submit(settings, "req-async-nosrc")
    for key in aws.objects():
        aws.s3.delete_object(Bucket=aws.bucket, Key=key)
    _handle(settings, aws, _message(aws, job_id), ScriptedGemini())
    job = _job(db_settings, job_id)
    assert (job["status"], job["error_code"]) == ("FAILED", "EXTRACTION_FAILED")


def test_unparseable_message_raises_so_it_reaches_the_dlq(db_settings: Settings, aws: Aws) -> None:
    with pytest.raises(ValueError, match="validation error"):
        _handle(app_settings(db_settings, aws), aws, {"Body": '{"oops": 1}'}, ScriptedGemini())


# ----------------------------------------------------------------------------- SSE
def _read_events(lines: Any, until: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    name, event_id = "", ""
    for line in lines:
        if line.startswith("id: "):
            event_id = line[4:]
        elif line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            data["_id"] = event_id
            events.append((name, data))
            if name == until or name in ("completed", "failed"):
                return events
    return events


def test_stream_replays_finished_job_and_resumes_from_last_event_id(
    db_settings: Settings, aws: Aws
) -> None:
    settings = app_settings(db_settings, aws)
    job_id, body = _submit(settings, "req-sse-replay")
    _handle(settings, aws, _message(aws, job_id), ScriptedGemini(response(invoice_payload())))

    with TestClient(create_app(settings)) as client:
        with client.stream("GET", body["stream_url"]) as res:
            assert res.headers["content-type"].startswith("text/event-stream")
            assert res.headers["cache-control"] == "no-cache"
            events = _read_events(res.iter_lines(), until="completed")
        assert [d["stage"] for _, d in events] == [
            "UPLOADED",
            "S3_STAGED",
            "GEMINI_PROCESSING",
            "VALIDATED",
            "COMPLETED",
        ]
        assert events[-1][0] == "completed"
        assert events[-1][1]["result_url"] == f"/api/v1/jobs/{job_id}"

        third_id = events[2][1]["_id"]
        with client.stream("GET", body["stream_url"], headers={"Last-Event-ID": third_id}) as res:
            resumed = _read_events(res.iter_lines(), until="completed")
        assert [d["stage"] for _, d in resumed] == ["VALIDATED", "COMPLETED"]


def test_stream_delivers_live_events_via_notify(db_settings: Settings, aws: Aws) -> None:
    """Heartbeat is 30 s, so finishing in < 10 s proves the NOTIFY wake-up path, not polling."""
    settings = app_settings(db_settings, aws, sse_heartbeat_s=30)
    job_id, body = _submit(settings, "req-sse-live")
    message = _message(aws, job_id)

    with TestClient(create_app(settings)) as client:
        for _ in range(50):  # the LISTEN connection is established in the background
            if client.get("/health/ready").json()["checks"]["events"]["status"] == "ok":
                break
            time.sleep(0.1)
        with client.stream("GET", body["stream_url"]) as res:
            lines = res.iter_lines()
            replayed = _read_events(lines, until="stage")  # first replayed event
            assert replayed[0][1]["stage"] == "UPLOADED"
            start = time.monotonic()
            worker = threading.Thread(
                target=_handle,
                args=(settings, aws, message, ScriptedGemini(response(invoice_payload()))),
            )
            worker.start()
            live = _read_events(lines, until="completed")
            worker.join(timeout=10)
    assert time.monotonic() - start < 10
    assert [d["stage"] for _, d in live][-3:] == ["GEMINI_PROCESSING", "VALIDATED", "COMPLETED"]
