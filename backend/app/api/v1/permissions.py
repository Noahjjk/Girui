"""知识隔离权限管理：知识库授权 + 片段级黑白名单。"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.enums import AclSubjectType, UserRole
from app.models.knowledge import ChunkAcl, KbDocument, KbPermission, KnowledgeBase
from app.models.user import User
from app.schemas.common import OkResponse
from app.schemas.converters import acl_to_out, permission_to_out
from app.schemas.knowledge import (
    ChunkAclCreate,
    ChunkAclOut,
    EffectivePermission,
    PermissionBatchGrant,
    PermissionGrant,
    PermissionOut,
)
from app.services import audit
from app.services.permission import PermissionService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb", tags=["权限管理"])


async def _get_kb(db: AsyncSession, kb_id: int) -> KnowledgeBase:
    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    return kb


async def _assert_manage(db: AsyncSession, user: User, kb_id: int) -> None:
    await PermissionService(db).assert_kb_manage(user, kb_id)


# ---------------------------------------------------------------- 知识库级授权


@router.get("/{kb_id}/permissions", response_model=List[PermissionOut], summary="知识库授权列表")
async def list_permissions(
    kb_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    rows = (
        await db.execute(select(KbPermission).where(KbPermission.kb_id == kb_id))
    ).scalars().all()
    users = {
        u.id: u
        for u in (
            await db.execute(select(User).where(User.id.in_([r.user_id for r in rows] or [-1])))
        ).scalars().all()
    }
    return [permission_to_out(r, users.get(r.user_id)) for r in rows]


@router.get("/{kb_id}/permissions/candidates", summary="尚未授权的候选用户")
async def permission_candidates(
    kb_id: int,
    keyword: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    granted = {
        r.user_id
        for r in (
            await db.execute(select(KbPermission).where(KbPermission.kb_id == kb_id))
        ).scalars().all()
    }
    stmt = select(User).where(User.is_active.is_(True))
    if keyword:
        like = f"%{keyword.strip()}%"
        from sqlalchemy import or_

        stmt = stmt.where(or_(User.username.ilike(like), User.display_name.ilike(like)))
    rows = (await db.execute(stmt.order_by(User.id))).scalars().all()

    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "role": u.role.value if hasattr(u.role, "value") else str(u.role),
            "department": u.department,
            "tags": u.tag_names,
            "granted": u.id in granted,
        }
        for u in rows
    ]


@router.post("/{kb_id}/permissions", response_model=PermissionOut, summary="授权单个用户")
async def grant_permission(
    kb_id: int,
    payload: PermissionGrant,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    target = (await db.execute(select(User).where(User.id == payload.user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标用户不存在")

    row = (
        await db.execute(
            select(KbPermission).where(
                KbPermission.kb_id == kb_id, KbPermission.user_id == payload.user_id
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = KbPermission(kb_id=kb_id, user_id=payload.user_id, granted_by=user.id)
        db.add(row)

    row.can_read = payload.can_read
    row.can_upload = payload.can_upload
    row.can_manage = payload.can_manage
    row.note = payload.note
    await db.flush()

    await audit.record(
        "perm.grant", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb_id, resource_name=target.username,
        detail={
            "kb_id": kb_id, "user_id": payload.user_id,
            "can_read": payload.can_read, "can_upload": payload.can_upload,
            "can_manage": payload.can_manage,
        },
    )
    return permission_to_out(row, target)


@router.post("/{kb_id}/permissions/batch", response_model=OkResponse, summary="批量授权")
async def grant_permissions_batch(
    kb_id: int,
    payload: PermissionBatchGrant,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    if not payload.user_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未选择用户")

    existing = {
        r.user_id: r
        for r in (
            await db.execute(
                select(KbPermission).where(
                    KbPermission.kb_id == kb_id, KbPermission.user_id.in_(payload.user_ids)
                )
            )
        ).scalars().all()
    }

    for uid in payload.user_ids:
        row = existing.get(uid)
        if row is None:
            row = KbPermission(kb_id=kb_id, user_id=uid, granted_by=user.id)
            db.add(row)
        row.can_read = payload.can_read
        row.can_upload = payload.can_upload
        row.can_manage = payload.can_manage

    await db.flush()
    await audit.record(
        "perm.grant_batch", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb_id,
        detail={
            "user_ids": payload.user_ids, "can_read": payload.can_read,
            "can_upload": payload.can_upload, "can_manage": payload.can_manage,
        },
    )
    return OkResponse(message=f"已为 {len(payload.user_ids)} 位用户更新授权")


@router.delete("/{kb_id}/permissions/{user_id}", response_model=OkResponse, summary="撤销授权")
async def revoke_permission(
    kb_id: int,
    user_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    row = (
        await db.execute(
            select(KbPermission).where(KbPermission.kb_id == kb_id, KbPermission.user_id == user_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="授权记录不存在")

    await db.delete(row)
    # 同时清理该用户在知识库内的片段级规则，避免残留导致越权
    acls = (
        await db.execute(
            select(ChunkAcl).where(
                ChunkAcl.kb_id == kb_id,
                ChunkAcl.subject_type == AclSubjectType.USER,
                ChunkAcl.subject_id == str(user_id),
            )
        )
    ).scalars().all()
    for acl in acls:
        await db.delete(acl)

    await audit.record(
        "perm.revoke", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb_id,
        detail={"user_id": user_id, "removed_acls": len(acls)},
    )
    return OkResponse(message=f"已撤销该用户对本知识库的访问权限（清理片段规则 {len(acls)} 条）")


# ---------------------------------------------------------------- 片段级 ACL


@router.get("/{kb_id}/acls", response_model=List[ChunkAclOut], summary="片段级权限规则列表")
async def list_acls(
    kb_id: int,
    document_id: Optional[str] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    stmt = select(ChunkAcl).where(ChunkAcl.kb_id == kb_id)
    if document_id:
        stmt = stmt.where(ChunkAcl.ragflow_document_id == document_id)
    rows = (await db.execute(stmt.order_by(ChunkAcl.id.desc()))).scalars().all()
    return [acl_to_out(r) for r in rows]


@router.post("/{kb_id}/acls", response_model=ChunkAclOut, status_code=status.HTTP_201_CREATED, summary="新增片段级权限规则")
async def create_acl(
    kb_id: int,
    payload: ChunkAclCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)

    if payload.kb_id != kb_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="路径与请求体中的知识库不一致")
    if payload.ragflow_document_id:
        exists = (
            await db.execute(
                select(KbDocument.id).where(
                    KbDocument.kb_id == kb_id,
                    KbDocument.ragflow_document_id == payload.ragflow_document_id,
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="指定的文档不属于该知识库")

    row = ChunkAcl(
        subject_type=payload.subject_type,
        subject_id=str(payload.subject_id),
        kb_id=kb_id,
        ragflow_document_id=payload.ragflow_document_id,
        chunk_id=payload.chunk_id,
        effect=payload.effect,
        note=payload.note,
        created_by=user.id,
    )
    db.add(row)
    await db.flush()

    await audit.record(
        "acl.create", db=db, request=request, user=user,
        resource_type="acl", resource_id=row.id,
        detail={
            "kb_id": kb_id, "scope": row.scope, "subject": f"{payload.subject_type.value}:{payload.subject_id}",
            "effect": payload.effect.value, "chunk_id": payload.chunk_id,
            "document_id": payload.ragflow_document_id,
        },
    )
    return acl_to_out(row)


@router.post("/{kb_id}/acls/batch", response_model=OkResponse, summary="批量为多个片段授权")
async def create_acls_batch(
    kb_id: int,
    request: Request,
    payload: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """payload: {subject_type, subject_id, effect, document_id, chunk_ids:[], note}

    管理员在片段列表里勾选若干片段一次性放开给某人，是最高频的操作。
    """
    await _assert_manage(db, user, kb_id)

    chunk_ids = payload.get("chunk_ids") or []
    if not chunk_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未选择任何片段")

    subject_type = AclSubjectType(payload.get("subject_type", "user"))
    created = 0
    for chunk_id in chunk_ids:
        db.add(
            ChunkAcl(
                subject_type=subject_type,
                subject_id=str(payload["subject_id"]),
                kb_id=kb_id,
                ragflow_document_id=payload.get("document_id"),
                chunk_id=str(chunk_id),
                effect=payload.get("effect", "allow"),
                note=payload.get("note"),
                created_by=user.id,
            )
        )
        created += 1

    await db.flush()
    await audit.record(
        "acl.create_batch", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb_id,
        detail={
            "subject": f"{subject_type.value}:{payload.get('subject_id')}",
            "document_id": payload.get("document_id"),
            "chunk_count": created,
            "effect": payload.get("effect", "allow"),
        },
    )
    return OkResponse(message=f"已创建 {created} 条片段级规则")


@router.delete("/{kb_id}/acls/{acl_id}", response_model=OkResponse, summary="删除片段级权限规则")
async def delete_acl(
    kb_id: int,
    acl_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    row = (
        await db.execute(select(ChunkAcl).where(ChunkAcl.id == acl_id, ChunkAcl.kb_id == kb_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="规则不存在")

    detail = row.to_dict()
    await db.delete(row)
    await audit.record(
        "acl.delete", db=db, request=request, user=user,
        resource_type="acl", resource_id=acl_id, detail=detail,
    )
    return OkResponse(message="规则已删除")


@router.delete("/{kb_id}/acls", response_model=OkResponse, summary="清空某文档的全部片段规则")
async def clear_acls(
    kb_id: int,
    request: Request,
    document_id: str = Query(..., description="文档 ID"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    rows = (
        await db.execute(
            select(ChunkAcl).where(
                ChunkAcl.kb_id == kb_id, ChunkAcl.ragflow_document_id == document_id
            )
        )
    ).scalars().all()
    for row in rows:
        await db.delete(row)
    await audit.record(
        "acl.clear", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb_id,
        detail={"document_id": document_id, "removed": len(rows)},
    )
    return OkResponse(message=f"已清空 {len(rows)} 条规则")


# ---------------------------------------------------------------- 权限诊断


@router.get("/{kb_id}/diagnose/{user_id}", summary="诊断某用户对该知识库的实际可见范围")
async def diagnose(
    kb_id: int,
    user_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    target = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="目标用户不存在")

    perm = PermissionService(db)
    cap = await perm.kb_capabilities(target, kb_id)
    detail = await perm.diagnose(target, kb_id)
    kb = await _get_kb(db, kb_id)

    return {
        "user_id": target.id,
        "username": target.username,
        "kb_id": kb_id,
        "kb_name": kb.name,
        "can_read": cap.can_read,
        "can_upload": cap.can_upload,
        "can_manage": cap.can_manage,
        "visible_document_ids": detail["visible_documents"],
        "denied_document_ids": detail["hidden_documents"],
        "allowed_chunk_count": len(detail["allowed_chunk_rules"]),
        "denied_chunk_count": len(detail["denied_chunk_rules"]),
        "detail": detail,
    }


@router.get("/{kb_id}/my-visibility", summary="查看我自己的可见范围")
async def my_visibility(
    kb_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    perm = PermissionService(db)
    await perm.assert_kb_read(user, kb_id)
    cap = await perm.kb_capabilities(user, kb_id)
    detail = await perm.diagnose(user, kb_id)
    kb = await _get_kb(db, kb_id)
    return {
        "user_id": user.id,
        "username": user.username,
        "kb_id": kb_id,
        "kb_name": kb.name,
        "can_read": cap.can_read,
        "can_upload": cap.can_upload,
        "can_manage": cap.can_manage,
        "visible_document_ids": detail["visible_documents"],
        "denied_document_ids": detail["hidden_documents"],
        "allowed_chunk_count": len(detail["allowed_chunk_rules"]),
        "denied_chunk_count": len(detail["denied_chunk_rules"]),
        "detail": detail,
    }
