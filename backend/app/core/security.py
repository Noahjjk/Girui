"""密码哈希、JWT 签发校验、Refresh Token 生成。"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import bcrypt
import jwt

from app.core.config import settings

# ---------------------------------------------------------------- 密码

BCRYPT_ROUNDS = 12
MIN_PASSWORD_LENGTH = 8


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def check_password_strength(password: str) -> Optional[str]:
    """返回错误说明；合规返回 None。"""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"密码长度不得少于 {MIN_PASSWORD_LENGTH} 位"
    if password.isdigit() or password.isalpha():
        return "密码需同时包含字母与数字"
    if re.search(r"\s", password):
        return "密码不得包含空白字符"
    return None


# ---------------------------------------------------------------- Access Token

def create_access_token(
    subject: str,
    extra: Optional[Dict[str, Any]] = None,
    expires_minutes: Optional[int] = None,
) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload: Dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "typ": "access",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[Dict[str, Any]]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "access":
        return None
    return payload


# ---------------------------------------------------------------- Refresh Token

def generate_refresh_token() -> str:
    """交给客户端的明文令牌，仅此一次可见。"""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """入库的哈希值。Refresh Token 本身是高熵随机串，SHA-256 足够且可快速比对。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_ttl(remember_me: bool) -> timedelta:
    if remember_me:
        return timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    return timedelta(hours=settings.REFRESH_TOKEN_EXPIRE_HOURS_SHORT)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime | None) -> datetime | None:
    """确保 datetime 对象带有 UTC 时区信息。SQLite 存储时会丢失时区信息，读出时为 naive datetime。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

