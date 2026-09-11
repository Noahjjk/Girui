"""大模型管理：增删改、连通性自检、默认模型切换。"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import encrypt
from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.enums import PROVIDER_LABELS, LogStatus, ProviderKind
from app.models.llm import ModelProvider
from app.models.user import User
from app.schemas.chat import (
    ProviderCreate,
    ProviderOut,
    ProviderTestResult,
    ProviderUpdate,
)
from app.schemas.common import OkResponse
from app.schemas.converters import provider_to_out
from app.services import audit
from app.services.llm import LLMError, build_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/models", tags=["模型管理"])


async def _get_provider(db: AsyncSession, provider_id: int) -> ModelProvider:
    row = (
        await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="模型配置不存在")
    return row


@router.get("/kinds", summary="可选供应商类型")
async def list_kinds(_: User = Depends(get_current_user)):
    return [
        {
            "value": kind.value,
            "label": PROVIDER_LABELS.get(kind, kind.value),
            "default_base_url": {
                ProviderKind.DEEPSEEK.value: "https://api.deepseek.com/v1",
                ProviderKind.QWEN.value: "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ProviderKind.OPENAI.value: "https://api.openai.com/v1",
                ProviderKind.VLLM.value: "http://127.0.0.1:8001/v1",
                ProviderKind.OLLAMA.value: "http://127.0.0.1:11434/v1",
                ProviderKind.OPENAI_COMPATIBLE.value: "",
            }.get(kind.value, ""),
        }
        for kind in ProviderKind
    ]


@router.get("/available", summary="问答页可选的模型（普通用户）")
async def available_models(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.execute(
            select(ModelProvider)
            .where(ModelProvider.enabled.is_(True))
            .order_by(ModelProvider.sort_order, ModelProvider.id)
        )
    ).scalars().all()
    default_id = next((r.id for r in rows if r.is_default), rows[0].id if rows else None)
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "model_name": r.model_name,
                "provider": r.provider.value if hasattr(r.provider, "value") else str(r.provider),
                "supports_vision": r.supports_vision,
                "is_default": r.id == default_id,
                "remark": r.remark,
            }
            for r in rows
        ],
        "default_id": default_id,
    }


@router.get("", response_model=List[ProviderOut], summary="模型配置列表")
async def list_providers(
    _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    rows = (
        await db.execute(
            select(ModelProvider).order_by(ModelProvider.sort_order, ModelProvider.id)
        )
    ).scalars().all()
    return [provider_to_out(r) for r in rows]


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED, summary="新增模型配置")
async def create_provider(
    payload: ProviderCreate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = ModelProvider(
        name=payload.name,
        provider=payload.provider,
        base_url=payload.base_url.rstrip("/"),
        model_name=payload.model_name,
        api_key_enc=encrypt(payload.api_key or ""),
        supports_vision=payload.supports_vision,
        supports_stream=payload.supports_stream,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        top_p=payload.top_p,
        timeout_seconds=payload.timeout_seconds,
        extra=payload.extra or {},
        enabled=payload.enabled,
        is_default=False,
        sort_order=payload.sort_order,
        remark=payload.remark,
        created_by=admin.id,
    )
    db.add(row)
    await db.flush()

    if payload.is_default:
        await _set_default(db, row.id)

    await audit.record(
        "model.create", db=db, request=request, user=admin,
        resource_type="model", resource_id=row.id, resource_name=row.name,
        detail={
            "provider": row.provider.value if hasattr(row.provider, "value") else str(row.provider),
            "base_url": row.base_url, "model_name": row.model_name,
        },
    )
    return provider_to_out(row)


@router.patch("/{provider_id}", response_model=ProviderOut, summary="修改模型配置")
async def update_provider(
    provider_id: int,
    payload: ProviderUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_provider(db, provider_id)

    for field in (
        "name", "provider", "model_name", "supports_vision", "supports_stream",
        "max_tokens", "temperature", "top_p", "timeout_seconds", "extra",
        "enabled", "sort_order", "remark",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)

    if payload.base_url is not None:
        row.base_url = payload.base_url.rstrip("/")
    if payload.api_key is not None:
        row.api_key_enc = encrypt(payload.api_key)

    await db.flush()

    if payload.is_default:
        await _set_default(db, row.id)
    elif payload.is_default is False and row.is_default:
        row.is_default = False

    await audit.record(
        "model.update", db=db, request=request, user=admin,
        resource_type="model", resource_id=row.id, resource_name=row.name,
        detail=payload.model_dump(exclude_unset=True, exclude={"api_key"}),
    )
    return provider_to_out(row)


@router.post("/{provider_id}/default", response_model=OkResponse, summary="设为默认模型")
async def set_default(
    provider_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_provider(db, provider_id)
    if not row.enabled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请先启用该模型再设为默认")
    await _set_default(db, row.id)
    await audit.record(
        "model.set_default", db=db, request=request, user=admin,
        resource_type="model", resource_id=row.id, resource_name=row.name,
    )
    return OkResponse(message=f"已将「{row.name}」设为默认模型")


@router.post("/{provider_id}/test", response_model=ProviderTestResult, summary="测试模型连通性")
async def test_provider(
    provider_id: int,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_provider(db, provider_id)
    client = build_client(row)
    try:
        result = await client.test()
    except LLMError as exc:
        return ProviderTestResult(success=False, message=str(exc))
    return ProviderTestResult(
        success=True,
        latency_ms=result.latency_ms,
        reply=(result.content or "").strip()[:200],
        message="连接正常",
    )


@router.delete("/{provider_id}", response_model=OkResponse, summary="删除模型配置")
async def delete_provider(
    provider_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    row = await _get_provider(db, provider_id)
    remaining = (
        await db.execute(
            select(ModelProvider.id).where(ModelProvider.id != provider_id)
        )
    ).scalars().all()
    if not remaining:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="至少需要保留一个模型配置")

    name = row.name
    was_default = row.is_default
    await db.delete(row)
    await db.flush()

    if was_default:
        fallback = (
            await db.execute(
                select(ModelProvider).order_by(ModelProvider.sort_order, ModelProvider.id).limit(1)
            )
        ).scalar_one_or_none()
        if fallback:
            fallback.is_default = True
            await db.flush()

    await audit.record(
        "model.delete", db=db, request=request, user=admin,
        resource_type="model", resource_id=provider_id, resource_name=name,
    )
    return OkResponse(message=f"模型「{name}」已删除")


async def _set_default(db: AsyncSession, provider_id: int) -> None:
    await db.execute(update(ModelProvider).values(is_default=False))
    await db.execute(
        update(ModelProvider).where(ModelProvider.id == provider_id).values(is_default=True)
    )
    await db.flush()
