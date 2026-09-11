"""账号管理（仅管理员）。含协作者新增、密码管理、标签维护。"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.core.security import check_password_strength, hash_password, utcnow
from app.db.session import get_db
from app.models.enums import ROLE_LABELS, LogStatus, UserRole
from app.models.user import RefreshToken, Tag, User
from app.schemas.common import OkResponse, Page
from app.schemas.converters import user_to_out
from app.schemas.user import (
    ResetPasswordRequest,
    TagCreate,
    TagOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.services import audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["账号管理"])


async def _resolve_tags(db: AsyncSession, names: List[str]) -> List[Tag]:
    """按名称取标签，不存在则自动创建。"""
    cleaned = [n.strip() for n in (names or []) if n and n.strip()]
    if not cleaned:
        return []
    existing = (
        await db.execute(select(Tag).where(Tag.name.in_(cleaned)))
    ).scalars().all()
    mapping = {t.name: t for t in existing}
    result: List[Tag] = []
    for name in cleaned:
        tag = mapping.get(name)
        if tag is None:
            tag = Tag(name=name)
            db.add(tag)
            await db.flush()
            mapping[name] = tag
        result.append(tag)
    return result


async def _get_user(db: AsyncSession, user_id: int) -> User:
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    return user


async def _count_active_admins(db: AsyncSession, exclude_id: Optional[int] = None) -> int:
    stmt = select(func.count(User.id)).where(
        User.role == UserRole.ADMIN, User.is_active.is_(True)
    )
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return int((await db.execute(stmt)).scalar() or 0)


# ---------------------------------------------------------------- 列表与详情


@router.get("", response_model=Page[UserOut], summary="用户列表")
async def list_users(
    keyword: Optional[str] = Query(None, description="按用户名/姓名/邮箱模糊搜索"),
    role: Optional[UserRole] = None,
    is_active: Optional[bool] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    count_stmt = select(func.count(User.id))

    conditions = []
    if keyword:
        like = f"%{keyword.strip()}%"
        conditions.append(
            or_(User.username.ilike(like), User.display_name.ilike(like), User.email.ilike(like))
        )
    if role is not None:
        conditions.append(User.role == role)
    if is_active is not None:
        conditions.append(User.is_active.is_(is_active))

    for cond in conditions:
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        await db.execute(
            stmt.order_by(User.id).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    return Page[UserOut](
        items=[user_to_out(u) for u in rows], total=total, page=page, page_size=page_size
    )


@router.get("/roles", summary="可选角色")
async def list_roles(_: User = Depends(get_current_user)):
    return [
        {
            "value": role.value,
            "label": ROLE_LABELS.get(role, role.value),
            "description": {
                "admin": "全部权限：账号管理、权限分配、知识上传、查看全部日志",
                "collaborator": "被授权后可向指定知识库上传知识，并参与问答",
                "user": "仅可问答，检索范围受管理员授权的知识库与片段限制",
            }[role.value],
        }
        for role in (UserRole.ADMIN, UserRole.COLLABORATOR, UserRole.USER)
    ]


@router.get("/{user_id}", response_model=UserOut, summary="用户详情")
async def get_user(
    user_id: int, _: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    return user_to_out(await _get_user(db, user_id))


# ---------------------------------------------------------------- 增删改


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, summary="新增账号/协作者")
async def create_user(
    payload: UserCreate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已存在")

    problem = check_password_strength(payload.password)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        email=payload.email,
        phone=payload.phone,
        department=payload.department,
        role=payload.role,
        remark=payload.remark,
        must_change_password=payload.must_change_password,
        created_by=admin.id,
    )
    user.tags = await _resolve_tags(db, payload.tags)
    db.add(user)
    await db.flush()

    await audit.record(
        "user.create", db=db, request=request, user=admin,
        resource_type="user", resource_id=user.id, resource_name=user.username,
        detail={"role": payload.role.value, "tags": payload.tags},
    )
    return user_to_out(user)


@router.patch("/{user_id}", response_model=UserOut, summary="修改用户")
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, user_id)
    changed = {}

    if payload.role is not None and payload.role != user.role:
        if user.id == admin.id and payload.role != UserRole.ADMIN:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能降级自己的管理员角色")
        if user.role == UserRole.ADMIN and payload.role != UserRole.ADMIN:
            if await _count_active_admins(db, exclude_id=user.id) == 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="系统必须保留至少一名管理员")
        changed["role"] = f"{user.role.value} -> {payload.role.value}"
        user.role = payload.role

    if payload.is_active is not None and payload.is_active != user.is_active:
        if user.id == admin.id and not payload.is_active:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能停用自己的账号")
        if not payload.is_active and user.role == UserRole.ADMIN:
            if await _count_active_admins(db, exclude_id=user.id) == 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="系统必须保留至少一名启用状态的管理员")
        changed["is_active"] = payload.is_active
        user.is_active = payload.is_active

    for field in ("display_name", "email", "phone", "department", "remark"):
        value = getattr(payload, field)
        if value is not None:
            setattr(user, field, value)

    if payload.tags is not None:
        user.tags = await _resolve_tags(db, payload.tags)
        changed["tags"] = payload.tags

    if not user.is_active:
        # 停用即吊销全部登录态
        rows = (
            await db.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)
                )
            )
        ).scalars().all()
        for row in rows:
            row.revoked = True

    await db.flush()
    await audit.record(
        "user.update", db=db, request=request, user=admin,
        resource_type="user", resource_id=user.id, resource_name=user.username,
        detail=changed,
    )
    return user_to_out(user)


@router.post("/{user_id}/reset-password", response_model=OkResponse, summary="重置密码")
async def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, user_id)
    problem = check_password_strength(payload.new_password)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None

    revoked = 0
    if payload.revoke_sessions:
        rows = (
            await db.execute(
                select(RefreshToken).where(
                    RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)
                )
            )
        ).scalars().all()
        for row in rows:
            row.revoked = True
        revoked = len(rows)

    await audit.record(
        "user.reset_password", db=db, request=request, user=admin,
        resource_type="user", resource_id=user.id, resource_name=user.username,
        detail={"revoked_sessions": revoked},
    )
    return OkResponse(message=f"密码已重置，该用户下次登录需修改密码（已下线 {revoked} 个设备）")


@router.post("/{user_id}/unlock", response_model=OkResponse, summary="解除账号锁定")
async def unlock_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, user_id)
    user.locked_until = None
    user.failed_login_count = 0
    await audit.record(
        "user.unlock", db=db, request=request, user=admin,
        resource_type="user", resource_id=user.id, resource_name=user.username,
    )
    return OkResponse(message="账号已解锁")


@router.delete("/{user_id}", response_model=OkResponse, summary="删除账号")
async def delete_user(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能删除自己的账号")
    if user.role == UserRole.ADMIN and await _count_active_admins(db, exclude_id=user.id) == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="系统必须保留至少一名管理员")

    username = user.username
    await db.delete(user)
    await audit.record(
        "user.delete", db=db, request=request, user=admin,
        resource_type="user", resource_id=user_id, resource_name=username,
    )
    return OkResponse(message=f"账号 {username} 已删除")


# ---------------------------------------------------------------- 标签


tag_router = APIRouter(prefix="/tags", tags=["标签"])


@tag_router.get("", response_model=List[TagOut], summary="标签列表")
async def list_tags(_: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Tag).order_by(Tag.name))).scalars().all()
    return [TagOut(id=t.id, name=t.name, description=t.description) for t in rows]


@tag_router.post("", response_model=TagOut, status_code=status.HTTP_201_CREATED, summary="新建标签")
async def create_tag(
    payload: TagCreate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(Tag).where(Tag.name == payload.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="标签已存在")
    tag = Tag(name=payload.name, description=payload.description)
    db.add(tag)
    await db.flush()
    await audit.record(
        "tag.create", db=db, request=request, user=admin,
        resource_type="tag", resource_id=tag.id, resource_name=tag.name,
    )
    return TagOut(id=tag.id, name=tag.name, description=tag.description)


@tag_router.delete("/{tag_id}", response_model=OkResponse, summary="删除标签")
async def delete_tag(
    tag_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="标签不存在")
    name = tag.name
    await db.delete(tag)
    await audit.record(
        "tag.delete", db=db, request=request, user=admin,
        resource_type="tag", resource_id=tag_id, resource_name=name,
    )
    return OkResponse(message="标签已删除")
