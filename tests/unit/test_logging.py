import json

import pytest
import structlog

from docintel.logging import configure_logging
from tests.conftest import make_settings


def _connect(password: str) -> None:
    raise ConnectionRefusedError("connect call failed")


@pytest.mark.parametrize("log_json", [True, False])
def test_exception_logs_never_include_frame_locals(
    log_json: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: structlog's default dict_tracebacks dumped frame locals, which leaked the DB
    password from asyncpg's connect frame into the logs."""
    configure_logging(make_settings(log_json=log_json))
    log = structlog.get_logger("test")
    secret = "s3cr3t-" + "hunter2"  # built at runtime: never appears in source lines
    try:
        _connect(password=secret)
    except ConnectionRefusedError:
        log.exception("db_failed")
    out = capsys.readouterr().out
    assert "s3cr3t-hunter2" not in out
    assert "_connect" in out  # the stack itself is still there to debug with
    if log_json:
        [frame] = [
            f
            for f in json.loads(out.strip().splitlines()[-1])["exception"][0]["frames"]
            if f["name"] == "_connect"
        ]
        assert "locals" not in frame


def test_http_client_debug_noise_is_suppressed() -> None:
    import logging

    configure_logging(make_settings(log_level="DEBUG"))
    for name in ("httpcore", "httpx", "httpx2", "google_genai"):
        assert logging.getLogger(name).level == logging.WARNING, name
