"""API v1 routers."""

from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.platforms import router as platforms_router

api_v1_router = APIRouter()
api_v1_router.include_router(health_router, prefix="/health")
api_v1_router.include_router(platforms_router)
