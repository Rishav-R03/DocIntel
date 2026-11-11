"""End-to-end Phase 3 sync mode: HTTP -> [S3 || scripted Gemini] -> response -> Postgres.

Gemini is scripted (no network); everything else is real (Postgres, moto S3). Persistence runs
after the response, so DB assertions are made after the TestClient context exits, which runs the
lifespan shutdown and drains background work.
"""

import functools
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from docintel.api.main import create_app
from docintel.api.resources import build_resources
from docintel.extraction.pipeline import ExtractionPipeline
from docintel.settings import Settings
from tests.fakes import ScriptedGemini, call_error, cv, invoice_payload, pipeline_with, response
from tests.helpers import make_pdf
from tests.integration.conftest import Aws, app_settings, fetch

pytestmark = pytest.mark.integration


def _app(settings: Settings, pipeline: ExtractionPipeline | None):  # type: ignore[no-untyped-def]
    return create_app(settings, functools.partial(build_resources, pipeline=pipeline))


def _sync(client: TestClient, *, request_id: str, schema_id: str = "invoice.v1"):  # type: ignore[no-untyped-def]
    return client.post(
        "/api/v1/extract?mode=sync",
        files={"file": ("invoice.pdf", make_pdf(2), "application/pdf")},
        data={"schema_id": schema_id},
        headers={"X-Request-Id": request_id, "X-Client-Id": "acme"},
    )


def _job(db: Settings, request_id: str) -> dict[str, Any]:
    [row] = fetch(db, "SELECT * FROM jobs WHERE request_id = $1", request_id)
    return dict(row)


def _stages(db: Settings, job_id: uuid.UUID) -> list[str]:
    rows = fetch(db, "SELECT stage FROM job_events WHERE job_id = $1 ORDER BY id", job_id)
    return [r["stage"] for r in rows]


def test_sync_extraction_returns_result_and_persists_audit_trail(
    db_settings: Settings, aws: Aws
) -> None:
    gemini = ScriptedGemini(response(invoice_payload(total_amount=cv(1200.0))))
    with TestClient(_app(app_settings(db_settings, aws), pipeline_with(gemini))) as client:
        res = _sync(client, request_id="req-sync-ok")

    # --- response contract
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "COMPLETED"
    assert body["model"] == "gemini-3.8-flash"
    assert body["data"]["invoice_number"] == {"value": "INV-2041", "confidence": 0.97, "page": 1}
    assert body["data"]["total_amount"]["value"] == "1200.00"
    assert [w["code"] for w in body["warnings"]] == ["TOTAL_MISMATCH"]
    assert body["document_validity_score"] < body["model_reported_validity"] == 0.96
    assert body["low_confidence_fields"] == ["vendor_tax_id"]
    assert body["usage"] == {
        "input_tokens": 2000,
        "output_tokens": 800,
        "thinking_tokens": 200,
        "estimated_cost_usd": body["usage"]["estimated_cost_usd"],
    }
    assert float(body["usage"]["estimated_cost_usd"]) > 0
    assert set(body["timings_ms"]) == {"upload_ms", "gemini_ms", "validation_ms", "total_ms"}
    assert res.headers["location"] == f"/api/v1/jobs/{body['job_id']}"

    # --- persisted after the response
    job = _job(db_settings, "req-sync-ok")
    assert (job["mode"], job["status"], job["attempt_count"]) == ("SYNC", "COMPLETED", 1)
    assert job["duration_ms"] is not None
    assert _stages(db_settings, job["id"]) == [
        "UPLOADED",
        "S3_STAGED",
        "GEMINI_PROCESSING",
        "VALIDATED",
        "COMPLETED",
    ]
    [result] = fetch(db_settings, "SELECT * FROM extraction_results WHERE job_id = $1", job["id"])
    assert json.loads(result["result"])["total_amount"]["value"] == "1200.00"
    assert [w["code"] for w in json.loads(result["warnings"])] == ["TOTAL_MISMATCH"]
    [audit] = fetch(db_settings, "SELECT * FROM llm_call_audit WHERE job_id = $1", job["id"])
    assert (audit["outcome"], audit["model"], audit["input_mode"]) == (
        "SUCCESS",
        "gemini-3.8-flash",
        "INLINE",
    )
    assert (audit["prompt_tokens"], audit["output_tokens"], audit["thinking_tokens"]) == (
        2000,
        800,
        200,
    )
    assert str(audit["estimated_cost_usd"]) == body["usage"]["estimated_cost_usd"]
    # --- COMPLETED: the staged source is deleted (data minimisation)
    assert aws.objects() == []


def test_provider_outage_returns_503_and_audits_every_attempt(
    db_settings: Settings, aws: Aws
) -> None:
    gemini = ScriptedGemini(*(call_error(503) for _ in range(3)))
    with TestClient(_app(app_settings(db_settings, aws), pipeline_with(gemini))) as client:
        res = _sync(client, request_id="req-sync-503")
    assert res.status_code == 503
    assert res.json()["code"] == "UPSTREAM_UNAVAILABLE"
    assert res.headers["retry-after"] == "10"
    job = _job(db_settings, "req-sync-503")
    assert (job["status"], job["error_code"], job["attempt_count"]) == (
        "FAILED",
        "UPSTREAM_UNAVAILABLE",
        3,
    )
    outcomes = fetch(
        db_settings, "SELECT outcome, http_status FROM llm_call_audit WHERE job_id = $1", job["id"]
    )
    assert [(r["outcome"], r["http_status"]) for r in outcomes] == [("RETRYABLE_ERROR", 503)] * 3
    assert _stages(db_settings, job["id"])[-1] == "FAILED"
    assert len(aws.objects()) == 1  # FAILED: source kept so the job can be re-driven


def test_database_down_still_returns_the_extraction(
    db_settings: Settings, aws: Aws, capsys: pytest.CaptureFixture[str]
) -> None:
    """NFR-05: if audit logging fails, the client still gets its result, and the audit record
    survives as a structured log line (our logging owns the root handler, so read stdout)."""
    settings = app_settings(db_settings, aws, db_port=1, db_connect_timeout_s=1)
    gemini = ScriptedGemini(response(invoice_payload()))
    with TestClient(_app(settings, pipeline_with(gemini))) as client:
        res = _sync(client, request_id="req-sync-nodb")
    assert res.status_code == 200
    assert res.json()["data"]["invoice_number"]["value"] == "INV-2041"
    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    [fallback] = [r for r in records if r.get("event") == "sync_persist_failed"]
    assert fallback["audit_fallback"] is True
    assert fallback["attempts"][0]["input_tokens"] == 2000
    assert fallback["attempts"][0]["cost_usd"] == "0.005250"
    assert "password" not in json.dumps(fallback).lower()  # no frame locals (see test_logging)


def test_storage_down_still_returns_the_extraction(db_settings: Settings, aws: Aws) -> None:
    settings = app_settings(db_settings, aws, s3_bucket="no-such-bucket")
    gemini = ScriptedGemini(response(invoice_payload()))
    with TestClient(_app(settings, pipeline_with(gemini))) as client:
        res = _sync(client, request_id="req-sync-nos3")
    assert res.status_code == 200
    job = _job(db_settings, "req-sync-nos3")
    assert job["status"] == "COMPLETED"
    assert "S3_STAGED" not in _stages(db_settings, job["id"])


def test_sync_without_gemini_configured_is_503(db_settings: Settings, aws: Aws) -> None:
    with TestClient(_app(app_settings(db_settings, aws), None)) as client:
        res = _sync(client, request_id="req-sync-nokey")
    assert res.status_code == 503
    assert res.json()["code"] == "UPSTREAM_UNAVAILABLE"


def test_rejected_upload_never_calls_the_model(db_settings: Settings, aws: Aws) -> None:
    gemini = ScriptedGemini()
    with TestClient(_app(app_settings(db_settings, aws), pipeline_with(gemini))) as client:
        res = client.post(
            "/api/v1/extract?mode=sync",
            files={"file": ("x.pdf", make_pdf(1, password="pw"), "application/pdf")},
            data={"schema_id": "invoice.v1"},
        )
    assert res.status_code == 422
    assert gemini.calls == []  # no tokens spent on documents we would reject anyway
