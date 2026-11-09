# Vandad Agent — Backend

Foundation for an internal Autonomous Company Operations & Marketing Agent platform.
Phase 1 delivered FastAPI + PostgreSQL + Redis + n8n on Docker Compose with the
platform registry, provider execution and workflow engine. Phase 4 adds the
first real content pipeline: a persistent Persian AI avatar and a branded
1080x1920 video produced through HeyGen and FFmpeg.

## Stack

- Python 3.12+, FastAPI (async), Pydantic v2
- PostgreSQL 16 (SQLAlchemy 2.x async, asyncpg, Alembic)
- Redis 7 (async connection manager)
- n8n (community edition, persisted in the shared Postgres)
- Docker Compose, Pytest

## Quick start

```bash
cp .env.example .env          # then edit every change-me value
docker compose up -d --build  # api + postgres + redis + n8n
```

- API:        http://localhost:8000/api/v1/health  (docs at /docs)
- n8n:        http://localhost:5678  (create the owner account on first visit)
- Migrations run automatically when the api container starts
  (`alembic upgrade head`).

### Local (host) development

```bash
docker compose up -d postgres redis
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

## API surface (Phase 1)

| Method | Path                             | Purpose                        |
|--------|----------------------------------|--------------------------------|
| GET    | `/api/v1/health`                 | Liveness (no dependency calls) |
| GET    | `/api/v1/health/ready`           | Readiness (DB + Redis checks)  |
| GET    | `/api/v1/platforms`              | List platforms (pagination)    |
| POST   | `/api/v1/platforms`              | Create platform                |
| GET    | `/api/v1/platforms/{id}`         | Retrieve platform              |
| PATCH  | `/api/v1/platforms/{id}`         | Partial update                 |
| DELETE | `/api/v1/platforms/{id}`         | Delete platform                |

Errors use one envelope: `{"error": {"code", "message", "details?", "correlation_id"}}`.
Every response carries an `X-Request-ID` correlation id (or pass your own).

## Content pipeline (Phase 4)

The pipeline is deliberately brand-agnostic. Vandefence is seeded as the first
brand, but every step reads its identity from `content_brands` +
`content_brand_characters` and its look-and-feel from `brand.visual_config`.
All endpoints live under `/api/v1/content` and are tenant-scoped by the
`X-Company-ID` header (cross-tenant reads return `404`, never `403`).

| Method | Path                                              | Purpose                                         |
|--------|---------------------------------------------------|-------------------------------------------------|
| GET    | `/content/brands`                                 | List brands (bootstraps Vandefence on first call) |
| GET    | `/content/brands/{id}/character`                  | Read the brand's persistent character           |
| POST   | `/content/brands/{id}/character`                  | Create the character once (paid HeyGen training) |
| POST   | `/content/brands/{id}/character/refresh`          | Poll avatar training and persist the avatar id  |
| POST   | `/content/brands/{id}/character/status`           | Activate / deactivate the character             |
| POST   | `/content/projects`                               | Create a video project (topic only)             |
| GET    | `/content/projects/{id}`                          | Read project state, script, cost and artifacts  |
| POST   | `/content/projects/{id}/generate-script`          | Start the LLM script workflow                   |
| POST   | `/content/projects/{id}/approve-script`           | Human approval gate before any paid video call  |
| POST   | `/content/projects/{id}/generate-video`           | Start the HeyGen + FFmpeg workflow              |
| GET    | `/content/jobs/{id}`                              | Poll a workflow job / execution status          |
| GET    | `/content/videos`                                 | List completed videos for the company           |
| GET    | `/content/videos/{id}`                            | Read one completed video                        |
| GET    | `/content/videos/{id}/content`                    | Stream the final MP4 (no storage paths exposed) |

### Flow

1. `GET /content/brands` bootstraps the Vandefence brand for the company.
2. Create the persistent character once from a source portrait. HeyGen trains a
   photo avatar; `refresh` polls until the look is `completed` and stores its id.
   Only a `not_created` or `failed` character may start (or restart) the paid
   training flow — an active character is reactivated, never re-created.
3. Create a project, run `generate-script`, review the output, then
   `approve-script`. Video generation refuses to run without approval.
4. `generate-video` drives the `content.*` workflow nodes:
   `submit_video` (idempotent paid HeyGen submission) → `poll_video` →
   `download_video` → `render_video` (FFmpeg subtitles + branding).
5. Poll `GET /content/jobs/{id}` until the execution completes, then stream the
   finished MP4 from `/content/videos/{id}/content`.

### Idempotency and cost honesty

- Paid calls send a stable `Idempotency-Key` (`vandad-video-{project.id}`,
  `vandad-avatar-{character.id}`) so retries and duplicate submissions never
  charge twice. A `409 request_in_progress` is treated as transient.
- LLM cost is recorded only when the provider reports it (for example OpenRouter
  `usage.cost`). HeyGen does not report a per-video price, so `cost_metadata`
  records duration and artifact metadata with `actual_cost_available: false`.
  Prices are never invented.

### Video rendering

`VideoRenderService` normalizes the raw HeyGen output to the brand's
`video_width` x `video_height` (1080x1920 by default), burns Persian RTL
subtitles (correct shaping, `Vazirmatn`), optionally concatenates intro/outro,
overlays the logo, mixes background music and applies loudness normalization.
Every stage runs in an isolated temp directory that is removed afterwards.

| Setting                   | Default            | Purpose                                             |
|---------------------------|--------------------|-----------------------------------------------------|
| `VIDEO_FONTS_DIR`         | `/usr/share/fonts` | Font directory for libass (falls back to fontconfig) |
| `VIDEO_FONT_NAME`         | `Vazirmatn`        | Subtitle font family                                |
| `VIDEO_FFMPEG_BIN`        | `ffmpeg`           | FFmpeg binary                                       |
| `VIDEO_FFPROBE_BIN`       | `ffprobe`          | FFprobe binary                                      |

The Docker runtime installs `ffmpeg` and `fonts-vazirmatn`; node-level
`fonts_dir`, `font_name`, `ffmpeg_bin` and `ffprobe_bin` override the defaults.

## Layout

```
app/
  api/          routers + request-scoped dependencies (unit of work)
  common/       framework-independent helpers (slugify)
  core/         config, logging, middleware, exceptions, redis manager, queue
  database/     engine/session factories, declarative base
  integrations/ provider adapters (HeyGen, OpenAI-compatible LLM) + registry/executor
  models/       SQLAlchemy ORM models (incl. content brands/characters/projects)
  repositories/ data access (no commits here)
  schemas/      Pydantic request/response models
  security/     secret references, encryption, redaction, authorization
  services/     business logic (content, video render, subtitles, artifacts, ...)
  storage/      artifact storage backends (local filesystem)
  workflows/    engine, node registry, content nodes
  workers/      queue consumer process
alembic/        migrations
tests/          pytest suite (needs dockerized postgres+redis running)
```

## Tests

```bash
docker compose up -d postgres redis
.venv/Scripts/python -m pytest
```

Tests run against an isolated `vandad_agent_test` database (created and
migrated automatically) and Redis DB 1. Override with `TEST_DATABASE_URL` /
`TEST_REDIS_URL`.

## Notes

- Credentials live only in `.env` (never committed). The database stores
  *references* to secrets (e.g. `vault://...`), never secrets themselves.
- Docker Desktop was configured with `ProxyHTTPMode: manual` (no proxy) during
  setup because the system proxy at `127.0.0.1:10808` was offline; a backup of
  the original settings is at `%APPDATA%\Docker\settings-store.json.bak-vandad`.
