"""Job lifecycle (implementation.md §4.5): statuses, legal transitions and progress stages."""

from collections.abc import Iterable
from enum import StrEnum


class JobStatus(StrEnum):
    RECEIVED = "RECEIVED"
    STAGED = "STAGED"
    PROCESSING = "PROCESSING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class JobMode(StrEnum):
    SYNC = "SYNC"
    ASYNC = "ASYNC"


class EventStage(StrEnum):
    """Progress stages streamed to clients (PRD wording), persisted in ``job_events``."""

    UPLOADED = "UPLOADED"
    S3_STAGED = "S3_STAGED"
    GEMINI_PROCESSING = "GEMINI_PROCESSING"
    VALIDATED = "VALIDATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"  # async: transient failure, job back in the queue


ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.RECEIVED: frozenset({JobStatus.STAGED, JobStatus.FAILED}),
    JobStatus.STAGED: frozenset({JobStatus.PROCESSING, JobStatus.FAILED}),
    # PROCESSING -> STAGED: retryable failure, message goes back to the queue.
    # PROCESSING -> PROCESSING: a redelivered message reclaims a job whose worker died mid-run.
    JobStatus.PROCESSING: frozenset(
        {JobStatus.VALIDATING, JobStatus.STAGED, JobStatus.PROCESSING, JobStatus.FAILED}
    ),
    JobStatus.VALIDATING: frozenset({JobStatus.COMPLETED, JobStatus.FAILED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


def check_transition(from_states: Iterable[JobStatus], to_state: JobStatus) -> None:
    """Guard against programming errors: every source state must allow ``to_state``."""
    illegal = [s for s in from_states if to_state not in ALLOWED_TRANSITIONS[s]]
    if illegal:
        raise ValueError(f"illegal job transition {illegal} -> {to_state}")
