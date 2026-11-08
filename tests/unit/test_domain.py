import time
import uuid

import pytest

from docintel.domain.ids import uuid7
from docintel.domain.job import TERMINAL_STATUSES, JobStatus, check_transition
from docintel.domain.messages import JobSubmitted
from docintel.ingestion.staging import source_key


def test_uuid7_has_version_and_variant_and_is_time_ordered() -> None:
    first = uuid7()
    time.sleep(0.002)
    second = uuid7()
    assert first.version == 7
    assert first.variant == uuid.RFC_4122
    assert first < second  # ms timestamp prefix -> sortable across milliseconds
    assert len({uuid7() for _ in range(1000)}) == 1000


def test_uuid7_timestamp_prefix_is_current_unix_ms() -> None:
    embedded_ms = uuid7().int >> 80
    assert abs(embedded_ms - time.time_ns() // 1_000_000) < 1000


@pytest.mark.parametrize(
    ("from_states", "to_state"),
    [
        ([JobStatus.STAGED], JobStatus.PROCESSING),
        ([JobStatus.PROCESSING], JobStatus.STAGED),  # retryable failure
        ([JobStatus.RECEIVED, JobStatus.STAGED], JobStatus.FAILED),
    ],
)
def test_allowed_transitions(from_states: list[JobStatus], to_state: JobStatus) -> None:
    check_transition(from_states, to_state)


@pytest.mark.parametrize(
    ("from_states", "to_state"),
    [
        ([JobStatus.COMPLETED], JobStatus.FAILED),  # terminal is terminal
        ([JobStatus.STAGED], JobStatus.COMPLETED),  # no skipping validation
        ([JobStatus.STAGED, JobStatus.FAILED], JobStatus.PROCESSING),
    ],
)
def test_illegal_transitions_raise(from_states: list[JobStatus], to_state: JobStatus) -> None:
    with pytest.raises(ValueError, match="illegal job transition"):
        check_transition(from_states, to_state)


def test_terminal_statuses() -> None:
    assert {JobStatus.COMPLETED, JobStatus.FAILED} == TERMINAL_STATUSES


def test_job_message_is_versioned_and_strict() -> None:
    job_id = uuid7()
    message = JobSubmitted(job_id=job_id, schema_id="invoice.v1")
    assert JobSubmitted.model_validate_json(message.model_dump_json()) == message
    assert '"v":1' in message.model_dump_json()
    with pytest.raises(ValueError, match="Extra inputs"):
        JobSubmitted.model_validate({"job_id": str(job_id), "schema_id": "x", "surprise": 1})


def test_source_key_never_contains_client_filename() -> None:
    from datetime import UTC, datetime

    job_id = uuid7()
    key = source_key(job_id, "image/jpeg", datetime(2026, 10, 29, tzinfo=UTC))
    assert key == f"staging/2026/10/29/{job_id}/source.jpg"
