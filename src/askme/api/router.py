"""统一管理 API 路由。"""

from fastapi import APIRouter

from askme.api.routes.health import router as health_router
from askme.api.routes.admin import router as admin_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(admin_router)
