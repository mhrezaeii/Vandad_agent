# Vandad Agent

Vandad Agent is the backend foundation for a multi-company operations and content-production platform. It is an API-first Python service with a separate Persian-first Next.js operator console under `web/`.

## Current stack

- Python 3.12+, FastAPI, Pydantic v2
- PostgreSQL 16, SQLAlchemy async, Alembic
- Redis-backed workflow queue and worker
- Provider adapters for OpenAI-compatible LLM APIs, browser automation, HeyGen and scene-video APIs
- APIMaster scene-video adapter (Wan video models), generic HTTP scene-video adapter and mock provider
- AI-provider-native video generation, editing/composition and tenant-scoped artifact storage
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
7. **Video pipeline** — scenario/storyboard handoff to AI video providers, provider-side video generation/composition, quality checks and artifact delivery.

The implementation has both a workflow execution lifecycle and an agent-task lifecycle. They are not interchangeable: workflow executions run validated workflow graphs; agent tasks represent higher-level agent requests. The feature stack includes a PostgreSQL transactional outbox, dedicated agent worker, code-owned capability router, durable task events, Monitoring / Content scenario handlers and an approval-gated Video Production Agent. The intended product flow is platform monitoring → Company Brain → GPT-based scenario → storyboard/video prompt → AI video provider → quality control → final video delivery. The Video Production Agent must use one unified AI video production path; there is no character-generation or talking-avatar product workflow. Paid dispatch requires a company budget. Aggregate end-to-end validation remains a release gate. See [Agent Execution Runtime](docs/AGENT_EXECUTION_RUNTIME.md) for runtime guarantees and remaining production gates.

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
| Content | `/content` | Brand, scenario, storyboard and video project workflows |
| Artifacts | `/artifacts` | Tenant-scoped artifact metadata and delivery |

Company-scoped endpoints use the `X-Company-ID` boundary. Cross-company resource access must remain denied without leaking resource existence.

## AI-native video production

Vandad uses one unified AI-only video production path: GPT-assisted scenario and storyboard preparation are handed to an AI video provider, which generates the video. Motion graphics, product demonstrations, interface showcases and cinematic product videos are visual styles within this path. Character generation, persistent brand characters and talking-avatar production are out of scope.

- **AI provider output:** when a provider returns a complete video, Vandad
  validates the media response and stores that artifact directly.
- **Provider-side generation/composition:** a provider may return a complete
  video directly or generate clips and compose them through its own explicit
  AI capability. Vandad does not perform creative local video assembly.
- **APIMaster:** supports scene generation; it is not treated as a final
  composition provider unless that capability is explicitly implemented.
- **Generic HTTP:** provider-side composition is enabled only when an
  authorised composition endpoint is configured with `SCENE_VIDEO_HTTP_COMPOSE_PATH`.
- **Mock:** produces synthetic, non-playable fixtures for automated tests and
  local development only; production rejects it.

Creative video production does not require character-generation systems,
FFmpeg, FFprobe, local TTS, local lip-sync or traditional editing software. Storage, integrity checks and metadata
handling remain ordinary backend responsibilities.
