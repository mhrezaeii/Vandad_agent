# Vandad Agent — Backend

Foundation for an internal Autonomous Company Operations & Marketing Agent platform.
Phase 1: FastAPI + PostgreSQL + Redis + n8n on Docker Compose, with platform
registry CRUD and audit logging.

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

## Layout

```
app/
  api/          routers + request-scoped dependencies (unit of work)
  common/       framework-independent helpers (slugify)
  core/         config, logging, middleware, exceptions, redis manager
  database/     engine/session factories, declarative base
  models/       SQLAlchemy ORM models
  repositories/ data access (no commits here)
  schemas/      Pydantic request/response models
  security/     secret-reference validation, redaction helpers
  services/     business logic (platform, audit, readiness)
  workers/      (empty in phase 1 — task queue layer comes later)
  integrations/ (n8n client arrives with the orchestration phase)
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
