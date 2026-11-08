"""aioboto3 session/client helpers shared by the api and worker."""

from typing import Any

import aioboto3
from botocore.config import Config

from docintel.settings import Settings

# Standard retry mode = exponential backoff + jitter for throttling/5xx inside botocore.
# read_timeout must stay above the SQS long-poll wait (20 s).
_BOTO_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 3},
    connect_timeout=2,
    read_timeout=30,
)


def create_session(settings: Settings) -> aioboto3.Session:
    return aioboto3.Session(region_name=settings.aws_region)


def client_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"config": _BOTO_CONFIG}
    if settings.aws_endpoint_url:
        kwargs["endpoint_url"] = settings.aws_endpoint_url
    return kwargs
