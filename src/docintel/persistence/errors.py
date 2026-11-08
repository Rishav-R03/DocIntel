"""Map transient database failures to a retryable 503 instead of an opaque 500."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import InterfaceError, OperationalError

from docintel.domain.errors import DependencyUnavailableError

# Connectivity/availability failures only. IntegrityError, DataError, ProgrammingError are bugs or
# conflicts and must surface as such, not be disguised as "try again later".
_TRANSIENT_DB_ERRORS = (OperationalError, InterfaceError, OSError, TimeoutError)


@contextmanager
def database_errors_as_unavailable() -> Iterator[None]:
    try:
        yield
    except _TRANSIENT_DB_ERRORS as exc:
        raise DependencyUnavailableError("The database is temporarily unavailable.") from exc
