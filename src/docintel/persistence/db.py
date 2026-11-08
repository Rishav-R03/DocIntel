"""Async SQLAlchemy engine factory (asyncpg driver)."""

import ssl
from typing import Any

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from docintel.settings import Settings


def build_url(settings: Settings) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
    )


def _ssl_arg(settings: Settings) -> ssl.SSLContext | str | bool:
    match settings.db_ssl_mode:
        case "disable":
            return False
        case "require":  # encrypted, server certificate not verified
            return "require"
        case "verify-full":  # encrypted + CA chain + hostname verified (RDS CA bundle in the image)
            cafile = str(settings.db_ssl_root_cert) if settings.db_ssl_root_cert else None
            return ssl.create_default_context(cafile=cafile)


def connect_args(settings: Settings, role: str) -> dict[str, Any]:
    return {
        "ssl": _ssl_arg(settings),
        "timeout": settings.db_connect_timeout_s,
        "server_settings": {"application_name": f"{settings.service_name}-{role}"},
    }


def create_engine(settings: Settings, *, role: str, **engine_kwargs: Any) -> AsyncEngine:
    """``role`` (api/worker/migrate) shows up in ``pg_stat_activity.application_name``."""
    if "poolclass" not in engine_kwargs:
        engine_kwargs.setdefault("pool_size", settings.db_pool_size)
        engine_kwargs.setdefault("max_overflow", settings.db_max_overflow)
        engine_kwargs.setdefault("pool_pre_ping", True)
        engine_kwargs.setdefault("pool_recycle", 1800)
    return create_async_engine(
        build_url(settings), connect_args=connect_args(settings, role), **engine_kwargs
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay readable after commit without an implicit (and, under
    # asyncio, illegal) lazy refresh.
    return async_sessionmaker(engine, expire_on_commit=False)
