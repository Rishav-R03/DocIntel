# DocIntel AI Engine

Headless microservice that turns PDFs and images into schema-validated JSON using Gemini.

- Product requirements: [product.md](product.md)
- Implementation plan (SDLC, design, phases): [implementation.md](implementation.md)
- Phase-by-phase engineering journal (decisions and trade-offs): [doc/engineering-journal.md](doc/engineering-journal.md)

**Status:** Phases 1–2 are complete. Documents can be uploaded (`POST /api/v1/extract`), validated, staged encrypted in S3, recorded as jobs and queued for the worker; `GET /api/v1/jobs/{id}` reports status. AI extraction arrives in Phase 3.

```bash
curl -F "file=@invoice.pdf" -F "schema_id=invoice.v1" localhost:8000/api/v1/extract   # 202 + job_id
curl localhost:8000/api/v1/jobs/<job_id>
```

## Quick start (Docker only)

You only need Docker. Python, uv and Terraform all run in containers.

```bash
docker compose up --build -d            # postgres, moto (S3/SQS), migrate, api, worker
curl localhost:8000/health/ready        # {"status":"ok", ... "checks":{"database":..,"s3":..}}
open http://localhost:8000/docs         # OpenAPI UI (disabled in prod)

docker compose run --rm test            # full suite: unit + Postgres integration + contract
docker compose run --rm test sh -c "ruff check . && ruff format --check . && mypy"
docker compose down -v
```

## Native development (optional)

```bash
pip install uv                          # or: pipx install uv
uv sync                                 # creates .venv with Python 3.12 + dev deps
cp .env.example .env                    # points at the compose postgres/moto ports
uv run uvicorn --factory docintel.api.main:create_app --reload
uv run python -m docintel.worker
uv run pre-commit install
```

## Layout

| Path | What |
|---|---|
| `src/docintel/api/` | FastAPI app factory, middleware, problem+json errors, routes |
| `src/docintel/worker/` | SQS consumer (`python -m docintel.worker`) |
| `src/docintel/persistence/` | SQLAlchemy async engine, ORM models |
| `src/docintel/domain/` | Job states, typed error codes |
| `migrations/` | Alembic (hand-written; drift-checked against models in tests) |
| `infra/` | Terraform modules + `envs/dev` ([infra/README.md](infra/README.md)) |
| `scripts/deploy_ecs.sh` | CI deploy: register task defs → migrate → roll services |
| `tests/` | `unit/`, `integration/` (real Postgres), `contract/` (OpenAPI snapshot) |

## Configuration

All settings are environment variables prefixed `DOCINTEL_`. See [src/docintel/settings.py](src/docintel/settings.py) for the full list. Secrets are never baked into the image; in AWS, ECS injects them from Secrets Manager.
