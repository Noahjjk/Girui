"""API v1 路由聚合。"""
from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, chat, folders, knowledge, logs, models, permissions, system
from app.api.v1.users import router as users_router, tag_router
from app.core.config import settings

api_router = APIRouter()


@api_router.get("/health", tags=["系统"], summary="健康检查")
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


api_router.include_router(auth.router)
api_router.include_router(users_router)
api_router.include_router(tag_router)
api_router.include_router(knowledge.router)
api_router.include_router(folders.router)
api_router.include_router(permissions.router)
api_router.include_router(chat.router)
api_router.include_router(models.router)
api_router.include_router(logs.router)
api_router.include_router(system.router)
