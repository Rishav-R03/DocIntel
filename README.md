# DocIntel AI Engine

Headless microservice that turns PDFs and images (invoices, financial statements, contracts, scans) into schema-validated JSON with field-level confidence scores, using Gemini.

- Product requirements: [product.md](product.md)
- Implementation plan (SDLC, design, phases): [implementation.md](implementation.md)
- Phase-by-phase engineering journal (decisions and trade-offs): [doc/engineering-journal.md](doc/engineering-journal.md)

**Status:**

| Phase | State |
|---|---|
| 1. Foundation | ✅ Complete |
| 2. Ingestion | ✅ Complete |
| 3. Extraction engine + sync API | 🟡 Built and tested; live Gemini verification pending |

Today you can upload a document and either:
- get the extracted JSON back immediately (`mode=sync`, which calls Gemini), or
- queue it as a job (`mode=async`). The worker that processes async jobs arrives in Phase 4.

---

## 1. Prerequisites

| Tool | Why | Check |
|---|---|---|
| **Docker Desktop** (with Compose v2.24+) | Runs everything: app, Postgres, S3/SQS emulator, tests | `docker compose version` |
| **Git** | Clone the repo | `git --version` |
| **curl** | Try the API (Windows: use `curl.exe`, not the PowerShell alias) | `curl.exe --version` |
| A **Gemini API key** *(optional)* | Only needed for `mode=sync` extraction. Get one from [Google AI Studio](https://aistudio.google.com/) | |

You do **not** need Python, uv, Postgres, Terraform or an AWS account locally. All of them run in containers.

## 2. One-time setup

```bash
git clone https://github.com/Rishav-R03/DocIntel.git
cd DocIntel
```

**Configure secrets (optional, for AI extraction).** Create a file named `.env` in the repo root containing one line:

```dotenv
DOCINTEL_GEMINI_API_KEY=your-key-here
```

> ⚠️ Put the key in **`.env`**, which is git-ignored, **never in `.env.example`**, which is committed. Check with `git status`: `.env` must not appear. Without a key, everything works except `mode=sync`, which returns `503 UPSTREAM_UNAVAILABLE`.

**Build the images** (first build takes a few minutes):

```bash
docker compose build
docker compose --profile test build test     # dev image used by tests and live checks
```

If builds time out on a slow or flaky connection, reduce parallel downloads:

```bash
docker compose build --build-arg UV_CONCURRENT_DOWNLOADS=4 --build-arg UV_HTTP_TIMEOUT=600
```

## 3. Run the application

```bash
docker compose up -d
```

This starts these services in order:

| Service | What it does | Host port |
|---|---|---|
| `postgres` | PostgreSQL 16 | `5433` (avoids clashing with a local Postgres on 5432; change with `DOCINTEL_HOST_PG_PORT`) |
| `moto` | Local S3 + SQS emulator | `5000` |
| `bootstrap` | Creates the local bucket and queues, then exits | — |
| `migrate` | Runs database migrations (`alembic upgrade head`), then exits | — |
| `api` | FastAPI service | **`8000`** |
| `worker` | SQS consumer | — |

Check that it's up:

```bash
docker compose ps -a                               # bootstrap/migrate "exited (0)", the rest "running"
curl.exe http://localhost:8000/health/ready        # {"status":"ok", ... "checks":{"database":...,"s3":...}}
```

Then open:
- **http://localhost:8000/**, the **demo UI**: drag in a document, pick a schema, choose Sync or Async, and see the extracted fields with confidence bars, validation warnings, token usage, cost and the raw JSON.
- **http://localhost:8000/docs**, the interactive API docs.

The demo UI is plain HTML/CSS/JS with vendored Bootstrap 5.3.3 (no build step, no CDN), lives in [src/docintel/web/](src/docintel/web/), and is only served when `DOCINTEL_ENV` is `local` or `dev`. The product itself is a headless API.

## 4. Try it

The examples use `curl.exe`. On macOS/Linux, plain `curl` works the same. Supported document types are PDF, PNG and JPEG, up to 25 MB and 50 pages. The schema is chosen with `schema_id`:

| `schema_id` | For |
|---|---|
| `invoice.v1` | Invoices and bills |
| `financial_statement.v1` | Balance sheets, income and cash-flow statements |
| `contract.v1` | Contracts |
| `generic_document.v1` | Anything else |

**Synchronous extraction** returns the result in the response (needs the Gemini key):

```bash
curl.exe -X POST "http://localhost:8000/api/v1/extract?mode=sync" \
  -F "file=@invoice.pdf" -F "schema_id=invoice.v1"
```

The response contains:
- `data`: every field as `{value, confidence, page}`;
- `document_validity_score` and `warnings`, e.g. `TOTAL_MISMATCH` when the totals don't add up;
- `low_confidence_fields`;
- `usage`: token counts and estimated cost in USD;
- `timings_ms`.

**Asynchronous submission** returns `202 Accepted` with a job id:

```bash
curl.exe -X POST "http://localhost:8000/api/v1/extract" -F "file=@invoice.pdf" -F "schema_id=invoice.v1"
curl.exe http://localhost:8000/api/v1/jobs/<job_id>
```

Optional request headers:
- `X-Request-Id` is echoed back on the response and appears in every log line.
- `X-Client-Id` scopes job visibility: a job submitted with one client id is invisible to others.

Errors are always [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json` with a stable `code` field, for example `UNSUPPORTED_MEDIA_TYPE`, `PAYLOAD_TOO_LARGE`, `PDF_ENCRYPTED` or `UPSTREAM_UNAVAILABLE`.

> **Windows PowerShell note:** use `curl.exe` explicitly, and put each command on one line or replace the trailing `\` with a backtick `` ` ``.

**Logs:**

```bash
docker compose logs -f api worker
```

## 5. Run the tests

```bash
docker compose up -d postgres moto                 # test dependencies
docker compose run --rm test                       # full suite: unit + Postgres/moto integration + contract
docker compose run --rm test sh -c "ruff check . && ruff format --check . && mypy"   # lint + types
```

The tests never call Gemini: they use a scripted fake. They also never see your real `.env` secrets.

**Live check against the real Gemini API** (opt-in; costs about $0.01 per run; needs the key in `.env`):

```bash
docker compose run --rm live python scripts/live_smoke.py --runs 3
```

It renders a synthetic invoice with known correct values and reports field accuracy, validity score, latency, tokens and cost for each run.

## 6. Stop and reset

```bash
docker compose down        # stop; keeps the Postgres data volume
docker compose down -v     # stop and delete all local data (fresh database next time)
```

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `failed to connect to the docker API ... dockerDesktopLinuxEngine` | Docker Desktop isn't running. Start it and wait until it reports "running". |
| `Bind for 0.0.0.0:5432 failed: port is already allocated` | Another Postgres uses the port. This stack defaults to 5433; set `DOCINTEL_HOST_PG_PORT=5434` (or similar) if 5433 is taken too. |
| `Bind for 0.0.0.0:8000` / `:5000` failed | Another app uses the port. Stop it, or change the left-hand side of `ports:` in `docker-compose.yml`. |
| Build fails with `Failed to download ... network timeout` | Slow network: rebuild with `--build-arg UV_CONCURRENT_DOWNLOADS=4 --build-arg UV_HTTP_TIMEOUT=600`. |
| `mode=sync` returns `503 UPSTREAM_UNAVAILABLE` | No key configured, **or** Gemini is overloaded ("high demand"; usually temporary). Retry later or use `mode=async`. |
| `mode=sync` returns `504 EXTRACTION_TIMEOUT` | Gemini didn't answer within the 30 s deadline (`DOCINTEL_SYNC_DEADLINE_S`). |
| Logs show `429 ... limit: 20 requests per day on Free Tier` | Free-tier daily quota used up (each retry counts). Wait for the reset shown in the message, or enable billing on the Google AI Studio project. |
| Edited `.env` but nothing changed | Containers read `.env` only when created: `docker compose up -d --force-recreate api`. |
| `gemini_failover` in the logs | The primary model was unavailable, so the request moved to `DOCINTEL_GEMINI_FALLBACK_MODEL` (default `gemini-3.5-flash-lite`; set it to an empty value to disable). |
| `live` service: `pull access denied for docintel-dev` | Build the dev image first: `docker compose --profile test build test`. |
| Images vanished after a Docker Desktop update | Rebuild with the two commands in §2. Your `.env` and the code are unaffected. |
| `curl.exe: (26) Failed to open/read local data` in Git Bash | Git Bash path conversion. Run from the file's folder with a relative path, or set `MSYS_NO_PATHCONV=1`. |
| Shell scripts fail with `\r: command not found` | CRLF line endings. The repo's `.gitattributes` forces LF; re-clone or run `git add --renormalize .`. |

---

## Native development (optional)

To run the Python code directly on your machine, against the dockerised Postgres and moto:

```bash
pip install uv                        # or: pipx install uv
uv sync                               # .venv with Python 3.12 + dev dependencies
cp .env.example .env                  # local settings (DB on localhost:5433, moto on localhost:5000)
#   ...then add your DOCINTEL_GEMINI_API_KEY to .env
docker compose up -d postgres moto bootstrap
uv run alembic upgrade head
uv run uvicorn --factory docintel.api.main:create_app --reload
uv run python -m docintel.worker      # in a second terminal
uv run pre-commit install             # git hooks: ruff, mypy, gitleaks
```

Inside compose, values set in `docker-compose.yml` take precedence over `.env`, so the same `.env` works for both setups.

## Layout

| Path | What |
|---|---|
| `src/docintel/api/` | FastAPI app factory, middleware, problem+json errors, routes |
| `src/docintel/ingestion/` | Streaming upload parser, document inspection, S3 staging, submission service |
| `src/docintel/extraction/` | Gemini adapter, retry policy, pipeline, confidence scoring, pricing, prompts |
| `src/docintel/schemas/` | Extraction schemas (wire + domain models, business rules) and the registry |
| `src/docintel/worker/` | SQS consumer (`python -m docintel.worker`) |
| `src/docintel/persistence/` | SQLAlchemy async engine, ORM models, repositories |
| `src/docintel/domain/` | Job states, IDs, queue messages, typed error codes |
| `migrations/` | Alembic (hand-written; drift-checked against models in tests) |
| `infra/` | Terraform modules + `envs/dev` ([infra/README.md](infra/README.md)) |
| `scripts/` | `deploy_ecs.sh` (CI deploy), `bootstrap_local.py`, `live_smoke.py` |
| `tests/` | `unit/`, `integration/` (real Postgres + moto), `contract/` (OpenAPI snapshot) |

## Configuration

All settings are environment variables prefixed `DOCINTEL_`. See [src/docintel/settings.py](src/docintel/settings.py) for the full list with defaults. Commonly changed ones:

| Variable | Default | Purpose |
|---|---|---|
| `DOCINTEL_GEMINI_API_KEY` | — | Gemini key (secret; `.env` locally, Secrets Manager in AWS) |
| `DOCINTEL_GEMINI_MODEL` | `gemini-3.8-flash` | Model id |
| `DOCINTEL_SYNC_DEADLINE_S` | `30` | Overall deadline for `mode=sync` |
| `DOCINTEL_MAX_UPLOAD_BYTES` | `26214400` (25 MB) | Upload size cap |
| `DOCINTEL_MAX_PAGES` | `50` | PDF page cap |
| `DOCINTEL_CONFIDENCE_THRESHOLD` | `0.7` | Below this a field is listed in `low_confidence_fields` |
| `DOCINTEL_LOG_JSON` | `true` | `false` for human-readable local logs |

Secrets are never baked into images. Outside `local`, the service refuses to start without TLS to the database, a KMS key for S3 and a Gemini key.
