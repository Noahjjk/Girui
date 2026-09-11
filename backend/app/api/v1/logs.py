"""操作日志查询。日志表只追加，此处仅提供读取接口。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.audit import OperationLog
from app.models.enums import LogStatus, UserRole
from app.models.user import User
from app.schemas.common import Page
from app.schemas.system import LogOut, LogStats

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/logs", tags=["操作日志"])


@router.get("", response_model=Page[LogOut], summary="操作日志列表（管理员可见全部，其他用户仅本人）")
async def list_logs(
    keyword: Optional[str] = None,
    action: Optional[str] = None,
    log_status: Optional[LogStatus] = Query(None, alias="status"),
    user_id: Optional[int] = None,
    days: int = Query(7, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conditions = [OperationLog.created_at >= datetime.utcnow() - timedelta(days=days)]

    if user.role != UserRole.ADMIN:
        conditions.append(OperationLog.user_id == user.id)
    elif user_id:
        conditions.append(OperationLog.user_id == user_id)

    if action:
        conditions.append(OperationLog.action == action)
    if log_status is not None:
        conditions.append(OperationLog.status == log_status)
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(
            OperationLog.resource_name.ilike(like)
            | OperationLog.username.ilike(like)
            | OperationLog.path.ilike(like)
        )

    total = int(
        (await db.execute(select(func.count(OperationLog.id)).where(*conditions))).scalar() or 0
    )
    rows = (
        await db.execute(
            select(OperationLog)
            .where(*conditions)
            .order_by(OperationLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return Page[LogOut](
        items=[LogOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.get("/actions", response_model=List[str], summary="已出现过的动作类型")
async def list_actions(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    stmt = select(OperationLog.action).distinct().order_by(OperationLog.action)
    if user.role != UserRole.ADMIN:
        stmt = stmt.where(OperationLog.user_id == user.id)
    return list((await db.execute(stmt)).scalars().all())


@router.get("/stats", response_model=LogStats, summary="日志统计")
async def log_stats(
    days: int = Query(7, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    since = datetime.utcnow() - timedelta(days=days)
    base = [OperationLog.created_at >= since]
    if user.role != UserRole.ADMIN:
        base.append(OperationLog.user_id == user.id)

    total = int(
        (await db.execute(select(func.count(OperationLog.id)).where(*base))).scalar() or 0
    )
    by_status = (
        await db.execute(
            select(OperationLog.status, func.count(OperationLog.id))
            .where(*base)
            .group_by(OperationLog.status)
        )
    ).all()
    status_map = {
        (s.value if hasattr(s, "value") else str(s)): int(c) for s, c in by_status
    }

    by_action = (
        await db.execute(
            select(OperationLog.action, func.count(OperationLog.id))
            .where(*base)
            .group_by(OperationLog.action)
            .order_by(func.count(OperationLog.id).desc())
            .limit(15)
        )
    ).all()

    by_user = (
        await db.execute(
            select(OperationLog.username, func.count(OperationLog.id))
            .where(*base, OperationLog.username.isnot(None))
            .group_by(OperationLog.username)
            .order_by(func.count(OperationLog.id).desc())
            .limit(15)
        )
    ).all()

    return LogStats(
        total=total,
        success=status_map.get("success", 0),
        failed=status_map.get("failed", 0),
        denied=status_map.get("denied", 0),
        by_action=[{"action": a, "count": int(c)} for a, c in by_action],
        by_user=[{"username": u, "count": int(c)} for u, c in by_user],
    )
