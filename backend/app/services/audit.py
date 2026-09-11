"""审计日志。

需求原文：「系统完整记录操作日志」。因此所有写操作与问答均落库，
日志表只追加、不提供删除接口。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_client_ip, get_device_id, get_user_agent
from app.db.session import SessionLocal
from app.models.audit import OperationLog
from app.models.enums import LogStatus
from app.models.user import User

logger = logging.getLogger(__name__)


async def record(
    action: str,
    *,
    db: Optional[AsyncSession] = None,
    user: Optional[User] = None,
    request: Optional[Request] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[Any] = None,
    resource_name: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
    status: LogStatus = LogStatus.SUCCESS,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    username: Optional[str] = None,
) -> None:
    """写一条操作日志。

    传入 db 时复用调用方事务；不传则自建会话并立即提交
    （流式响应场景下请求级会话可能已关闭，此时必须自建）。
    """
    entry = OperationLog(
        user_id=user.id if user else None,
        username=username or (user.username if user else None),
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        resource_name=resource_name,
        detail=_sanitize(detail or {}),
        method=request.method if request else None,
        path=str(request.url.path) if request else None,
        ip=get_client_ip(request) if request else None,
        user_agent=get_user_agent(request) if request else None,
        device_id=get_device_id(request) if request else None,
        status=status,
        error=(error or "")[:2000] or None,
        duration_ms=duration_ms,
    )

    if db is not None:
        db.add(entry)
        return

    try:
        async with SessionLocal() as session:
            session.add(entry)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        # 审计失败不能影响主流程
        logger.warning("写入操作日志失败 action=%s: %s", action, exc)


SENSITIVE_KEYS = {
    "password", "old_password", "new_password", "api_key", "token",
    "refresh_token", "access_token", "secret", "authorization",
}


def _sanitize(detail: Dict[str, Any]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key, value in detail.items():
        if key.lower() in SENSITIVE_KEYS:
            cleaned[key] = "***"
        elif isinstance(value, str) and len(value) > 2000:
            cleaned[key] = value[:2000] + "…"
        elif isinstance(value, dict):
            cleaned[key] = _sanitize(value)
        else:
            cleaned[key] = value
    return cleaned
