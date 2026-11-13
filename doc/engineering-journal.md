# DocIntel AI Engine: Engineering Journal

A phase-by-phase record of what was built, the decisions made, the trade-offs accepted and the problems hit along the way. Each phase's entry is written **after the phase is complete**, so it reflects what actually happened rather than the plan.

The plan lives in [implementation.md](../implementation.md) and the product requirements in [product.md](../product.md).

| Phase | Status | Entry |
|---|---|---|
| 0: Inception & detailed design | Partially done (plan written; stakeholder items open) | [see below](#phase-0-inception--status-note) |
| 1: Foundation & walking skeleton | ✅ Code-complete and verified locally; AWS apply pending account access | [Phase 1](#phase-1-foundation--walking-skeleton) |
| 2: Ingestion & staging | ✅ Complete and verified locally; KMS/checksum enforcement to confirm in AWS dev | [Phase 2](#phase-2-ingestion--staging-pipeline) |
| 3: Extraction engine & sync API | ✅ Complete; verified live on real Gemini. Primary-model latency and golden-set accuracy pending (provider overload, no labelled set) | [Phase 3](#phase-3-extraction-engine--sync-api) |
| 4: Async processing & SSE | ✅ Complete; verified live on real Gemini. SSE load targets pending (Phase 8) | [Phase 4](#phase-4-async-processing--live-progress-sse) |
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

---

## Phase 3: Extraction Engine & Sync API

### 3.1 Goal

Turn a validated document into **schema-validated JSON with field-level confidence**, synchronously (FR-04, FR-05, FR-06, FR-08, FR-10). This means the model integration, per-document-type schemas, business-rule validation, scoring, cost accounting, retries, and `mode=sync` with an audit trail.

### 3.2 What we built

| Area | What exists now | Key files |
|---|---|---|
| Schemas | 4 types (`invoice`, `financial_statement`, `contract`, `generic_document`), each a **wire** model (what Gemini fills) plus a strict **domain** model (`Decimal`, `date`, ISO codes) and business rules | [schemas/](../src/docintel/schemas/) |
| Schema registry | Converts Pydantic to Gemini JSON Schema; **rejects unsupported keywords at import time** (startup + CI) | [schemas/registry.py](../src/docintel/schemas/registry.py) |
| Gemini adapter | Interactions API, inline or Files API input, `store=False`, SDK retries off, error classification, provider message capture | [extraction/gemini_client.py](../src/docintel/extraction/gemini_client.py) |
| Retry policy | Deadline-aware, full jitter, honours `Retry-After`, reports every attempt | [extraction/resilience.py](../src/docintel/extraction/resilience.py) |
| Model failover | Primary → fallback on retryable failure, sticky per request; per-model thinking level | [extraction/pipeline.py](../src/docintel/extraction/pipeline.py), [model_profiles.py](../src/docintel/extraction/model_profiles.py) |
| Pipeline | Repair retry on invalid JSON, token-budget retry on truncation, domain conversion, rules, scoring | [extraction/pipeline.py](../src/docintel/extraction/pipeline.py) |
| Scoring | Adjusted validity = model score × rule penalties × critical-field confidence; `low_confidence_fields` vs `missing_fields` | [extraction/confidence.py](../src/docintel/extraction/confidence.py) |
| Pricing | Effective-dated prices per model; thinking billed as output | [extraction/pricing.py](../src/docintel/extraction/pricing.py) |
| Sync API | `POST /api/v1/extract?mode=sync`: S3 staging in parallel with Gemini, persistence after the response, capacity guard | [ingestion/service.py](../src/docintel/ingestion/service.py), [routes/extract.py](../src/docintel/api/routes/extract.py) |
| Schemas API | `GET /api/v1/schemas` | [routes/schemas.py](../src/docintel/api/routes/schemas.py) |
| Contract | OpenAPI now documents `400`/problem+json everywhere (fixed Phase 2's 422 mismatch) | [api/errors.py](../src/docintel/api/errors.py) |
| Demo UI | Vanilla JS + vendored Bootstrap at `/ui` (local/dev only), strict CSP, renders untrusted output as text only | [web/](../src/docintel/web/) |
| Live tooling | `live` compose service (opt-in, sees `.env`); `scripts/live_smoke.py` with a synthetic invoice of known truth | [scripts/live_smoke.py](../scripts/live_smoke.py) |
| Tests | **178** (79 new), **95 %** coverage. Gemini is scripted in tests; real-API checks are opt-in | [tests/](../tests/) |

### 3.3 Key decisions and trade-offs

#### D1. Re-researched the model and API before writing code. The PRD's choice was obsolete
- **Decision:** Default to **`gemini-3.8-flash`** on the **Interactions API**, not the PRD's `gemini-2.5-flash` on `generateContent`.
- **Why:** On 2026-11-11, `gemini-2.5-flash` was closed to new projects on the Gemini API and retired on Vertex AI. Google calls Interactions "the recommended standard API for all new projects". The research also changed four other assumptions:
  - Thinking can no longer be disabled (`thinking_budget=0` is gone; `thinking_level` replaces it).
  - `temperature`/`top_p` are deprecated, and future models will reject them with a 400.
  - Prices are 2.5–3× the plan's.
  - Schemas are now plain JSON Schema.
- **Cost:** The SDK's Interactions client and its error classes live in a **private** package (`google.genai._gaos`). We confined those imports to one adapter module and pinned their behaviour with tests, so an SDK upgrade that moves them fails CI instead of breaking production silently.

#### D2. Wire model ≠ domain model (ADR-04, now proven)
- **Decision:** Gemini fills a wire model of simple JSON types, where every field is `{value, confidence, page}`. We convert it to a strict domain model. A field that fails to convert becomes `null` with confidence 0 and an `UNPARSEABLE_VALUE` warning, instead of failing the document.
- **Why:** Money must never be a float (`Decimal(str(v)).quantize("0.01")`), and dates must be real dates. Models produce `"31/02/2026"` and `"dollars"`. Tolerating that per field keeps 95 % of a document useful when 5 % is garbage.
- **Cost:** Each schema needs a conversion function. It's mechanical, but it's code.

#### D3. A schema check that rejects even the constraints Gemini *ignores*
- **Decision:** The registry walks every generated JSON Schema. It strips descriptive keys (`title`, `default`) and **raises** on any keyword outside Gemini's supported set, including `maxLength`/`pattern`, which Gemini documents as ignored. Since a real-world 400 (P8), it also rejects **length limits on arrays of objects**.
- **Why:** An ignored constraint is worse than a missing one: you believe it's enforced. Running the check at import time means a bad schema fails at startup and in CI, never on a customer's request.
- **Cost:** Limits that Gemini can't express (string lengths, list caps) are enforced in our domain conversion instead (e.g. `MAX_KEY_VALUES`, with a `TRUNCATED` warning).

#### D4. Never return the model's confidence alone
- **Decision:** `document_validity_score` = model self-assessment × Π(rule penalties) × min(1, mean critical-field confidence / 0.9). The raw model score is returned next to it.
- **Business rules:**
  - invoice: line items sum to the subtotal, subtotal + tax = total, date sanity, due date after issue date;
  - financial statement: assets = liabilities + equity;
  - contract: term not reversed, at least two parties.
- **Why:** Live data confirmed the concern from the plan's risk register. The model reported **1.0** validity and **0.95** on nearly every field, on a study guide. Self-reported confidence is not calibrated. Rules catch what the model can't be trusted to check.
- **Cost:** The penalty weights are guesses until Phase 8 calibrates them against labelled documents.

#### D5. "Missing" is not "uncertain"
- **Decision:** `null` values go to `missing_fields`, and `low_confidence_fields` contains only values that were found but are shaky. A missing *critical* field still lowers the score.
- **Why:** In the first live result, `document_date` (correctly absent from a study guide) was flagged as low confidence. That's noise that trains reviewers to ignore the flag. These are two different signals for downstream human review.

#### D6. One retry layer, and it's ours
- **Decision:** A hand-written `with_retries`:
  - exponential backoff with **full jitter**;
  - honours `Retry-After`;
  - **never starts an attempt that can't finish before the deadline**;
  - calls `on_failure` for every attempt (the audit trail).

  The SDK's own retries are disabled (P3).
- **Why not tenacity:** These three requirements interact (the deadline, `Retry-After` and audit callbacks), and 40 readable lines beat configuring around a general library.
- **Classification:** 408/429/5xx and connection errors are retryable. 400/401/403/404 and **unknown errors** are fatal, since retrying something you don't understand just burns quota.

#### D7. Model failover, with per-model settings
- **Decision:** With `gemini-3.5-flash-lite` configured as the fallback:
  - the primary gets 2 attempts, then the request **fails over** inside the same deadline;
  - the switch is **sticky** for the rest of that request, so a repair retry doesn't go back to the overloaded model;
  - 4xx errors never fail over (our request is wrong; another model won't fix it);
  - every attempt is audited and priced under the model that actually ran.
- **Why:** The provider was overloaded on and off for two days of live testing. Failover turned "every request fails" into **9/9 fields correct on all three live runs**.
- **Lesson baked in:** thinking levels are **not portable**. 3.8 Flash rejects `minimal` and accepts `low`; Flash-Lite documents `minimal`/`medium`/`high` and never `low`. The level is a property of the model (`model_profiles.py`), not of the schema.

#### D8. Sync mode keeps S3 and Postgres off the critical path
- **Decision:** S3 staging runs **concurrently** with the Gemini call (`asyncio.create_task`). Job, events, result and audit rows are written by a background task **after** the response is sent, and the lifespan drains those tasks on shutdown.
- **Why:** Neither backing store adds latency or can fail the request (NFR-05, tested): with the database down the client still gets its result, and the audit record goes to a structured `audit_fallback` log line.
- **Retention:** source deleted on COMPLETED (data minimisation), kept on FAILED (can be re-driven, and expires via the lifecycle rule).
- **Cost:** a "200 OK" doesn't guarantee the audit row exists yet. That's accepted; Phase 5 adds automated replay of the fallback log.

#### D9. Fail fast on capacity rather than queueing
- **Decision:** `SYNC_MAX_CONCURRENCY` (32) uses a semaphore checked with `locked()` before acquiring, returning `429 SYNC_CAPACITY_EXHAUSTED` + `Retry-After: 2` instead of waiting.
- **Why:** A sync caller queued behind 32 others would blow its own deadline anyway. Telling it immediately lets it retry or switch to async. There's no `await` between the check and the acquire, so it can't race.

#### D10. Privacy defaults that don't depend on vendor defaults
- **Decision:** Every Interactions call sends `store=False`, pinned by a test.
- **Why:** The API can persist requests and responses server-side, and the default isn't documented. For financial documents that must be opt-in.
- **Related finding:** On the **free tier, Google may use the data to improve its products** (pricing page: "Used to improve our products: Yes"). Real documents require the paid tier (plan assumption A-5).

#### D11. Prices are code, effective-dated
- **Decision:** `PRICES[model] = (Price(from, in, out), ...)`, using the rate in force when the call ran. Thinking tokens are billed at the output rate (confirmed on the pricing page). An unknown model logs an error and records `cost=None`. It **never blocks** an extraction.
- **Why:** 3.8 Flash's price **doubles on 2027-01-01**, and a config file would let someone change prices without review. Live check: 2,405 in × $0.30/M + 2,161 out × $2.50/M = **$0.006124**, exactly what was recorded.

#### D12. A demo UI that doesn't weaken the security posture
- **Decision:**
  - Plain HTML/JS with vendored Bootstrap, verified against the official SRI hashes;
  - served by the API at `/ui` only in local/dev;
  - all model output written via `textContent`;
  - strict CSP: same-origin script, no inline code, `frame-ancestors 'none'`.

  Static tests fail the build on `.innerHTML`, inline `<script>`, `style=` or external URLs.
- **Why:** Extracted values come from **untrusted documents**. A PDF containing `<script>` text will be faithfully extracted by the model. The UI must treat model output like user input.

### 3.4 Problems we hit (and how we solved them)

| # | Problem | Root cause | Fix / lesson |
|---|---|---|---|
| P1 | The PRD's model was unusable | `gemini-2.5-flash` closed to new projects and retired on Vertex AI (Oct 2026) | Research first; `gemini-3.8-flash` via config; recorded as a PRD deviation. **Lesson:** a model ID in a requirements doc is a dependency with an expiry date. |
| P2 | The plan's latency lever disappeared | Gemini 3.x can't turn thinking off; `minimal` is rejected by 3.8 Flash | `thinking_level="low"`; latency became a measured risk instead of an assumption |
| P3 | One "attempt" took ~27 s and the deadline never fired; the audit showed 1 attempt where ~5 happened | **The SDK retries silently by default (5 attempts / 30 s)** under our own policy | `create_genai_client()` sets `retry_options.attempts=1`; a regression test asserts the SDK's own config translates to 0 retries. **Lesson:** check every layer for retries. Stacked retries multiply calls into an outage. |
| P4 | A test's error log contained **`'password': 'docintel'`** | structlog's default `dict_tracebacks` serialises **every frame's local variables**, and asyncpg's connect frame holds the password | `ExceptionDictTransformer(show_locals=False)`; regression test with a runtime-built secret. In production this would have written the RDS password to CloudWatch on every DB error. |
| P5 | A settings test passed for the wrong reason | Loading `.env` into the `test` container exposed the real Gemini key to the suite | Separate opt-in `live` service; autouse fixture deletes `DOCINTEL_GEMINI_API_KEY` in every test |
| P6 | The API key was pasted into **`.env.example`** | That file is tracked and the repo has a GitHub remote | Moved to git-ignored `.env` without printing it (only names and lengths shown); confirmed not in any commit or the index |
| P7 | A Flash-Lite fallback would always have 400'd | Thinking levels differ per model (`low` vs `minimal`) | Per-model profiles; caught by **reading the docs before shipping the fallback**, not in production |
| P8 | `generic_document.v1` → **400 "too many states for serving"** on Flash-Lite | `maxItems=50` on an array of nested objects explodes the constrained-decoding grammar | Cap moved to domain conversion; the registry now rejects length limits on arrays of objects |
| P9 | Requests kept failing after the outage cleared | **Free tier: 20 requests/day** on 3.8 Flash, and *failed* 503 retries count against it | Surfaced by logging Gemini's own error text (`provider_message`); README troubleshooting updated; billing recommended |
| P10 | README promised `docker compose down` keeps data; `live` failed in a fresh clone | Anonymous Postgres volume; image name derived from the folder name | Named `pgdata` volume; fixed image name `docintel-dev:local`. Found by **walking the README end to end on a clean machine state** |
| P11 | Builds failed on DNS hiccups | `# syntax=docker/dockerfile:1.7` fetches the Dockerfile frontend from Docker Hub on every build | Removed; Docker 29's built-in frontend supports our features |
| P12 | 71–75 s live latency even though Flash-Lite answered in ~7 s | Each hung primary attempt waited the full per-call timeout (60 s demo override) before failover | Carried forward (§3.7): per-attempt timeouts must reserve budget for failover |

### 3.5 Verification evidence

| Check | Result |
|---|---|
| ruff, `mypy --strict` (53 files) | Clean |
| Tests | **178 passed**, 95 % line+branch coverage |
| Recovery paths (scripted Gemini) | Repair retry, truncation retry, outage → 503, 400 → 502, deadline → 504, failover, sticky failover, 4xx never fails over, capacity → 429 |
| Graceful degradation | DB down → 200 + `audit_fallback` log; S3 down → 200; no key → 503 |
| SDK contract pins | `store=False`, no sampling params, JSON-schema `response_format`, Files API cleanup even on failure, SDK retries = 0, private error classes classified |
| Live: key and network | Key authenticates; `models.get` 200–350 ms |
| Live: schema acceptance | `generic_document.v1` and `invoice.v1` accepted by Flash-Lite and validated against the wire models |
| Live: accuracy (synthetic invoice, known truth) | **9/9 fields correct ×3 runs**, line items exact, no false warnings (served by the fallback) |
| Live: real 4-page PDF (generic) | COMPLETED; 3 audited attempts (2 × 503 primary, then success on fallback); full event history; source deleted from S3 |
| Live: cost | $0.002386 (1-page synthetic invoice), $0.006124 (4-page PDF); recomputed by hand, exact |
| Live: resilience | 503 / 429 / timeouts classified correctly; retries visible; failover observed |

### 3.6 Phase 3 exit criteria

| Criterion | Status |
|---|---|
| 100 % of responses validate against the domain model or fail with a documented code | ✅ |
| Every field carries `confidence`; the response carries `document_validity_score` | ✅ (plus `model_reported_validity`, `low_confidence_fields`, `missing_fields`) |
| Retries proven with injected 429/503 sequences | ✅ in tests, and observed live |
| Invoice critical-field accuracy ≥ 90 % on the golden set | 🟡 **100 % on a synthetic invoice (3 runs)**, on the fallback model. No labelled golden set exists yet (a Phase 0 item) |
| Sync p95 for 3-page PDFs measured (target < 4.0 s) | ⏳ **Not met / not measurable**: the primary model was overloaded for two days; observed 16–75 s, dominated by failed primary attempts. Fallback attempts alone took ~7–9 s |

### 3.7 Known limitations carried forward

- **Timeout budgeting with failover (P12).** A hung primary can consume the deadline before failover. Options: cap each primary attempt at a fraction of the remaining budget, or send a hedged request to the fallback after N seconds. **Next.**
- **A 429 that says "retry in 15h59m"** is still retried once on the same model, wasting quota. It should fail over immediately.
- Confidence and validity are uncalibrated (1.0 / 0.95 everywhere observed). Calibrate against a golden set. **Phase 8.**
- No golden dataset yet, so the accuracy claim rests on one synthetic document.
- Audit replay from the `audit_fallback` log line. **Phase 5.**
- Breaker state, metrics and dashboards. **Phase 6.**
- The demo UI isn't browser-tested in CI (static guards plus manual use only).

### 3.8 Likely interview questions, with answers

**"How do you guarantee the model's output matches your schema?"**
Three layers:
1. Gemini's structured output, constrained decoding against a JSON Schema generated from Pydantic, which guarantees shape.
2. Strict Pydantic validation of the raw response. If that fails, one repair retry quotes the validation errors back to the model, and a second failure returns a documented 502.
3. Conversion into strict domain types, where a single bad field becomes null with a warning instead of failing the document.

On top of that, a registry check rejects any JSON Schema construct Gemini doesn't enforce, at startup and in CI.

**"Can you trust the model's confidence scores?"**
No, and we measured that. On a study guide it reported 1.0 validity and 0.95 on almost every field. So the score we return combines the model's self-assessment with deterministic business rules (totals add up, the balance sheet balances) and the confidence on critical fields, and we return the raw score alongside so the adjustment is auditable. Proper calibration needs a labelled dataset; that's planned.

**"Walk me through what happens when Gemini is overloaded."**
The first attempt gets a 503. Our retry layer backs off with full jitter and tries again within the request's deadline. After two failures on the primary, the request fails over to Flash-Lite with that model's own settings, and stays there. Every attempt is written to the audit table with its model, status, latency, tokens and cost. If everything fails, the client gets a 503 with `Retry-After`, never a hang. Live, this turned an outage where every request failed into 9/9 correct extractions.

**"What was the hardest bug?"**
Two candidates:
- **Hidden SDK retries.** One "attempt" took 27 seconds and our deadline logic seemed broken. The Gemini SDK retries five times internally by default, stacked under our retries. That multiplied calls into an already overloaded provider and hid most attempts from the audit log. We turned SDK retries off, and a test pins it.
- **The traceback that leaked the database password.** The structured logger serialised stack-frame locals by default, and one of those frames was the Postgres driver's connect call.

**"How do you handle the cost of LLM calls?"**
Every attempt, including failed and repaired ones, is priced from token counts using an effective-dated price table in code, so it's reviewed and the January 2027 price change applies automatically. Thinking tokens are billed at the output rate. A missing price logs an error but never blocks extraction. The numbers reconcile exactly with a hand calculation.

**"Why is the UI safe if the model output comes from untrusted documents?"**
A malicious PDF can contain HTML or script, and the model will faithfully extract it. The UI only writes values with `textContent`, never `innerHTML`; a test fails the build if anyone adds an HTML sink. The Content-Security-Policy allows only same-origin scripts with no inline code, so even a mistake there can't execute injected script.

**Numbers to remember:** `gemini-3.8-flash` primary / `gemini-3.5-flash-lite` fallback · 2 primary attempts then failover · 30 s sync deadline · 25 s per-call timeout · 32 sync slots · $0.75/$3.75 per 1M tokens (→ $1.50/$7.50 on 2027-01-01) · ~$0.002–0.006 per document · 4 schemas · 178 tests / 95 % coverage · 9/9 fields × 3 live runs.

---

## Phase 4: Async Processing & Live Progress (SSE)

### 4.1 Goal

Make `mode=async` actually process documents: a worker consumes the queue, extracts, persists and publishes progress, and clients watch it live over Server-Sent Events (FR-07, FR-11). It also closes the Phase 3 carry-over on timeout budgeting, so failover works inside a deadline.

### 4.2 What we built

| Area | What exists now | Key files |
|---|---|---|
| Extraction handler | Claim (compare-and-set) → load source from S3 → extract → persist result, audit and events → delete source; transient vs permanent failure handling | [worker/extraction_handler.py](../src/docintel/worker/extraction_handler.py) |
| Runner upgrades | **Visibility heartbeat** while a job runs; `RetryLaterError` reschedules a message with backoff instead of deleting it | [worker/runner.py](../src/docintel/worker/runner.py) |
| Live events | `pg_notify` written in the same transaction as the event; one LISTEN connection per API process (`EventBroker`) with auto-reconnect | [persistence/events.py](../src/docintel/persistence/events.py) |
| SSE endpoint | `GET /api/v1/jobs/{id}/stream`: replay after `Last-Event-ID`, live wake-ups, 15 s keep-alive, terminal close, 10 min cap | [api/routes/jobs.py](../src/docintel/api/routes/jobs.py) |
| Job result | `GET /api/v1/jobs/{id}` returns data, validity, warnings, model and **usage/cost summed across all attempts** once COMPLETED | [api/routes/jobs.py](../src/docintel/api/routes/jobs.py), [repositories.py](../src/docintel/persistence/repositories.py) |
| Readiness | `events` check (LISTEN connected), reported but never fatal | [health.py](../src/docintel/health.py) |
| Timeout budgeting | A non-final model may use at most **50 %** of the remaining deadline; a provider "retry in 15h59m" stops same-model retries and fails over immediately | [pipeline.py](../src/docintel/extraction/pipeline.py), [resilience.py](../src/docintel/extraction/resilience.py), [gemini_client.py](../src/docintel/extraction/gemini_client.py) |
| Refactors | `build_pipeline` → `extraction/factory.py` (the worker doesn't import the API); row mappers → `persistence/records.py` | |
| UI | Async mode streams a live progress timeline (`EventSource`), including "AI provider busy, retrying in 30 s", then shows the full result | [web/static/app.js](../src/docintel/web/static/app.js) |
| Tests | **200** (22 new), 94 % coverage, incl. a live NOTIFY → SSE test | [test_async_worker.py](../tests/integration/test_async_worker.py) |

### 4.3 Key decisions and trade-offs

#### D1. Claim with compare-and-set; reclaim only on redelivery
- **Decision:** `UPDATE jobs SET status='PROCESSING', attempt_count = attempt_count + 1 ... WHERE id=:id AND status IN (:from) RETURNING *`.
  - `from` is `{STAGED}` on first delivery.
  - On a **redelivery** (receive count > 1) it's `{STAGED, PROCESSING}`.
  - A delivery that claims nothing is acked as a duplicate.
- **Why reclaiming is safe:** the visibility heartbeat keeps a running job's message invisible. The only way a `PROCESSING` job's message becomes visible again is that its worker stopped heartbeating, i.e. died. Without reclaim, a crash would orphan the job in `PROCESSING` forever.
- **Cost:** If a worker is merely *stalled* (e.g. a long GC pause beyond the 180 s visibility window), two workers could process the same job. The second commit fails the `PROCESSING → VALIDATING` transition and the result is written once; the price is a duplicate model call. Accepted, and documented.

#### D2. The DLQ is a safety net, not the failure path
- **Decision:**
  - Transient failures (provider overload, deadline, DB or S3 hiccups) put the job back to `STAGED`, add a `RETRY_SCHEDULED` event, and **shorten the message's visibility to a backoff** (30 → 60 → 120 s, cap 15 min). Without that, every retry would wait the full 180 s.
  - On the **last allowed receive** (= the queue's `maxReceiveCount`), the handler marks the job `FAILED` and acks.
  - Permanent failures (bad output, rejected request, missing source) are `FAILED` immediately.
- **Why:** If we let SQS move the message to the DLQ, the job row would stay `STAGED` forever: the database would say "still waiting" about a job that is dead. With this approach the job's state is always the truth. Only messages we can't even parse reach the DLQ, and they're kept there for inspection.
- **Cost:** `worker_max_receives` must match the queue's `maxReceiveCount` in Terraform and bootstrap. Both are 3, and the setting's comment says so.

#### D3. Heartbeat the visibility timeout instead of setting it huge
- **Decision:** 180 s visibility, extended every 60 s while a job runs.
- **Alternatives:** A 30-minute visibility timeout. That's simple, but a crashed worker's job would then wait 30 minutes to be retried.
- **Why:** Long jobs stay protected while crash recovery takes at most one visibility period (~3 min). Live evidence: an overloaded Gemini made one job take 58 s, with no duplicate pickup.

#### D4. NOTIFY is a wake-up, the database is the source of truth
- **Decision:**
  - The worker inserts each event and calls `pg_notify('job_events', {job_id})` **in the same transaction**, so the notification is delivered only if, and when, it commits.
  - Each API process holds **one** LISTEN connection and wakes the matching subscribers.
  - Every SSE stream then reads `job_events WHERE id > last_seen`.
  - Each stream clears its wake flag *before* reading, so a NOTIFY that arrives mid-read triggers another pass.
- **Alternatives:** Redis pub/sub (another component to run), or polling only (simple, but adds up to 15 s of latency and a query per stream per tick).
- **Why:**
  - **Nothing can be lost.** A dropped, coalesced or missed notification only delays the next database read; `Last-Event-ID` resume falls out of the same query.
  - **It degrades gracefully.** If the LISTEN connection drops, streams keep working via the 15 s heartbeat poll, and readiness reports `events: unavailable`.
  - **No new infrastructure.**
- **Evidence:** a test with a **30 s heartbeat** requires the live events within 10 s, so it passes only via NOTIFY. Live: `VALIDATED`/`COMPLETED` arrived the same second the worker committed.
- **Cost:** LISTEN needs a dedicated connection that bypasses pooling and RDS Proxy (ADR-03), and each API task holds one extra Postgres connection.

#### D5. SSE without a library
- **Decision:** A `StreamingResponse` generator:
  1. sends `retry: 3000` as the first byte (TTFB);
  2. replays events after `Last-Event-ID`;
  3. sends a `: keep-alive` comment every 15 s (the ALB idle timeout is 120 s);
  4. closes on `completed`/`failed` or after 10 minutes.

  Starlette cancels the generator when the client disconnects, and the `subscribe()` context manager unregisters it.
- **Why:** About 50 lines, and no dependency to download on a flaky network. Our raw-ASGI middleware (Phase 1, D3) was chosen with exactly this in mind.
- **Limitation:** Browsers' `EventSource` can't send headers, so a job submitted with `X-Client-Id` can't be streamed from a browser. The UI falls back to polling for those. Phase 6 could use short-lived signed stream URLs.

#### D6. Failover needs a time budget (Phase 3 carry-over)
- **Decision:**
  - A model that isn't the last resort gets at most **half the remaining deadline** per attempt.
  - Gemini's 429 hides its retry delay in prose (*"Please retry in 15h59m52s"*), with no `Retry-After` header. We parse it, and if honouring it would overrun the deadline we stop retrying that model at once: fail over, or return **503 + Retry-After** (not a misleading 504).
- **Why:** Live runs showed a hung primary waiting 60 s and wasting free-tier quota on hopeless retries before failover.

### 4.4 Problems we hit

| # | Problem | Root cause | Fix / lesson |
|---|---|---|---|
| P1 | The live async job stayed `STAGED` for 5 minutes | `docker compose build` failed on the network, so the **old Phase 1 worker** (logging placeholder) was still running. It consumed the message and **deleted** it | Rebuilt, recreated, re-verified. **Lessons:** (1) check that a deploy actually rolled, not just that the command ran (we confirmed by importing the new module inside the container); (2) this is exactly the "STAGED with no message" gap the Phase 6 **stuck-job reaper** must close. The orphaned job is still in the local database as a real example. |
| P2 | ruff B023 on the reconnect callback | A closure inside a loop captured `lost` by reference | Bound it as a default argument. Correct here anyway (one connection per iteration), but explicit is safer |
| P3 | Polling `is_closed()` every 5 s to detect a dropped LISTEN connection | Wrong tool | asyncpg's `add_termination_listener`: event-driven, no sleep loop |
| P4 | A test fake's AWS-style parameter names (`QueueUrl`) tripped N803 after reformatting split the line | The formatter moved the `noqa` | Allowed N803 in `tests/**`, since fakes deliberately mirror AWS signatures |

### 4.5 Verification evidence

| Check | Result |
|---|---|
| ruff, `mypy --strict` (57 files) | Clean |
| Tests | **200 passed**, 94 % coverage |
| Worker paths (real Postgres + moto, scripted Gemini) | Complete; duplicate delivery processed once; 503 → requeue (30 s backoff) → `FAILED` on last receive with 6 audit rows; bad output → permanent `FAILED`; dead worker's job reclaimed on redelivery; missing source → `FAILED`; poison body → raises (DLQ) |
| SSE | Replay of a finished job + `result_url`; `Last-Event-ID` resume returns only later events; **live via NOTIFY in < 10 s with a 30 s heartbeat** |
| Runner | `RetryLaterError` reschedules visibility without deleting; heartbeat extends visibility while a job runs |
| Quota and budget | "retry in 15h59m52s" parsed; daily-quota 429 fails over after 1 attempt; 503 (not 504) without a fallback; primary attempt ≤ 50 % of the budget |
| **Live, real stack + real Gemini** | async submit → `GEMINI_PROCESSING` instantly → failover to Flash-Lite → `VALIDATED` + `COMPLETED` pushed live the same second → all fields correct, $0.002386 |
| Live readiness | `{"database":"ok","s3":"ok","events":"ok"}` |

### 4.6 Phase 4 exit criteria

| Criterion | Status |
|---|---|
| SSE emits all PRD stages in order | ✅ `UPLOADED → S3_STAGED → GEMINI_PROCESSING → VALIDATED → COMPLETED` (tests and live) |
| SSE TTFB p95 < 300 ms (100 concurrent streams) | 🟡 First byte is sent before any I/O beyond the job lookup; **not load-tested** (Phase 8 k6) |
| Killing a worker mid-job → no duplicate results, no lost jobs | ✅ Reclaim-on-redelivery test; duplicate-delivery test; at most one result row |
| `Last-Event-ID` replays only missed events | ✅ |

### 4.7 Known limitations carried forward

- **Stuck-job reaper:** a job in `STAGED` with no message (P1 is a real instance) or a crash between commit and publish. Re-enqueue jobs stuck longer than N minutes. **Phase 6.**
- A failed result write re-extracts on redelivery (duplicate cost). Write results to S3 first. **Phase 5.**
- Browser `EventSource` can't carry `X-Client-Id`, so client-scoped jobs use polling. Signed stream URLs are a later option.
- The SSE TTFB and 100-stream targets need load testing. **Phase 8.**
- `worker/__main__.py` wiring is only exercised live, not by unit tests (0 % in coverage).

### 4.8 Likely interview questions, with answers

**"How do you make a queue consumer safe with at-least-once delivery?"**
Claim with compare-and-set: one `UPDATE ... WHERE status='STAGED' RETURNING`. Exactly one consumer wins; a duplicate gets no row and acks. The result is written in the same transaction as the `VALIDATING → COMPLETED` transition, so it can only be written once. A test delivers the same message twice and asserts one result and zero extra model calls.

**"What happens if a worker crashes mid-job?"**
While a job runs, the worker extends the message's visibility every minute. If it dies, the extensions stop and the message reappears within one visibility window, about 3 minutes. The redelivery has a receive count above 1, which is allowed to reclaim a `PROCESSING` job, because the only way that message can come back while the job is still processing is that its owner stopped heartbeating. Without that, a crash would strand the job forever.

**"Why not just let failed messages go to the DLQ?"**
Then the job row would say `STAGED` forever: the database would be lying. The worker tracks attempts through the receive count and, on the last one, marks the job `FAILED` itself and acks, so the database is always the source of truth. The DLQ only catches messages we can't even parse, and keeps them for inspection.

**"How does live progress work without Redis?"**
Postgres `LISTEN/NOTIFY`. The worker inserts the event and notifies in the same transaction, so the notification fires only on commit. Each API process has one listening connection that wakes the right streams. Notifications are just wake-ups: the stream always reads the events table after its last event id, so a lost notification can't lose an event, and `Last-Event-ID` resume is the same query. If the listener dies, streams fall back to a 15-second poll.

**"Prove the live path isn't just polling."**
The integration test sets the heartbeat (the polling interval) to 30 seconds and requires the worker's events to arrive within 10. That only passes if NOTIFY woke the stream.

**"Tell me about a deployment bug."**
During live verification an async job never progressed. The image build had silently failed, so the old placeholder worker was still running, consuming messages and deleting them. Two takeaways:
- Verify that a deploy actually rolled out: I checked by importing the new module inside the running container.
- That failure is precisely the "job stuck in `STAGED` with no message" case. I'd designed a reaper for it, and now I have a concrete reproduction to test it against.

**Numbers to remember:** 180 s visibility, heartbeat every 60 s · backoff 30/60/120 s (cap 15 min) · 3 receives then `FAILED` · 5 Gemini attempts per async job vs 3 sync · 120 s async job deadline · 15 s SSE keep-alive, 10 min stream cap · primary ≤ 50 % of the remaining budget · 200 tests / 94 % coverage.
