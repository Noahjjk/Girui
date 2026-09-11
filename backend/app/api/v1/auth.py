"""认证：登录、刷新、登出、改密、登录设备管理。"""
from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_client_ip, get_device_id, get_user_agent
from app.core.security import (
    check_password_strength,
    create_access_token,
    decode_access_token,
    ensure_utc,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    refresh_token_ttl,
    utcnow,
    verify_password,
)
from app.db.session import get_db
from app.models.enums import LogStatus
from app.models.user import RefreshToken, User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    RefreshRequest,
    SessionInfo,
    TokenResponse,
)
from app.schemas.common import OkResponse
from app.schemas.converters import token_to_session_info, user_to_brief
from app.core.deps import get_current_user
from app.services import audit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["认证"])


async def _issue_tokens(
    db: AsyncSession,
    user: User,
    remember_me: bool,
    request: Request,
) -> TokenResponse:
    access = create_access_token(
        subject=str(user.id),
        extra={"username": user.username, "role": user.role.value if hasattr(user.role, "value") else str(user.role)},
    )
    plain_refresh = generate_refresh_token()
    ttl = refresh_token_ttl(remember_me)

    raw_device_name = request.headers.get("x-device-name")
    device_name = unquote(raw_device_name) if raw_device_name else None

    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_refresh_token(plain_refresh),
            device_id=get_device_id(request),
            device_name=device_name,
            user_agent=get_user_agent(request),
            ip=get_client_ip(request),
            remember_me=remember_me,
            expires_at=utcnow() + ttl,
        )
    )

    return TokenResponse(
        access_token=access,
        refresh_token=plain_refresh,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=user_to_brief(user),
    )


@router.post("/login", response_model=TokenResponse, summary="账号密码登录")
async def login(payload: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = (
        await db.execute(select(User).where(User.username == payload.username))
    ).scalar_one_or_none()

    # 统一失败文案，避免暴露账号是否存在
    generic_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误"
    )

    if user is None:
        await audit.record(
            "login", db=db, request=request, username=payload.username,
            status=LogStatus.FAILED, error="账号不存在",
            detail={"username": payload.username},
        )
        raise generic_error

    now = utcnow()
    if user.locked_until and user.locked_until > now:
        remain = int((user.locked_until - now).total_seconds() // 60) + 1
        await audit.record(
            "login", db=db, request=request, user=user,
            status=LogStatus.DENIED, error="账号锁定中",
        )
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"连续登录失败次数过多，账号已锁定，请 {remain} 分钟后重试",
        )

    if not verify_password(payload.password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= settings.LOGIN_MAX_FAIL:
            user.locked_until = now + timedelta(minutes=settings.LOGIN_LOCK_MINUTES)
            user.failed_login_count = 0
            message = f"连续失败 {settings.LOGIN_MAX_FAIL} 次，账号已锁定 {settings.LOGIN_LOCK_MINUTES} 分钟"
        else:
            left = settings.LOGIN_MAX_FAIL - user.failed_login_count
            message = f"账号或密码错误，还可尝试 {left} 次"
        await audit.record(
            "login", db=db, request=request, user=user,
            status=LogStatus.FAILED, error=message,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=message)

    if not user.is_active:
        await audit.record(
            "login", db=db, request=request, user=user,
            status=LogStatus.DENIED, error="账号已停用",
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被停用，请联系管理员")

    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    user.last_login_ip = get_client_ip(request)

    tokens = await _issue_tokens(db, user, payload.remember_me, request)

    await audit.record(
        "login", db=db, request=request, user=user,
        detail={"remember_me": payload.remember_me, "device": payload.device_name},
    )
    return tokens


@router.post("/refresh", response_model=TokenResponse, summary="用 Refresh Token 换取新的登录态")
async def refresh(payload: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    token_hash = hash_refresh_token(payload.refresh_token)
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()

    if row is None or row.revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录")
    expires_at = ensure_utc(row.expires_at)
    if expires_at is not None and expires_at <= utcnow():
        row.revoked = True
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已过期，请重新登录")

    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        row.revoked = True
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用，请重新登录")

    # 刷新令牌轮转：旧令牌立即作废，降低泄露风险
    row.revoked = True
    row.last_used_at = utcnow()

    tokens = await _issue_tokens(db, user, row.remember_me, request)
    await audit.record("token.refresh", db=db, request=request, user=user)
    return tokens


@router.post("/logout", response_model=OkResponse, summary="登出当前设备")
async def logout(payload: RefreshRequest, request: Request, db: AsyncSession = Depends(get_db)):
    token_hash = hash_refresh_token(payload.refresh_token)
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if row:
        row.revoked = True
    await audit.record("logout", db=db, request=request)
    return OkResponse(message="已登出")


@router.post("/logout-all", response_model=OkResponse, summary="登出全部设备")
async def logout_all(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)
            )
        )
    ).scalars().all()
    for row in rows:
        row.revoked = True
    await audit.record("logout.all", db=db, request=request, user=user, detail={"count": len(rows)})
    return OkResponse(message=f"已登出 {len(rows)} 个设备")


@router.get("/me", summary="获取当前登录用户")
async def me(user: User = Depends(get_current_user)):
    return user_to_brief(user)


@router.post("/change-password", response_model=OkResponse, summary="修改自己的密码")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        await audit.record(
            "password.change", db=db, request=request, user=user,
            status=LogStatus.FAILED, error="原密码错误",
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码错误")

    problem = check_password_strength(payload.new_password)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)

    if verify_password(payload.new_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与原密码相同")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    user.failed_login_count = 0
    user.locked_until = None

    # 改密后吊销其它设备的登录态，当前设备重新登录
    rows = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False)
            )
        )
    ).scalars().all()
    for row in rows:
        row.revoked = True

    await audit.record("password.change", db=db, request=request, user=user)
    return OkResponse(message="密码已修改，请使用新密码重新登录")


@router.get("/sessions", response_model=list[SessionInfo], summary="我登录的设备")
async def my_sessions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.user_id == user.id,
                RefreshToken.revoked.is_(False),
                RefreshToken.expires_at > utcnow(),
            )
            .order_by(RefreshToken.created_at.desc())
        )
    ).scalars().all()
    return [token_to_session_info(r) for r in rows]


@router.delete("/sessions/{session_id}", response_model=OkResponse, summary="下线指定设备")
async def revoke_session(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    row = (
        await db.execute(
            select(RefreshToken).where(
                RefreshToken.id == session_id, RefreshToken.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="设备不存在")
    row.revoked = True
    await audit.record(
        "logout.device", db=db, request=request, user=user,
        resource_type="session", resource_id=session_id,
    )
    return OkResponse(message="该设备已下线")
