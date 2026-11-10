"""Root API router.

The version prefix (``/api/v1``) is applied once by ``create_app`` via
``settings.api_prefix``; sub-routers here carry only their own segments.
"""

from fastapi import APIRouter

from app.api.v1 import api_v1_router

api_router = APIRouter()
api_router.include_router(api_v1_router)
