# Vandad Agent

Vandad Agent is the backend foundation for a multi-company operations and content-production platform. It is an API-first Python service with a separate Persian-first Next.js operator console under `web/`.

## Current stack

- Python 3.12+, FastAPI, Pydantic v2
- PostgreSQL 16, SQLAlchemy async, Alembic
- Redis-backed workflow queue and worker
- Provider adapters for OpenAI-compatible LLM APIs, browser automation, HeyGen and scene-video APIs
- APIMaster scene-video adapter (Wan video models), generic HTTP scene-video adapter and mock provider
- FFmpeg-based video rendering and local artifact storage
- Next.js 15 + React 19 + TypeScript frontend, Docker Compose, pytest and Ruff

## Start locally

```powershell
Copy-Item .env.example .env
# Edit .env and replace every required change-me value.
docker compose up -d --build
```

- API health: `http://localhost:8000/api/v1/health`
- OpenAPI: `http://localhost:8000/docs`
- PostgreSQL and Redis are required for normal API/worker operation.
- The API and worker run Alembic migrations through the container startup configuration. Start the optional UI profile with `docker compose --profile ui up -d --build web`; the UI is available at `http://localhost:3000`.

For host frontend development:

```powershell
cd web
Copy-Item .env.example .env.local
npm install
npm run dev
```

For host backend development:

```powershell
docker compose up -d postgres redis
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\alembic upgrade head
.venv\Scripts\uvicorn app.main:app --reload
```

Run tests:

```powershell
docker compose up -d postgres redis
.venv\Scripts\python -m ruff check app tests
.venv\Scripts\python -m pytest -q
```

The test suite expects an isolated test database and Redis DB 1. See `.github/workflows/backend-tests.yml` for the CI environment.

## Product architecture

The backend is organized around these boundaries:

1. **API and tenant security** — FastAPI routes, company scope, request correlation and authorization.
2. **Company context** — connected platforms, source documents, extracted platform knowledge and brand-specific context.
3. **Agent catalog** — code-owned agent definitions and tenant-level enable/disable state.
4. **Task lifecycle** — company-scoped task requests, idempotency, cancellation, retries and immutable run history.
5. **Workflow runtime** — validated workflow graphs, persisted executions, Redis queue, worker, retries and schedules.
6. **Provider fabric** — provider registry and adapters; credentials are resolved through the secret boundary.
7. **Media pipeline** — brand character management, scene generation, assembly, Persian subtitles and artifact delivery.

The implementation has both a workflow execution lifecycle and an agent-task lifecycle. They are not interchangeable: workflow executions run validated workflow graphs; agent tasks represent higher-level agent requests. The feature stack includes a PostgreSQL transactional outbox, dedicated agent worker, code-owned capability router, durable task events, Monitoring / Content scenario handlers and an approval-gated Video Production Agent. The video handler routes to the existing avatar or scene workflow; paid dispatch requires a company budget. Aggregate end-to-end validation remains a release gate. See [Agent Execution Runtime](docs/AGENT_EXECUTION_RUNTIME.md) for runtime guarantees and remaining production gates.

See [Architecture and implementation status](docs/ARCHITECTURE.md).

## API areas

All routes are under `/api/v1`. The main route groups are:

| Area | Route prefix | Purpose |
|---|---|---|
| Health | `/health` | Liveness and readiness |
| Platforms | `/platforms` | Platform registry |
| Platform discovery | `/platform-discovery` | Discover sources and extract platform knowledge |
| Connections | `/connections` | Tenant-owned platform connections |
| Providers | `/providers` | Provider metadata, scene-video capabilities, company budgets and usage ledger |
| Workflows | `/workflows` | Workflow definitions and versions |
| Executions | `/executions` | Workflow execution and schedules |
| Agents | `/agents` | Agent catalog and tenant agent activation |
| Agent tasks | `/agent-tasks` | Task requests, approval, cancellation, retry, run history and SSE progress events |
| Content | `/content` | Brand, character and video project workflows |
| Artifacts | `/artifacts` | Tenant-scoped artifact metadata and delivery |

Company-scoped endpoints use the `X-Company-ID` boundary. Cross-company resource access must remain denied without leaking resource existence.

## Video production: two distinct paths

These paths solve different jobs and should remain separate behind a common orchestration layer.

### 1. Talking-avatar video

The local renderer in `scripts/create_video.py` uses Persian Piper TTS, Wav2Lip and FFmpeg. It is intended for a persistent character speaking a generated script. The runtime assets and model weights are local setup artifacts; they must not be committed to Git.

### 2. Scene-based video

The scene pipeline creates a storyboard and generates individual scenes through a configured scene-video provider, then assembles and renders the result. Available adapters include:

- **APIMaster** — configured through `SCENE_VIDEO_APIMASTER_*`; current default model is `wan3.0-video-prime`.
- **Generic HTTP** — for a compatible asynchronous video API with explicitly configured paths and download host allowlist.
- **Mock** — for tests and local development only; production rejects it.

APIMaster is a scene-video provider, not automatically an LLM provider. The OpenAI-compatible LLM adapter is a separate integration and can target compatible endpoints when the provider exposes the expected chat-completions contract.

APIMaster scene generation does not provide lip-sync. For a speaking character, generate speech and use the talking-avatar path or a provider that explicitly supports that capability; do not assume a scene-generation model can animate accurate dialogue.

## Provider and cost policy

- Provider selection is explicit through configuration; unavailable providers fail closed.
- Keep API credentials in `.env` or the configured secret store, never in source code.
- Record provider-reported usage/cost only. Do not infer or fabricate cost when a provider omits it.
- Before enabling a paid provider in production, validate its endpoint contract, output retrieval, download allowlist, timeout behavior and retry/idempotency behavior.
- Add new providers as isolated adapters; do not add vendor-specific branches to workflow business logic.

## Deployment notes

Docker Compose includes API, workflow worker, agent worker, PostgreSQL, Redis and an n8n service; the Next.js UI is an optional `ui` profile. The in-repository workflow engine and worker are the execution path implemented by this codebase. n8n should be treated as an optional/legacy integration until its intended responsibility is confirmed; do not remove its service, volumes or configuration until local deployment usage has been checked.

Artifacts are stored through the configured artifact storage backend and shared between API and worker containers. Never expose internal filesystem paths through public API responses.

## Repository map

```
web/                Next.js + TypeScript operator console
app/
  agents/          code-owned agent catalog and registry
  api/             FastAPI routers and request dependencies
  core/            settings, logging, middleware, queue and Redis
  database/        SQLAlchemy engine/session setup
  integrations/    platform discovery and provider adapters
  models/          SQLAlchemy models
  repositories/    data access
  schemas/         API request/response schemas
  security/        authorization, encryption, secret handling, redaction
  services/        business services
  storage/         artifact storage backends
  workers/         background workflow and agent-task workers
  workflows/       workflow graph validation, mapping, engine and nodes
alembic/            database migrations (append-only history)
scripts/            local utilities, including talking-avatar renderer setup
tests/              automated tests
docs/               architecture and implementation status
```

## Safety rules for repository maintenance

- Do not delete or rewrite applied Alembic migrations.
- Do not commit `.env`, API keys, runtime model weights, generated media, local databases, virtual environments or caches.
- Do not remove the existing talking-avatar renderer just because the scene-video pipeline exists; they have different responsibilities.
- Do not mark an agent dispatch-ready until its handler, authorization boundary, retries, cancellation and tests are implemented.
- Keep the UI as a separate frontend application consuming stable, versioned API contracts. Production UI/API access must be behind the trusted OIDC identity proxy and role contract in [Production identity proxy](docs/DEPLOYMENT_AUTH.md); never expose the local bootstrap principal publicly.
