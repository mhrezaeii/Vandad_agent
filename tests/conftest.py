"""Pytest fixtures for the Vandad Agent test suite.

Environment variables are configured at import time (before any application
import) so every layer — settings, Alembic, app lifespan, dependencies —
points at the isolated test database and test Redis logical DB.

Required services: the dockerized PostgreSQL and Redis from docker-compose.yml.
Override with TEST_DATABASE_URL / TEST_REDIS_URL when needed.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://vandad:change-me-strong-password@localhost:15432/vandad_agent_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://:change-me-redis-password@localhost:6379/1")
# Same server, closed port — used to verify degraded readiness behavior.
from sqlalchemy.engine import make_url as _make_url  # noqa: E402

DEAD_DATABASE_URL = str(_make_url(TEST_DATABASE_URL).set(port=15499))

# Point the whole application at the isolated test stores before app imports.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Deterministic 32-byte master key for tests only (never used outside pytest).
import base64  # noqa: E402
import json  # noqa: E402

_TEST_MASTER_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
TEST_MASTER_KEY_CONFIG = {"1": _TEST_MASTER_KEY}
os.environ["CREDENTIAL_MASTER_KEYS"] = json.dumps(TEST_MASTER_KEY_CONFIG)
os.environ.setdefault("CREDENTIAL_ACTIVE_KEY_VERSION", "1")
os.environ.setdefault("PLATFORM_BOOTSTRAP_COMPANY_IDS", "*")

import httpx  # noqa: E402
import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool  # noqa: E402

from alembic import command  # noqa: E402
from app.api.deps import build_provider_executor  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.redis import RedisConnectionManager  # noqa: E402
from app.integrations.providers import build_default_registry  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security.authorization import Principal  # noqa: E402
from app.security.crypto import MasterKeyBox  # noqa: E402

TABLES = (
    "audit_events",
    "company_agents",
    "connection_events",
    "credential_versions",
    "credential_references",
    "workflow_execution_logs",
    "workflow_execution_steps",
    "workflow_schedules",
    "workflow_edges",
    "workflow_nodes",
    "workflow_versions",
    "workflows",
    "workflow_executions",
    "platform_connections",
    "platforms",
    "companies",
)


# --------------------------------------------------------------------------
# Session-scoped provisioning: create the test database, apply migrations.
# --------------------------------------------------------------------------
async def _ensure_database_exists(admin_url: str, target_db: str) -> bool:
    import asyncpg

    def _sync_url(url: str) -> str:
        return url.replace("+asyncpg", "")

    try:
        conn = await asyncpg.connect(_sync_url(admin_url), timeout=5)
    except Exception:
        return False
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target_db)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{target_db}"')
        return True
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def provisioned_database() -> str:
    """Create the test database if needed and run migrations to head."""
    from sqlalchemy.engine import make_url

    parsed = make_url(TEST_DATABASE_URL)
    target_db = parsed.database
    candidates = [target_db, "vandad_agent", "postgres"]

    created = False
    for admin_db in candidates:
        # render_as_string(hide_password=False): str(url) masks the password.
        admin_url = parsed.set(database=admin_db).render_as_string(hide_password=False)
        try:
            created = asyncio.run(_ensure_database_exists(admin_url, target_db))
        except Exception:  # noqa: BLE001 - try the next candidate database
            created = False
        if created:
            break
    if not created:
        pytest.fail(
            "Could not reach PostgreSQL to provision the test database. "
            "Start the compose services (docker compose up -d postgres redis) "
            "or set TEST_DATABASE_URL."
        )

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(alembic_cfg, "head")
    return TEST_DATABASE_URL


# --------------------------------------------------------------------------
# Per-test infrastructure (function-scoped: no cross-event-loop sharing).
# --------------------------------------------------------------------------
@pytest.fixture
async def engine(provisioned_database) -> AsyncEngine:
    test_engine = create_async_engine(provisioned_database, poolclass=NullPool)
    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def clean_database(engine):
    """Wipe all application tables before the test runs."""
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def session_factory(engine) -> async_sessionmaker:
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def db_session(clean_database, session_factory):
    """Direct database access for assertions; rolled back afterwards."""
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def redis_manager():
    manager = RedisConnectionManager(TEST_REDIS_URL, socket_timeout=3.0, socket_connect_timeout=3.0)
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture
def app(clean_database, engine, session_factory, redis_manager, tmp_path):
    """Application instance wired to test infrastructure (lifespan not run)."""
    application = create_app()
    application.state.engine = engine
    application.state.session_factory = session_factory
    application.state.redis_manager = redis_manager
    application.state.master_key_box = MasterKeyBox.from_settings(get_settings())
    from app.agents import build_default_agent_registry  # noqa: PLC0415

    application.state.agent_registry = build_default_agent_registry()
    application.state.provider_registry = build_default_registry()
    application.state.provider_executor = build_provider_executor(get_settings())
    from app.integrations.video import build_scene_video_registry  # noqa: PLC0415

    application.state.scene_video_registry = build_scene_video_registry()
    application.state.principal = Principal(subject="test-runner", company_ids=frozenset({"*"}))
    from app.core.queue import WorkflowQueue  # noqa: PLC0415
    from app.storage.local import LocalArtifactStorage  # noqa: PLC0415

    application.state.workflow_queue = (
        WorkflowQueue(redis_manager.client) if redis_manager.client is not None else None
    )
    application.state.artifact_storage = LocalArtifactStorage(
        tmp_path / "artifacts", max_size_bytes=4 * 1024 * 1024
    )
    return application


@pytest.fixture
async def workflow_factory(db_session, session_factory):
    """Create (and optionally publish) workflows directly through the service."""

    from app.integrations.providers import build_default_registry
    from app.security.authorization import CompanyScope, Principal
    from app.services.workflow_service import WorkflowService

    principal = Principal(subject="test-runner", company_ids=frozenset({"*"}))

    async def _make(company_id, nodes, edges, *, publish=True, name=None):
        service = WorkflowService(db_session, build_default_registry())
        scope = CompanyScope(company_id=company_id, principal=principal)
        workflow = await service.create_workflow(
            scope=scope,
            name=name or f"wf-{uuid.uuid4().hex[:8]}",
            description=None,
            nodes=nodes,
            edges=edges,
        )
        if publish:
            workflow = await service.publish_workflow(workflow.id, company_id=company_id)
        return workflow

    return _make


@pytest.fixture
async def workflow_queue(redis_manager):
    from app.core.queue import (  # noqa: PLC0415
        DELAYED_KEY,
        MAIN_KEY,
        PROCESSING_KEY,
        WorkflowQueue,
    )

    assert redis_manager.client is not None
    # Isolate queue state per test (Redis persists across tests).
    await redis_manager.client.delete(MAIN_KEY, PROCESSING_KEY, DELAYED_KEY)
    return WorkflowQueue(redis_manager.client)


@pytest.fixture
def key_box():
    from app.security.crypto import MasterKeyBox  # noqa: PLC0415
    from tests.conftest import TEST_MASTER_KEY_CONFIG  # noqa: PLC0415

    return MasterKeyBox(
        {int(v): base64.b64decode(k) for v, k in TEST_MASTER_KEY_CONFIG.items()},
        active_key_version=1,
    )


@pytest.fixture
def engine_pair(key_box):
    """(registry, executor) pair for in-process engine tests."""
    from app.integrations.providers import (  # noqa: PLC0415
        ProviderExecutor,
        RetryPolicy,
        build_default_registry,
    )

    registry = build_default_registry()
    executor = ProviderExecutor(
        default_policy=RetryPolicy(max_attempts=1, backoff_base_seconds=0.0),
        default_timeout_seconds=3.0,
    )
    return registry, executor


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client


@pytest.fixture
def platform_payload() -> dict:
    return {
        "name": "Admin Panel",
        "platform_type": "admin_panel",
        "base_url": "https://admin.example.com",
        "integration_type": "browser_automation",
        "description": "Internal admin panel",
        "configuration": {"login_url": "https://admin.example.com/login"},
    }


@pytest.fixture
async def company_and_platform(db_session):
    """A company with one platform, committed and available for API tests."""
    from app.models import Company, Platform

    company = Company(name="Acme Corp", slug="acme-corp")
    db_session.add(company)
    await db_session.flush()
    platform = Platform(
        company_id=company.id,
        name="Acme Admin",
        slug="acme-admin",
        platform_type="admin_panel",
    )
    db_session.add(platform)
    await db_session.commit()
    return company, platform


@pytest.fixture
def fast_retry_policy():
    """Retry policy with no backoff delay, for fast reliability tests."""
    from app.integrations.providers import RetryPolicy

    return RetryPolicy(max_attempts=3, backoff_base_seconds=0.0, backoff_cap_seconds=0.0)
