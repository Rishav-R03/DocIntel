"""Typed application configuration loaded from environment variables (prefix ``DOCINTEL_``).

In AWS, plain values come from the ECS task definition ``environment`` block and secrets
(DB credentials, Gemini key) from the ``secrets`` block, which ECS resolves from Secrets
Manager at task start. Nothing secret is ever read from a file in the image.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "dev", "staging", "prod"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCINTEL_", extra="ignore", frozen=True)

    # ---- service identity
    env: Environment = "local"
    service_name: str = "docintel"
    version: str = "0.0.0-dev"  # overwritten with the git SHA at image build time
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True

    # ---- database (credentials injected separately so they can come from the RDS-managed secret)
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "docintel"
    db_user: str = "docintel"
    db_password: SecretStr
    db_ssl_mode: Literal["disable", "require", "verify-full"] = "verify-full"
    db_ssl_root_cert: Path | None = None
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_connect_timeout_s: float = Field(default=5.0, gt=0)

    # ---- AWS
    aws_region: str = "us-east-1"
    aws_endpoint_url: str | None = None  # moto/LocalStack only; rejected outside `local`
    s3_bucket: str
    s3_kms_key_id: str | None = None  # required outside `local`: every staged object is SSE-KMS
    sqs_jobs_url: str

    # ---- ingestion limits (FR-03)
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, gt=0)
    max_pages: int = Field(default=50, ge=1)
    max_image_pixels: int = Field(default=50_000_000, gt=0)  # A4 @ 600 dpi is ~35 MP
    upload_spool_bytes: int = Field(default=1024 * 1024, gt=0)  # beyond this, spool to /tmp

    # ---- extraction (Gemini). The PRD named gemini-2.5-flash; it is closed to new projects and
    # retired on Vertex AI as of 2026-10, so the default is its GA successor (journal, Phase 3).
    gemini_api_key: SecretStr | None = None  # required outside `local`
    gemini_model: str = "gemini-3.8-flash"
    # On retryable failures (503 / 429 / timeout) after `gemini_fallback_after_attempts` tries,
    # the request moves to this model. Empty string disables failover.
    gemini_fallback_model: str | None = "gemini-3.5-flash-lite"
    gemini_fallback_after_attempts: int = Field(default=2, ge=1, le=10)
    gemini_timeout_s: float = Field(default=25.0, gt=0)
    gemini_inline_max_bytes: int = Field(default=14 * 1024 * 1024, gt=0)  # base64 adds ~33 %
    retry_max_attempts_sync: int = Field(default=3, ge=1, le=10)
    sync_deadline_s: float = Field(default=30.0, gt=0)
    sync_max_concurrency: int = Field(default=32, ge=1)
    confidence_threshold: float = Field(default=0.7, ge=0, le=1)

    # ---- health
    health_check_timeout_s: float = Field(default=1.0, gt=0)

    # ---- worker
    worker_concurrency: int = Field(default=16, ge=1, le=256)
    worker_drain_timeout_s: float = Field(default=25.0, gt=0)
    sqs_wait_time_s: int = Field(default=20, ge=0, le=20)
    sqs_visibility_timeout_s: int = Field(default=180, ge=30, le=43_200)  # = queue setting (TF)
    worker_job_deadline_s: float = Field(default=120.0, gt=0)
    worker_max_receives: int = Field(default=3, ge=1)  # = DLQ maxReceiveCount (TF / bootstrap)
    retry_max_attempts_async: int = Field(default=5, ge=1, le=10)

    # ---- live progress (SSE)
    sse_heartbeat_s: float = Field(default=15.0, gt=0)
    sse_max_duration_s: float = Field(default=600.0, gt=0)

    @model_validator(mode="after")
    def _enforce_cloud_safety(self) -> "Settings":
        if self.env == "local":
            return self
        if self.db_ssl_mode == "disable":
            raise ValueError("DOCINTEL_DB_SSL_MODE=disable is only allowed when DOCINTEL_ENV=local")
        if self.aws_endpoint_url:
            raise ValueError("DOCINTEL_AWS_ENDPOINT_URL is only allowed when DOCINTEL_ENV=local")
        if not self.s3_kms_key_id:
            raise ValueError("DOCINTEL_S3_KMS_KEY_ID is required when DOCINTEL_ENV is not local")
        if self.gemini_api_key is None:
            raise ValueError("DOCINTEL_GEMINI_API_KEY is required when DOCINTEL_ENV is not local")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # required fields come from the environment
