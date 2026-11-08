# DocIntel AI Engine: Engineering Journal

A phase-by-phase record of what was built, the decisions made, the trade-offs accepted and the problems hit along the way. Each phase's entry is written **after the phase is complete**, so it reflects what actually happened rather than the plan.

The plan lives in [implementation.md](../implementation.md) and the product requirements in [product.md](../product.md).

| Phase | Status | Entry |
|---|---|---|
| 0: Inception & detailed design | Partially done (plan written; stakeholder items open) | [see below](#phase-0-inception--status-note) |
| 1: Foundation & walking skeleton | ✅ Code-complete and verified locally; AWS apply pending account access | [Phase 1](#phase-1-foundation--walking-skeleton) |
| 2: Ingestion & staging | ✅ Complete and verified locally; KMS/checksum enforcement to confirm in AWS dev | [Phase 2](#phase-2-ingestion--staging-pipeline) |
| 3: Extraction engine & sync API | Not started | |
| 4: Async processing & SSE | Not started | |
| 5: Audit, cost & persistence | Not started | |
| 6: Reliability & security hardening | Not started | |
| 7: Production infra & CD | Not started | |
| 8: Verification & UAT | Not started | |
| 9: Launch & hypercare | Not started | |

---

## Phase 0: Inception (status note)

The implementation plan (requirements catalogue, gap analysis, architecture, ADR summaries, phased plan) was written first. These Phase 0 items **still need real stakeholders or accounts** and are carried forward:

- Open questions in implementation.md §3.5: throughput, retention, PII, AWS account layout.
- Confirm `gemini-2.5-flash` availability and pricing; get a quota.
- Golden dataset of 50 or more labeled documents per schema.
- Full ADR write-ups in `doc/adr/`. The summaries in implementation.md §4.3 stand in for now.

---

## Phase 1: Foundation & Walking Skeleton

### 1.1 Goal

Build a deployable "empty" service with all the engineering scaffolding in place: repository layout, quality gates, local stack, database schema, container image, CI/CD and AWS infrastructure code. Every later phase adds features to this skeleton without reworking it.

### 1.2 What we built

| Area | What exists now | Key files |
|---|---|---|
| Project & tooling | Python 3.12, `uv` with a lockfile (76 packages), ruff (incl. bandit rules), `mypy --strict`, pytest | [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock) |
| Configuration | Typed settings from `DOCINTEL_*` env vars; secrets as `SecretStr`; refuses unsafe config outside `local` | [settings.py](../src/docintel/settings.py) |
| Logging | structlog JSON to stdout; uvicorn/botocore logs routed through the same pipeline; `request_id` bound via contextvars | [logging.py](../src/docintel/logging.py) |
| API skeleton | App factory, lifespan-managed resources, request-id + access-log middleware, RFC 9457 problem+json errors | [api/main.py](../src/docintel/api/main.py), [middleware.py](../src/docintel/api/middleware.py), [errors.py](../src/docintel/api/errors.py) |
| Health | `/health/live` (no I/O) and `/health/ready` (DB + S3 checks, concurrent, 1 s timeout, `ok`/`degraded`) | [routes/health.py](../src/docintel/api/routes/health.py), [health.py](../src/docintel/health.py) |
| Worker skeleton | SQS long-poll loop, bounded concurrency, at-least-once acking, graceful SIGTERM drain | [worker/runner.py](../src/docintel/worker/runner.py), [worker/\_\_main\_\_.py](../src/docintel/worker/__main__.py) |
| Database | Async SQLAlchemy engine (asyncpg, TLS verify-full), ORM models for all 4 tables, hand-written Alembic migration `0001` | [persistence/](../src/docintel/persistence/), [migrations/](../migrations/) |
| Container | Multi-stage Dockerfile: `builder` → `runtime` (non-root uid 10001, no source tree, RDS CA bundle) and a `dev` stage for tests | [Dockerfile](../Dockerfile) |
| Local stack | `docker compose`: Postgres 16, moto (S3+SQS), bootstrap, migrate, api, worker, test | [docker-compose.yml](../docker-compose.yml) |
| Tests | 34 tests: unit, real-Postgres integration (migration drift + round trip), OpenAPI contract snapshot. 89 % coverage | [tests/](../tests/) |
| CI | Lint/types, tests with a Postgres service, gitleaks, pip-audit, image build + Trivy + non-root check, Terraform fmt/validate | [ci.yml](../.github/workflows/ci.yml) |
| CD | Runs after CI succeeds on `main`: OIDC → build/push to ECR → migration task → roll api + worker → smoke test | [deploy.yml](../.github/workflows/deploy.yml), [deploy_ecs.sh](../scripts/deploy_ecs.sh) |
| Infrastructure | 9 Terraform modules and a `dev` environment: VPC, S3+KMS, SQS+DLQ (x2), RDS, ECR, ECS cluster, 2 Fargate services, ALB (TLS 1.3), GitHub OIDC deploy role | [infra/](../infra/) |

### 1.3 Key decisions and trade-offs

Each entry follows the same shape: **Decision** · **Alternatives** · **Why** · **Cost we accepted**.

#### D1. One container image, two ECS services (api + worker)
- **Decision:** A single image. The api runs `uvicorn --factory ...` (the default CMD) and the worker runs `python -m docintel.worker`.
- **Alternatives:** Separate images per service; a single service doing both jobs.
- **Why:** The api and worker share about 90 % of their code (extraction core, persistence). One image means one build, one scan and one version, and the same SHA always runs in both. Separate services still let them scale and fail independently. The api scales on CPU and requests; the worker will scale on queue depth.
- **Cost:** The worker image carries web dependencies (uvicorn) it doesn't use, roughly 10 MB. Negligible.

#### D2. App factory + injectable resources instead of a module-level `app`
- **Decision:** `create_app(settings, resources_factory)`. The lifespan builds the DB engine and S3 client and disposes of them on shutdown.
- **Alternatives:** Global `app = FastAPI()` with dependencies created at import time.
- **Why:** Importing the module never reads the environment or opens connections, so tests build apps with explicit settings and **fake resources** (no monkeypatching of globals). Startup and shutdown order is explicit.
- **Cost:** uvicorn needs the `--factory` flag. Slightly more ceremony.

#### D3. Raw ASGI middleware instead of Starlette's `BaseHTTPMiddleware`
- **Decision:** The request-context middleware is a plain ASGI callable that wraps `send`.
- **Why:** `BaseHTTPMiddleware` runs the downstream app in a separate task and re-wraps every response body. That adds overhead, stops contextvar changes from flowing back, and puts a layer in front of streaming responses. **SSE is a core feature in Phase 4**, so we avoided that from day one. Raw ASGI just observes messages as they pass.
- **Cost:** A little more low-level code (about 40 lines).

#### D4. Unhandled exceptions are caught in *our* middleware, not via `app.exception_handler(Exception)`
- **Decision:** The middleware catches anything unhandled, logs it with the request context, and returns a masked 500 problem+json that includes `X-Request-Id`.
- **Why:** Starlette runs `Exception`/500 handlers inside `ServerErrorMiddleware`, which is the **outermost** layer, *outside* our middleware. A handler registered there would produce a 500 without the `X-Request-Id` header and log it without the `request_id`. In production that is exactly the error you most need to correlate. A test ([test_middleware_errors.py](../tests/unit/test_middleware_errors.py)) proves the header and ID are present and the internal message is not leaked.
- **Cost:** If an error happens *after* the response headers were already sent (mid-stream), we can't send a new response, so we re-raise and the connection drops. That's inherent to HTTP.

#### D5. Readiness returns 200 "degraded" when a dependency is down. This changed the plan.
- **Decision:** `/health/ready` reports each dependency's status, but answers **200** with `status: degraded` rather than 503 when the DB or S3 fails. `/health/live` does no I/O and is used by the ECS container health check.
- **Alternatives:** The classic readiness pattern of returning 503 when any dependency fails. The original plan (§4.6) said S3 would be fatal.
- **Why:** The DB and S3 are **shared** by every task. If readiness fails on a shared dependency, *every* task fails its ALB check at the same moment and the target group empties, which is a full outage. That contradicts our degradation design (§4.10.4: sync extraction must keep working with the DB down). Per-task problems (crashed process, wedged event loop) still fail the check, because the request times out. Dependency health becomes a **metric and alarm**, not a routing decision.
- **Cost:** The ALB will route to a task even when its DB is unreachable. That's acceptable because each endpoint degrades gracefully by design. Verified locally: stopping Postgres gives `200 {"status":"degraded","database":{"error":"Timeout"}}`.

#### D6. Worker: at-least-once delivery, bounded concurrency, graceful drain
- **Decision:** Messages are deleted only after the handler succeeds. Concurrency is capped (default 16), and the batch size is `min(free slots, 10)`. On SIGTERM: stop polling, **cancel the in-flight 20 s long poll**, wait up to 25 s for running jobs, then cancel the rest *without acking them*.
- **Why:** SQS is at-least-once anyway, so we design for it: unacked messages reappear after the visibility timeout and go to the DLQ after 3 receives. The 25 s drain is deliberately below the ECS `stopTimeout` of 30 s, so we finish cleanly before SIGKILL. Cancelling the long poll means shutdown takes milliseconds, not up to 20 s (verified: the worker exited 0 immediately).
- **Cost:** Handlers **must be idempotent**. Phase 4 does this with a conditional `UPDATE ... WHERE status='STAGED'` claim. A message received at the exact moment of shutdown is redelivered rather than lost, which is the correct failure mode.

#### D7. Hand-written migrations, drift-checked against ORM models in CI
- **Decision:** Migration `0001` is written explicitly, not autogenerated. An integration test runs `upgrade` → `compare_metadata` (must be empty) → `downgrade base` → `upgrade` against real Postgres, in a throwaway database.
- **Why:** Autogenerate is a draft tool. It misses things such as enum lifecycles and server defaults, and it can emit destructive operations. Writing migrations by hand keeps DDL reviewable. The drift test gives us autogenerate's safety net anyway: if someone changes a model without a migration (or the reverse), CI fails. The round trip proves `downgrade` actually works instead of being dead code.
- **Cost:** Every schema change is written twice (model + migration). That's intentional friction for a database.

#### D8. Migrations: one transaction + Postgres advisory lock + run as a pre-deploy ECS task
- **Decision:** `env.py` takes `pg_advisory_xact_lock(<constant>)` inside the migration transaction. The deploy pipeline runs `alembic upgrade head` as a one-off Fargate task using the **new** image, and only rolls services if it exits 0.
- **Why:** Postgres DDL is transactional, so a failed migration rolls back completely instead of leaving a half-migrated schema. The advisory lock serialises concurrent runs (e.g. two pipelines). Running migrations in a separate task, rather than on app startup, avoids N tasks racing to migrate and keeps a failed migration from crash-looping the service.
- **Cost:** Migrations run against the *old* code still serving traffic, so they must be **backward compatible for one release** (expand/contract). Some operations (`CREATE INDEX CONCURRENTLY`) can't run in a transaction and will need their own migration.

#### D9. Secrets: RDS-managed master secret + ECS `secrets` injection; nothing in Terraform state
- **Decision:** `manage_master_user_password = true`, so RDS generates and rotates the password in Secrets Manager. ECS injects `DOCINTEL_DB_USER` and `DOCINTEL_DB_PASSWORD` using JSON-key references (`<arn>:password::`). The Gemini key secret is created **empty** by Terraform and filled out of band. Settings use `SecretStr`, so values never appear in `repr` or logs (tested).
- **Why:** Terraform state is a common place for secrets to leak. Here no secret value ever passes through Terraform. Splitting DB settings into host/user/password instead of one URL lets us take user and password straight from the RDS secret.
- **Cost:** ECS reads secrets **at task start**, so after a password rotation, new connections from old tasks fail until the tasks are replaced. Phase 6 needs either a forced redeploy on rotation or fetching the secret at runtime. Also, ECS refuses to start a task that references a secret with no value, which is why the Gemini key is not injected until Phase 3.

#### D10. DB TLS with full certificate verification
- **Decision:** Default `db_ssl_mode=verify-full`. The RDS global CA bundle is baked into the image (`ADD --chmod=644`), and RDS has `rds.force_ssl=1`. Settings **reject** `disable` outside `local`.
- **Why:** `sslmode=require` encrypts but doesn't authenticate the server, so it's open to man-in-the-middle attacks. Making the secure option the default, and making the insecure option impossible in cloud environments, beats relying on reviewers.
- **Cost:** An image dependency on AWS's CA bundle URL at build time. The bundle needs refreshing when AWS rotates CAs.

#### D11. moto instead of LocalStack for local AWS
- **Decision:** `motoserver/moto` in compose for S3 and SQS, plus a small idempotent bootstrap script that mirrors what Terraform creates.
- **Why:** We need only two services. moto is one lightweight container and starts in about a second. Same tooling idea as `moto` in unit tests.
- **Cost:** moto doesn't enforce IAM, KMS or bucket policies, so permission bugs only show up in `dev` AWS. It also has **API-fidelity gaps**, and we hit one (see Problems, P1).

#### D12. GitHub OIDC deploy role, scoped to a GitHub Environment
- **Decision:** No AWS keys in GitHub. The deploy role trusts tokens whose `sub` is `repo:<org/repo>:environment:dev`. Its permissions cover ECR push to one repo, ECS update for services in one cluster, `RunTask` only for our task families in our cluster, and `iam:PassRole` only for our four roles and only to `ecs-tasks`.
- **Why:** Short-lived credentials, nothing to rotate or leak. Binding to the *environment* (not just the repo) means environment protection rules (reviewers, branch restrictions) gate who can deploy.
- **Cost:** More IAM to write up front. `RegisterTaskDefinition` doesn't support resource-level permissions, so it has to be `*`.

#### D13. Terraform owns infrastructure; CI owns task-definition revisions
- **Decision:** `aws_ecs_service` has `ignore_changes = [task_definition, desired_count]`. CI clones the current task definition, swaps the image, and registers a new revision.
- **Why:** Without this, every `terraform apply` would roll services back to whatever image Terraform last knew, and fight the autoscaler over `desired_count`.
- **Cost:** Changes to the task definition's *non-image* settings (env vars, CPU) need either a Terraform apply followed by a deploy, or tooling that merges both. Documented in [infra/README.md](../infra/README.md).

#### D14. Smaller infrastructure choices

| Choice | Why | Trade-off |
|---|---|---|
| ECR tags `IMMUTABLE` | A git-SHA tag always means the same bytes; you can't overwrite a deployed image | Can't "re-push" a tag; you need a new commit |
| S3 gateway endpoint (free); interface endpoints off in dev | Document bytes bypass NAT (cost + data path) | Dev SQS, Secrets Manager and ECR traffic still goes through NAT |
| Single NAT in dev | ~$32/month per NAT gateway | Dev egress is single-AZ |
| Worker on `FARGATE_SPOT` in dev | Up to ~70 % cheaper; SQS redelivery makes interruptions harmless | Not for prod sync capacity |
| S3 native state locking (`use_lockfile`, TF ≥ 1.11) | No DynamoDB lock table to manage | Requires a newer Terraform |
| `Identity` columns instead of `BIGSERIAL` | SQL-standard; the sequence is owned by the column | None worth noting |
| Read-only root FS + `/tmp` volume; `initProcessEnabled` | Smaller attack surface; PID 1 forwards signals and reaps zombies | Any new write path must use `/tmp` |
| `.gitattributes` forcing LF | See Problems, P3 | None |
| ALB `drop_invalid_header_fields`, TLS 1.3-only policy | Header-smuggling hardening; NFR-06 | Clients older than TLS 1.3 can't connect (the upstream gateway is the only client) |

### 1.4 Problems we hit (and how we solved them)

| # | Problem | Root cause | Fix |
|---|---|---|---|
| P1 | The worker logged `receive_count=None` against moto | We requested `MessageSystemAttributeNames` (the current SQS API name); moto 5.1.4 only honours the legacy `AttributeNames` | Request both. Phase 6 needs the receive count to mark jobs failed on their last attempt, so this would have been a silent bug later. **Lesson:** emulators lag real APIs. |
| P2 | Readiness design in the plan contradicted the degradation matrix | The plan said S3 failure means not ready, but §4.10.4 says sync must work with S3 down | Changed readiness semantics (D5) and updated implementation.md §4.6 |
| P3 | `git add` warned that every file would become CRLF | The machine's global `core.autocrlf=true`; `deploy_ecs.sh` with CRLF breaks bash in Linux containers | Repo-level `.gitattributes` with `* text=auto eol=lf`; verified with `git ls-files --eol` |
| P4 | Host has Python 3.15, with no `uv` or Terraform | The project targets 3.12; newer interpreters may lack wheels (asyncpg, pydantic-core) | Every tool runs in pinned containers: lockfile generation, tests, Terraform, Trivy, shellcheck. CI uses the same pins. |
| P5 | ruff's bandit rules flagged `assert isinstance` in error handlers | Asserts are stripped under `python -O`, so they aren't real guards | Used `typing.cast`; FastAPI already dispatches by exception type |
| P6 | Integration tests could wipe the local dev database | Migration round trip runs `downgrade base` | Each test session creates a uniquely named throwaway database and drops it (`WITH (FORCE)`) afterwards |
| P7 | Not sure whether `use_lockfile` was GA in Terraform 1.10 | Version uncertainty | Raised the floor to 1.11 and re-validated on 1.11.4 rather than ship on a guess |

### 1.5 Verification evidence

| Check | Result |
|---|---|
| ruff (lint + format), `mypy --strict` | Clean |
| Test suite (34 tests: unit + integration + contract) | All pass; 89 % line+branch coverage |
| Migration drift (models vs migration) and downgrade round trip | No drift; round trip OK |
| Local stack (`docker compose up`) | api 200 on `/health/ready`, `X-Request-Id` echoed; migrate and bootstrap exit 0 |
| Worker end to end with moto SQS | Message received, handled and deleted (queue 0 visible / 0 in flight) |
| Graceful degradation | Postgres stopped → `/health/ready` 200 `degraded`, DB `Timeout` after 1 s, S3 still ok |
| Graceful shutdown | `docker compose stop worker` → `worker_stop_requested` → `worker_stopped` immediately, exit 0 |
| Trivy (runtime image, fixable HIGH/CRITICAL) | 0 findings |
| pip-audit (runtime deps, hash-pinned) | No known vulnerabilities |
| Container user | uid 10001 (non-root) |
| shellcheck on deploy script | Clean |
| `terraform fmt` + `validate` (TF 1.11.4, AWS provider 6.70.0) | Valid |

### 1.6 Phase 1 exit criteria (implementation.md §6, Phase 1)

| Criterion | Status |
|---|---|
| `/health/ready` returns 200 from ECS via the ALB over TLS 1.3 | ⏳ **Pending AWS access.** Infra code is valid; needs the state bucket, `github_repository`, an ACM certificate and a `terraform apply` (steps in [infra/README.md](../infra/README.md)). Verified locally through the same image. |
| A merge to `main` deploys to dev automatically in under 15 min | ⏳ Workflow and script are written and shellchecked; needs a GitHub repo and environment variables |
| Migrations run as a pre-deploy task | ✅ Implemented in `deploy_ecs.sh`; the services don't roll if the migration exits non-zero |
| No secrets in the repo | ✅ gitleaks in CI and pre-commit; no secret values in code or Terraform |

### 1.7 Known limitations carried forward

- Interface VPC endpoints are off in dev, and log groups aren't KMS-encrypted yet. **Phase 6/7.**
- Autoscaling is CPU-only. Request-count (api) and backlog-per-task (worker) scaling come in **Phase 7**.
- The ALB is internet-facing with `0.0.0.0/0`. It should be narrowed to the API Gateway, or made internal behind a VPC link. **Phase 7 decision.**
- The app connects as the RDS **master user**. A least-privilege app role and a separate migration role are needed. **Phase 6.**
- GitHub Actions are pinned by tag, not commit SHA. Pinning to SHAs (Dependabot keeps them fresh) is a supply-chain hardening item for **Phase 6**.
- Starlette warns that its TestClient is moving off `httpx`. This is upstream and harmless for now; revisit on the next dependency bump.

### 1.8 Likely interview questions, with answers

**"Walk me through what happens when ECS stops a worker task."**
ECS deregisters the task and sends SIGTERM. Our signal handler sets a stop event. The poll loop is waiting on `asyncio.wait({receive, stop})`, so it wakes immediately and cancels the in-flight 20-second long poll. It then waits up to 25 s for running jobs to finish and ack. Anything still running after that is cancelled *without* being deleted from SQS, so it reappears after the visibility timeout. 25 s is under the 30 s `stopTimeout`, so we're never SIGKILLed mid-ack. `initProcessEnabled` makes sure the signal actually reaches Python.

**"Why doesn't your readiness probe fail when the database is down?"**
The DB is shared by every task. If readiness fails on a shared dependency, every task goes unhealthy together and the ALB has nothing to route to, which turns a partial outage into a total one. Our sync extraction path is designed to work without the DB, so the right signal is "degraded" plus an alarm, not removing capacity. Readiness still fails for per-task problems such as a hung event loop, because the check times out.

**"How do you keep migrations safe in a CI/CD pipeline?"**
Migrations are hand-written and reviewed. CI checks them against the ORM models for drift and round-trips upgrade/downgrade on real Postgres. In deploy, they run as a one-off ECS task using the new image, *before* any service changes. They're transactional (Postgres DDL rolls back) and serialised by an advisory lock. If the migration fails, services aren't touched. Because old code keeps serving while the migration runs, every migration has to be backward compatible: expand first, contract a release later.

**"How are secrets handled end to end?"**
There are no secret values in code, images, Terraform state or GitHub. RDS generates and rotates the DB password in Secrets Manager. ECS injects individual JSON keys of that secret as env vars at task start, using an execution role allowed to read only those ARNs. The Gemini key secret is created empty and filled out of band. In-process, secrets are `SecretStr`, so they don't show up in reprs or logs. CI deploys through OIDC, with no AWS keys stored anywhere. The known gap is that rotation needs task replacement, which is planned for Phase 6.

**"Why is your request-ID middleware written in raw ASGI?"**
Two reasons. `BaseHTTPMiddleware` runs the app in a separate task and re-wraps response bodies, which adds overhead and gets in the way of streaming. We're shipping SSE, so we didn't want that in the path. Second, we catch unhandled exceptions in that middleware rather than in an `Exception` handler, because Starlette runs those handlers outside all user middleware. Doing it in our middleware means the 500 response still carries `X-Request-Id` and the log line has the request ID.

**"What does at-least-once delivery mean for your design?"**
Any message can be processed more than once: after a crash, a timeout, or a shutdown during receive. So we only delete after success, and handlers are idempotent. In Phase 4 the worker claims a job with a conditional `UPDATE ... WHERE status = 'STAGED' RETURNING`, so a duplicate delivery finds nothing to claim and just acks. Messages that keep failing go to a DLQ after 3 receives.

**"Why one Docker image for two services?"**
They share almost all their code, and we want the guarantee that the api and worker run the same commit. One build, one scan, one SBOM, one tag. They're still separate ECS services, so they scale and fail independently.

**"Why not let Terraform deploy new images?"**
Terraform is good at desired-state infrastructure and bad at high-frequency releases. If both owned the task definition, every infra apply would roll back the running image. So Terraform creates the service and the initial task definition and then ignores those fields, and CI registers new revisions per commit.

**Numbers to remember:** 20 s SQS long poll · 180 s visibility timeout · 3 receives → DLQ · 25 s drain < 30 s stopTimeout · 1 s health-check timeout · 120 s ALB idle timeout (for SSE) · uid 10001 · 34 tests / 89 % coverage · 9 Terraform modules.

---

## Phase 2: Ingestion & Staging Pipeline

### 2.1 Goal

Accept a document over HTTP, prove it is safe and processable, store it durably and encrypted, record a job, and hand it to the worker. This covers FR-01 (upload API), FR-02 (S3 staging) and FR-03 (MIME and size validation *before* the payload hits memory or the AI layer), plus a status endpoint (FR-11). No AI yet. Phase 3 consumes what this phase produces.

### 2.2 What we built

| Area | What exists now | Key files |
|---|---|---|
| Upload API | `POST /api/v1/extract` (async mode) → `202` + `Location`; `mode=sync` returns `501` until Phase 3 | [routes/extract.py](../src/docintel/api/routes/extract.py) |
| Status API | `GET /api/v1/jobs/{job_id}`, visible only to the submitting client | [routes/jobs.py](../src/docintel/api/routes/jobs.py) |
| Streaming upload reader | Our own multipart driver: byte cap, magic-byte sniffing, incremental SHA-256, spooled temp file | [ingestion/upload.py](../src/docintel/ingestion/upload.py) |
| Request body guard | ASGI middleware: `Content-Length` precheck plus a running byte count for chunked bodies | [api/middleware.py](../src/docintel/api/middleware.py) |
| Document inspection | PDF page count and encryption (pypdf); image dimensions and integrity (Pillow); runs in a thread | [ingestion/inspect.py](../src/docintel/ingestion/inspect.py) |
| S3 staging | Single PUT with SSE-KMS, `ChecksumSHA256`, tags, metadata; date-partitioned keys | [ingestion/staging.py](../src/docintel/ingestion/staging.py) |
| Orchestration | inspect → stage → persist (one transaction) → enqueue, with compensation on each failure | [ingestion/service.py](../src/docintel/ingestion/service.py) |
| Persistence | `JobRepository` (compare-and-set transitions), `EventRepository`; transient DB errors → 503 | [persistence/](../src/docintel/persistence/) |
| Domain | Transition table, event stages, UUIDv7 IDs, versioned queue message, 10 new error codes | [domain/](../src/docintel/domain/) |
| Schema registry (stub) | The 4 v1 schema IDs; Phase 3 adds models and prompts | [schemas/registry.py](../src/docintel/schemas/registry.py) |
| Worker | Binds `request_id` from the SQS message attribute, so logs correlate across processes | [worker/runner.py](../src/docintel/worker/runner.py) |
| Infra | App gets the KMS key; `s3:PutObjectTagging` granted; bucket policy denies non-KMS PUTs | [infra/](../infra/) |
| Tests | 99 tests (65 new): parser, inspection, body guard, staging, domain, end-to-end over real Postgres + moto. 94 % coverage | [tests/](../tests/) |

### 2.3 Key decisions and trade-offs

#### D1. Drive the multipart parser ourselves instead of FastAPI's `UploadFile`
- **Decision:** The endpoint takes the raw `Request` and feeds `request.stream()` chunks into `python-multipart`'s low-level `MultipartParser` with our own callbacks.
- **Alternatives:** `UploadFile` / `File(...)` parameters (idiomatic FastAPI).
- **Why:** Starlette parses the **whole body** (spooling it to disk) *before* the endpoint runs. A size limit there can only reject an upload after 100 MB has already been received and written. With our own callbacks, every check runs *as bytes arrive*:
  - the byte counter aborts at the cap;
  - the first 8 bytes are sniffed, and anything else is rejected immediately;
  - SHA-256 is computed incrementally;
  - data spools to disk past 1 MiB.

  Evidence: a unit test streams 100 MB with no `Content-Length` and gets a 413 after reading ≤ 26 MiB, with **peak Python memory under 6 MiB** (measured with `tracemalloc`). A test offering 50 MB of a GIF stops reading after the first chunk.
- **Cost:**
  - About 200 lines of careful code that we now own, including part-count and field-size limits.
  - OpenAPI no longer infers the request body, so we declare it by hand with `openapi_extra`.
  - FastAPI's form validation isn't available; we validate fields ourselves.

#### D2. Two layers of size enforcement
- **Decision:** A coarse **request-level guard** (cap + 256 KiB multipart overhead) in ASGI middleware, plus the exact **per-file** check inside the parser.
- **Why:** The middleware rejects on `Content-Length` *before the app runs*. In the smoke test, curl sent `Expect: 100-continue` for a 30 MB file and got `413` with **0 body bytes uploaded**. For chunked bodies without a length, the wrapped `receive` raises mid-stream. The parser check is precise (exactly 25 MiB passes, 25 MiB + 1 B fails; both are tested). The middleware also protects routes that don't use our parser.
- **Cost:** Two numbers to keep consistent. The overhead allowance is a constant next to the parser that defines it.

#### D3. Never trust the client's Content-Type; sniff magic bytes
- **Decision:** The type is decided by the file's first bytes: `%PDF-`, `\x89PNG\r\n\x1a\n` or `\xFF\xD8\xFF`, at offset 0. The declared type is logged when it disagrees, but never used.
- **Why:** The header and the file extension are entirely client-controlled. An `.exe` renamed to `.pdf` with `type=application/pdf` is rejected with 415 (unit, integration and smoke tested). A PNG declared as a PDF is stored and processed as a PNG.
- **Cost:** Strict offset 0 rejects the rare real PDF with junk bytes before `%PDF-`; readers like Acrobat tolerate that within the first 1 KB. We chose strictness and can relax it if real clients hit this.

#### D4. Inspect *before* staging; open images, never decode them
- **Decision:** pypdf gives the page count and encryption flag. Pillow `open()` (header only) gives dimensions, followed by `verify()`. Both run via `asyncio.to_thread` before anything is written to S3.
- **Why:** Rejecting early is cheaper and leaves nothing to clean up. Six rejection cases each assert that S3 **and** SQS stay empty. Checking the pixel count from the header catches decompression bombs before any pixel buffer is allocated.
- **Trade-offs we chose:**
  - We reject **all** encrypted PDFs, including owner-password-only ones that open without a password, because we can't vouch for what the model will see.
  - A thread can't be cancelled, so a pathological PDF could occupy a worker thread. This is bounded by the 25 MB cap. A process pool with a hard timeout is on the Phase 6 list.

#### D5. Single PUT with `ChecksumSHA256` instead of multipart upload
- **Decision:** One `PutObject` per document, carrying the SHA-256 we computed during upload as `ChecksumSHA256`. The plan said multipart; we changed it.
- **Why:** Multipart helps for large objects (parallel parts, retrying individual parts). At 25 MB or less, it triples the request count and adds an abort/cleanup path for nothing. Reusing the upload hash as the S3 checksum gives **end-to-end integrity**: S3 recomputes it and rejects the write on mismatch, so a corrupted transfer can't be stored. The same hash goes into `jobs.payload_sha256` for audit.
- **Cost:** It can't be verified locally. moto silently ignores checksums (see P1). We verify that the request carries the correct header in a unit test; S3's enforcement is confirmed in AWS dev.

#### D6. KMS enforced three times (defense in depth)
- **Decision:**
  1. The app sends `ServerSideEncryption=aws:kms` + `SSEKMSKeyId` + `BucketKeyEnabled` on every PUT.
  2. Settings **refuse to start** outside `local` without `DOCINTEL_S3_KMS_KEY_ID`.
  3. The bucket policy **denies** any PUT that lacks the KMS header.

  On top of that, the bucket's default encryption already applies.
- **Why:** Default encryption alone means a misconfigured writer silently relies on it. The deny policy makes such a writer fail loudly, and the settings check fails at deploy time instead of at the first upload.
- **Cost:** Every future writer (e.g. the worker writing `results/` in Phase 5) **must** send the header or get AccessDenied. That's intended, but easy to forget, so it's noted here.

#### D7. Three writes, no distributed transaction: explicit failure handling
- **Decision:** The order is S3 → DB (job + 2 events in **one** transaction) → SQS, with a defined outcome for each failure:

  | Fails at | State left behind | Client sees |
  |---|---|---|
  | S3 put | Nothing | 503 |
  | DB commit | Staged object deleted (best effort; lifecycle rule is the backstop) | 503 |
  | SQS publish | Job marked `FAILED(ENQUEUE_FAILED)` + `FAILED` event | 503 |
  | Process dies between commit and publish | Job stuck in `STAGED` with no message | (connection drops) |

  All three tested failures are integration tests against real Postgres and moto.
- **Alternatives:** A **transactional outbox** (write the message to an `outbox` table in the same DB transaction, with a relay process publishing to SQS). Or publishing to SQS *before* committing (the worker could see a job that doesn't exist yet).
- **Why:** The outbox closes the last row of that table completely, but needs a relay process, polling, and a second delivery path to operate. At v1 scale, a **stuck-job reaper** (Phase 6) that re-enqueues `STAGED` jobs older than N minutes covers the same gap more cheaply. Marking enqueue failures as `FAILED` means a job never sits in `STAGED` looking healthy.
- **Cost:** A small window where a job can be stuck until the reaper runs. Also, until `Idempotency-Key` (Phase 6) a client that retries after a 503 creates a *new* job.

#### D8. Compare-and-set state transitions
- **Decision:** `JobRepository.transition()` runs a single `UPDATE jobs SET status=:to ... WHERE id=:id AND status IN (:from) RETURNING *`. A central table (`ALLOWED_TRANSITIONS`) rejects illegal moves (e.g. `STAGED → COMPLETED`) as programming errors.
- **Why:** This is the foundation for Phase 4. With at-least-once SQS delivery, two workers can receive the same job. Only one `UPDATE` matches, and the other gets `None` and acks. No `SELECT ... FOR UPDATE`, no lock held across a 3-second Gemini call.
- **Cost:** Callers must handle `None` ("someone else moved it"), which is the point.

#### D9. UUIDv7 job IDs
- **Decision:** Generated in-app with a 48-bit millisecond timestamp prefix (RFC 9562). This is a 10-line implementation, since Python only gets `uuid.uuid7` in 3.14.
- **Why:** Random UUIDv4 primary keys scatter inserts across the B-tree, causing page splits and poor cache locality. v7 keys append to the right edge like a sequence, but remain globally unique and unguessable (74 random bits), and are safe to put in URLs. They also sort roughly by creation time, which helps debugging.
- **Cost:** IDs within the same millisecond aren't ordered relative to each other. Nothing relies on that.

#### D10. A versioned message contract
- **Decision:** `{"type":"job.submitted","v":1,"job_id":...,"schema_id":...}` as a strict Pydantic model (`extra="forbid"`), with `request_id` carried as an SQS **message attribute**.
- **Why:** The producer (api) and consumer (worker) deploy independently, so a consumer must recognise a shape it doesn't understand rather than mis-parse it. Carrying `request_id` makes one ID traceable from HTTP through SQS into worker logs (verified in the smoke test). It stands in for W3C `traceparent`, which arrives with OpenTelemetry in Phase 6.

#### D11. Small API-contract choices

| Choice | Why |
|---|---|
| Unknown form fields → 400 | A typo like `schema-id` fails loudly instead of being silently ignored |
| `mode=sync` → 501, checked **before** reading the body | The contract is stable now; no wasted upload |
| Job visible only to its `X-Client-Id`; otherwise **404**, not 403 | Defense in depth beyond unguessable IDs; 403 would confirm the job exists |
| `stream_url` left out of the 202 response until Phase 4 | Never return a link that 404s; adding the field later is backward compatible |
| Transient DB errors → 503 + `Retry-After: 5`; `IntegrityError` is *not* | Only "try again later" failures get the retryable signal; bugs and conflicts must surface as bugs |
| Client filename sanitized and stored only as metadata, never in the S3 key | Path traversal (`../../etc/passwd`) and control characters are neutralised and tested |

#### D12. Test infrastructure: moto as a container, not a library
- **Decision:** Integration tests reach S3, SQS and KMS through the **moto container**. It's a compose service locally and a GitHub Actions service container in CI, the same pattern as Postgres. We first tried `moto[server]` in-process.
- **Why:** `moto[server]` pulls a large dependency tree (cfn-lint, sympy and more) into the dev image. On the slow network it repeatedly timed out the build (see P3). A container keeps the image lean, matches how Postgres is handled, and tests the real HTTP path through aiobotocore.
- **Cost:** Integration tests need the compose stack (`docker compose run --rm test` handles it). As before, CI sets `REQUIRE_INTEGRATION=1`, so a missing service **fails** the run instead of skipping it.

### 2.4 Problems we hit (and how we solved them)

| # | Problem | Root cause | Fix / lesson |
|---|---|---|---|
| P1 | The integration test asserting `ChecksumSHA256` on the stored object failed | We probed moto: it **neither stores nor validates** checksums, and a deliberately wrong checksum was *accepted* | Moved the assertion to a unit test on the outgoing request; S3-side enforcement goes on the AWS dev checklist. **Second moto fidelity gap** (Phase 1 had the receive count). Lesson: probe the emulator before trusting a green test for the behaviour. |
| P2 | Postgres container lost its network endpoint; tests failed with DNS errors | Another local project already publishes host port 5432; our port binding failed | Host port is now `${DOCINTEL_HOST_PG_PORT:-5433}`. Containers use the compose network and never needed the host port. |
| P3 | Image builds timed out repeatedly (`Failed to download botocore ... network timeout`) | A slow link plus uv's default of 50 parallel downloads | Removed `moto[server]` (D12). Added `UV_CONCURRENT_DOWNLOADS` / `UV_HTTP_TIMEOUT` build args (defaults unchanged). Built with `--build-arg UV_CONCURRENT_DOWNLOADS=4`. |
| P4 | ruff RUF012 flagged SQLAlchemy `__mapper_args__`; adding `ClassVar` then broke mypy | SQLAlchemy's typing declares it as an instance attribute | A targeted `noqa: RUF012` with the reason in a comment. Satisfying a linter must not break the type checker. |
| P5 | The OpenAPI snapshot test failed | It worked as designed: the contract changed (2 new endpoints) | Regenerated deliberately with `UPDATE_SNAPSHOTS=1`. Reviewers see the contract diff in the PR. |
| P6 | Smoke-test `curl -F "file=@/c/...;type=..."` couldn't open the file | Git Bash path conversion splits arguments on `;` | Run from the file's directory with relative paths and `MSYS_NO_PATHCONV=1`. A tooling quirk, not an app bug. |

### 2.5 Verification evidence

| Check | Result |
|---|---|
| ruff, `mypy --strict` (36 files) | Clean |
| Tests | **99 passed** (unit + real Postgres + moto); **94 %** line+branch coverage |
| 100 MB stream, no `Content-Length` | 413 after ≤ 26 MiB read; peak Python memory < 6 MiB |
| Exact boundary | Cap passes; cap + 1 byte → 413 |
| Rejections leave no trace | 6 cases (EXE-as-PDF, encrypted, 51 pages, unreadable, unknown schema, no schema): S3 and SQS empty |
| Failure handling | SQS down → 503 + job `FAILED(ENQUEUE_FAILED)`; DB down → 503 + staged object deleted; S3 down → 503 + no job row |
| End to end on the running stack (runtime image) | `202` + `Location` + `X-Request-Id`; GET shows `STAGED`, 3 pages; 30 MB → `413` with **0 body bytes sent**; EXE-as-PDF → `415` problem+json; worker log carries `request_id=smoke-p2-1` and `receive_count=1` |
| Trivy (runtime image incl. Pillow, pypdf) | 0 HIGH/CRITICAL |
| pip-audit | No known vulnerabilities |
| Terraform fmt + validate | Clean / valid |

### 2.6 Phase 2 exit criteria

| Criterion | Status |
|---|---|
| 25 MB + 1 B upload rejected with 413 **before the full body is read**, memory bounded | ✅ Parser and body-guard tests plus the smoke test above |
| EXE renamed to `.pdf` rejected with 415 | ✅ Unit, integration and smoke |
| Staged objects KMS-encrypted (checked in dev) | ⏳ **AWS dev pending.** Locally: every PUT carries the SSE-KMS headers (unit test), moto reports `aws:kms`, the bucket policy denies non-KMS PUTs, and settings require a key outside `local` |
| Job + event rows created; SQS message carries `job_id` and trace context | ✅ `job_id` + `request_id` correlation (verified across processes). W3C `traceparent` arrives with OTel in Phase 6 |

### 2.7 Known limitations carried forward

- **Stuck-job reaper** for the commit→publish crash window (D7). **Phase 6.**
- **`Idempotency-Key`**: retrying after a 503 creates a new job. **Phase 6.**
- Async submit **requires the database**. The plan's "accept without DB" degraded mode isn't implemented. **Phase 6.**
- pypdf/Pillow run in threads that can't be time-bounded. Consider a process pool with a timeout. **Phase 6.**
- OpenAPI still advertises FastAPI's default `422 HTTPValidationError` for path/query validation, although we return `400` problem+json. **Fix in Phase 3.**
- Spool writes are synchronous file I/O on the event loop (64 KB chunks). Fine at this scale; revisit if profiling says otherwise.
- In compose, `/tmp` is a tmpfs (RAM-backed), so locally the spool uses RAM. On Fargate `/tmp` is ephemeral disk.

### 2.8 Likely interview questions, with answers

**"How do you enforce a 25 MB upload limit without reading 25 MB first?"**
Two layers. An ASGI middleware checks `Content-Length` before the app runs. In our smoke test a 30 MB upload got a 413 with zero body bytes sent, because curl waits on `Expect: 100-continue`. For chunked uploads with no length, the middleware wraps `receive` and raises the moment the running count passes the cap. Inside, we don't use FastAPI's `UploadFile`, because Starlette reads the whole body before your code runs. We drive the multipart parser ourselves, so the per-file counter, the type sniffing and the hashing all happen as bytes arrive. A test streams 100 MB and we stop after about 25, with peak memory under 6 MB.

**"Why not trust the Content-Type header?"**
It's client-controlled, like the file extension. We look at the first bytes of the actual file (`%PDF-`, the PNG signature, the JPEG SOI marker) and reject everything else with 415 as soon as 8 bytes have arrived. A renamed executable never reaches the parser libraries or S3.

**"You write to S3, Postgres and SQS. How do you keep them consistent?"**
There's no distributed transaction, so I ordered the writes and defined what happens when each one fails. S3 first: if it fails, nothing exists. Then the job and its events in one DB transaction: if that fails, we delete the staged object, and a lifecycle rule backs that up. Then SQS: if publishing fails, we mark the job `FAILED` so it doesn't sit in `STAGED` looking fine. The remaining gap is a crash between commit and publish. I considered a transactional outbox, which closes it fully but needs a relay process. For v1, a reaper that re-enqueues old `STAGED` jobs is cheaper and good enough. All three failure paths have integration tests.

**"How do you know the bytes in S3 are what the client sent?"**
We hash the stream as it arrives and send that SHA-256 as S3's `ChecksumSHA256` on the PUT. S3 recomputes it and rejects the write on mismatch, so corruption anywhere in between can't be stored. The same hash goes into the job row for audit. Interesting detail: our local emulator ignores checksums entirely. We found that by probing it with a wrong checksum, so that guarantee is verified on the request side in tests and on the S3 side in the real AWS environment.

**"How do you defend against malicious files?"**
Layers:
- Size caps per file and per request.
- Magic-byte allow-list.
- Encrypted PDFs rejected.
- Page cap.
- Images checked by declared dimensions from the header *before* decoding, which defuses decompression bombs.
- Filenames sanitized and never used as paths or keys.
- S3 keys built only from our own IDs.

What's left is that a pathological PDF can tie up a thread during parsing. It's bounded by the size cap, and a process pool with a hard timeout is planned.

**"Why UUIDv7 instead of auto-increment or UUIDv4?"**
Auto-increment leaks volume and is guessable in URLs. UUIDv4 is unguessable but random, so inserts land all over the primary-key B-tree. UUIDv7 puts a millisecond timestamp first, so inserts append like a sequence, while keeping 74 random bits. You get unguessable and index-friendly at the same time.

**"What happens if two workers get the same job?"**
SQS is at-least-once, so that can happen. The claim is one conditional `UPDATE ... WHERE status = 'STAGED' RETURNING`. Exactly one worker's update matches; the other gets no row back and just acks the message. No row locks are held during the long AI call.

**Numbers to remember:** 25 MiB per file · +256 KiB request overhead · 64 KiB form fields · 10 parts max · 1 MiB in-memory spool · 8-byte sniff window · 50 pages · 50 MP images · `Retry-After: 5` · 99 tests / 94 % coverage · 0 body bytes sent before a 413 on oversize.
