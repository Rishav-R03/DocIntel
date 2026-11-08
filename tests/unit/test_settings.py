import ssl

import pytest
from pydantic import ValidationError

from docintel.persistence.db import build_url, connect_args
from tests.conftest import make_settings


def test_local_allows_plaintext_db_and_endpoint_override() -> None:
    settings = make_settings(db_ssl_mode="disable", aws_endpoint_url="http://moto:5000")
    assert settings.aws_endpoint_url == "http://moto:5000"


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_cloud_envs_reject_plaintext_db(env: str) -> None:
    with pytest.raises(ValidationError, match="DB_SSL_MODE=disable"):
        make_settings(env=env, db_ssl_mode="disable")


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_cloud_envs_reject_endpoint_override(env: str) -> None:
    with pytest.raises(ValidationError, match="AWS_ENDPOINT_URL"):
        make_settings(env=env, db_ssl_mode="verify-full", aws_endpoint_url="http://moto:5000")


def test_cloud_envs_require_kms_key() -> None:
    with pytest.raises(ValidationError, match="S3_KMS_KEY_ID"):
        make_settings(env="dev", db_ssl_mode="verify-full")
    make_settings(env="dev", db_ssl_mode="verify-full", s3_kms_key_id="arn:aws:kms:x")


def test_secrets_are_masked_in_repr() -> None:
    settings = make_settings(db_password="hunter2")
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(build_url(settings))


def test_verify_full_builds_hostname_checking_ssl_context() -> None:
    ssl_arg = connect_args(make_settings(db_ssl_mode="verify-full"), role="api")["ssl"]
    assert isinstance(ssl_arg, ssl.SSLContext)
    assert ssl_arg.check_hostname is True
    assert ssl_arg.verify_mode == ssl.CERT_REQUIRED


def test_application_name_carries_role() -> None:
    args = connect_args(make_settings(), role="worker")
    assert args["server_settings"]["application_name"] == "docintel-worker"
