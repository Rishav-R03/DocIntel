# One image, two entrypoints (ADR-01): api (default CMD) and worker (`python -m docintel.worker`).
# No `# syntax=` directive on purpose: it makes every build fetch the Dockerfile frontend from Docker
# Hub, which fails on flaky DNS. Docker's built-in frontend supports everything used here
# (RUN --mount=type=cache, ADD --chmod).

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- base: python + uv
FROM ${PYTHON_IMAGE} AS base
COPY --from=uv /uv /usr/local/bin/uv
# Lower these on slow/flaky networks: --build-arg UV_CONCURRENT_DOWNLOADS=4
ARG UV_CONCURRENT_DOWNLOADS=50
ARG UV_HTTP_TIMEOUT=120
ENV UV_COMPILE_BYTECODE=1 \
    UV_CONCURRENT_DOWNLOADS=${UV_CONCURRENT_DOWNLOADS} \
    UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT} \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# ---------------------------------------------------------------- builder: prod deps only
FROM base AS builder
COPY pyproject.toml uv.lock ./
# Dependencies first (cached layer), then the project itself as a non-editable install
# so the runtime stage only needs /app/.venv, not the source tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- dev: tests, lint, local tools
FROM base AS dev
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG GIT_SHA=unknown
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
# RDS CA bundle so DB connections can run with ssl verify-full.
ADD --chmod=644 https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
    /etc/ssl/certs/rds-global-bundle.pem
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCINTEL_VERSION=${GIT_SHA}
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "--factory", "docintel.api.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-access-log", "--timeout-graceful-shutdown", "25"]
