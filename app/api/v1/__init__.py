"""API v1 routers."""

from fastapi import APIRouter

from app.api.v1.artifacts import artifact_router as artifacts_router
from app.api.v1.artifacts import router as execution_artifacts_router
from app.api.v1.connections import router as connections_router
from app.api.v1.content import router as content_router
from app.api.v1.executions import router as executions_router
from app.api.v1.executions import schedule_router as schedules_router
from app.api.v1.health import router as health_router
from app.api.v1.platforms import router as platforms_router
from app.api.v1.providers import router as providers_router
from app.api.v1.workflows import router as workflows_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router, prefix="/health")
api_v1_router.include_router(platforms_router)
api_v1_router.include_router(connections_router)
api_v1_router.include_router(providers_router)
api_v1_router.include_router(workflows_router)
api_v1_router.include_router(executions_router)
api_v1_router.include_router(schedules_router)
api_v1_router.include_router(execution_artifacts_router)
api_v1_router.include_router(artifacts_router)
api_v1_router.include_router(content_router)
