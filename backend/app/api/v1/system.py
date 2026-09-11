"""系统信息、健康状态与桌面端版本迭代。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.chat import ChatMessage, ChatSession
from app.models.knowledge import KbDocument, KnowledgeBase
from app.models.system import AppVersion
from app.models.user import User
from app.schemas.common import OkResponse, Page
from app.schemas.system import (
    SystemInfo,
    UpdateCheckResponse,
    VersionCreate,
    VersionOut,
)
from app.services import audit
from app.services.embedding import embedding_available
from app.services.retriever import RetrievalError, retrieval_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system", tags=["系统"])


def _parse_version(value: str) -> tuple:
    """把 1.2.3 / v1.2.3-beta 解析为可比较元组。"""
    cleaned = (value or "").strip().lstrip("vV").split("-")[0]
    parts = []
    for token in cleaned.split("."):
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


@router.get("/info", response_model=SystemInfo, summary="系统概览")
async def system_info(
    _: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    user_count = int((await db.execute(select(func.count(User.id)))).scalar() or 0)
    kb_count = int((await db.execute(select(func.count(KnowledgeBase.id)))).scalar() or 0)
    doc_count = int((await db.execute(select(func.count(KbDocument.id)))).scalar() or 0)
    chunk_count = int(
        (await db.execute(select(func.coalesce(func.sum(KbDocument.chunk_count), 0)))).scalar() or 0
    )

    retrieval_ok = False
    retrieval_ver = None
    try:
        retrieval_ok = await retrieval_client.ping()
        retrieval_ver = await retrieval_client.version()
    except RetrievalError:
        retrieval_ok = False

    # 本地内核的嵌入是进程内的 ONNX 推理，没有独立服务可以探活，
    # 因此用「模型能否加载」等价表达嵌入是否可用。首次调用会加载模型
    # （约 0.4 秒），之后命中缓存；放到线程里跑避免阻塞事件循环。
    if settings.use_local_retrieval:
        embedding_ok = await asyncio.to_thread(embedding_available)
    else:
        # 外部内核自带嵌入模型，跟随其连通状态即可
        embedding_ok = retrieval_ok

    return SystemInfo(
        app_name=settings.APP_NAME,
        app_version=settings.APP_VERSION,
        api_version="v1",
        server_time=datetime.now(timezone.utc),
        retrieval_connected=retrieval_ok,
        retrieval_version=retrieval_ver,
        embedding_connected=embedding_ok,
        embedding_model=settings.EMBEDDING_MODEL,
        embedding_dim=settings.EMBEDDING_DIM,
        database_ok=True,
        user_count=user_count,
        kb_count=kb_count,
        document_count=doc_count,
        chunk_count=chunk_count,
    )


@router.get("/health", summary="健康检查（无需登录）")
async def health():
    return {"status": "ok", "app": settings.APP_NAME, "version": settings.APP_VERSION}


# ---------------------------------------------------------------- 版本迭代


@router.get("/versions", response_model=Page[VersionOut], summary="版本清单")
async def list_versions(
    platform: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    conditions = []
    if platform:
        conditions.append(AppVersion.platform == platform)
    total = int(
        (await db.execute(select(func.count(AppVersion.id)).where(*conditions))).scalar() or 0
    )
    rows = (
        await db.execute(
            select(AppVersion)
            .where(*conditions)
            .order_by(AppVersion.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return Page[VersionOut](
        items=[VersionOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post("/versions", response_model=VersionOut, status_code=status.HTTP_201_CREATED, summary="发布新版本")
async def create_version(
    payload: VersionCreate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = AppVersion(
        platform=payload.platform,
        version=payload.version,
        min_supported_version=payload.min_supported_version,
        notes=payload.notes,
        release_notes_url=payload.release_notes_url,
        download_url=payload.download_url,
        sha512=payload.sha512,
        file_size=payload.file_size,
        mandatory=payload.mandatory,
        published=payload.published,
        released_at=payload.released_at or datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    await audit.record(
        "version.publish", db=db, request=request, user=admin,
        resource_type="version", resource_id=row.id, resource_name=row.version,
        detail={"platform": row.platform, "mandatory": row.mandatory},
    )
    return VersionOut.model_validate(row)


@router.delete("/versions/{version_id}", response_model=OkResponse, summary="下架版本")
async def delete_version(
    version_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(select(AppVersion).where(AppVersion.id == version_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="版本不存在")
    version = row.version
    await db.delete(row)
    await audit.record(
        "version.delete", db=db, request=request, user=admin,
        resource_type="version", resource_id=version_id, resource_name=version,
    )
    return OkResponse(message=f"版本 {version} 已下架")


@router.get("/updates/check", response_model=UpdateCheckResponse, summary="桌面端检查更新")
async def check_update(
    current_version: str = Query(..., description="客户端当前版本"),
    platform: str = Query("windows"),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(AppVersion)
            .where(AppVersion.platform == platform, AppVersion.published.is_(True))
            .order_by(AppVersion.id.desc())
            .limit(50)
        )
    ).scalars().all()

    if not rows:
        return UpdateCheckResponse(has_update=False, current_version=current_version)

    latest = max(rows, key=lambda r: _parse_version(r.version))
    current = _parse_version(current_version)
    target = _parse_version(latest.version)

    has_update = target > current
    mandatory = latest.mandatory or (
        bool(latest.min_supported_version)
        and current < _parse_version(latest.min_supported_version)
    )

    return UpdateCheckResponse(
        has_update=has_update,
        current_version=current_version,
        latest_version=latest.version,
        mandatory=mandatory,
        notes=latest.notes,
        download_url=latest.download_url if has_update else None,
        sha512=latest.sha512 if has_update else None,
        file_size=latest.file_size if has_update else None,
    )


@router.get("/updates/desktop/latest.yml", response_class=PlainTextResponse, summary="更新源清单")
async def update_manifest(
    db: AsyncSession = Depends(get_db),
):
    """electron-updater 的 generic 协议入口。

    客户端会请求 <feedUrl>/latest.yml，本接口动态生成。
    """
    rows = (
        await db.execute(
            select(AppVersion)
            .where(AppVersion.platform == "windows", AppVersion.published.is_(True))
            .order_by(AppVersion.id.desc())
            .limit(50)
        )
    ).scalars().all()

    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="暂无可用版本")

    latest = max(rows, key=lambda r: _parse_version(r.version))
    filename = latest.download_url.rstrip("/").split("/")[-1]
    release_date = (latest.released_at or latest.created_at).astimezone(timezone.utc)
    notes = (latest.notes or "").strip()

    lines = [
        f"version: {latest.version}",
        "files:",
        f"  - url: {filename}",
        f"    sha512: {latest.sha512 or ''}",
        f"    size: {latest.file_size}",
        f"path: {filename}",
        f"sha512: {latest.sha512 or ''}",
        f"releaseDate: '{release_date.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3]}Z'",
    ]
    if notes:
        lines.append("releaseNotes: |")
        lines.extend(f"  {line}" for line in notes.splitlines())

    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/yaml")


@router.get("/updates/desktop/{filename}", summary="更新包下载重定向")
async def download_release(filename: str, db: AsyncSession = Depends(get_db)):
    """把更新包文件名映射到配置的下载地址，避免把存储路径暴露给客户端。"""
    rows = (
        await db.execute(
            select(AppVersion)
            .where(AppVersion.platform == "windows", AppVersion.published.is_(True))
            .order_by(AppVersion.id.desc())
            .limit(50)
        )
    ).scalars().all()
    for row in rows:
        if row.download_url.rstrip("/").endswith(filename):
            return {"url": row.download_url, "version": row.version, "size": row.file_size}
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")


@router.get("/stats/mine", summary="我的使用统计")
async def my_stats(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    session_count = int(
        (
            await db.execute(
                select(func.count(ChatSession.id)).where(ChatSession.user_id == user.id)
            )
        ).scalar() or 0
    )
    message_count = int(
        (
            await db.execute(
                select(func.count(ChatMessage.id))
                .join(ChatSession, ChatSession.id == ChatMessage.session_id)
                .where(ChatSession.user_id == user.id)
            )
        ).scalar() or 0
    )
    return {"session_count": session_count, "message_count": message_count}
