"""认证相关模型。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.enums import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
    remember_me: bool = True
    device_id: Optional[str] = Field(None, max_length=128)
    device_name: Optional[str] = Field(None, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str
    device_id: Optional[str] = Field(None, max_length=128)


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class UserBrief(ORMModel):
    id: int
    username: str
    display_name: str
    role: UserRole
    email: Optional[str] = None
    department: Optional[str] = None
    is_active: bool
    must_change_password: bool = False
    tags: List[str] = Field(default_factory=list)
    last_login_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserBrief


class SessionInfo(ORMModel):
    """已登录设备，用于后台「登录设备管理」。"""

    id: int
    device_id: Optional[str] = None
    device_name: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    remember_me: bool
    last_used_at: Optional[datetime] = None
    expires_at: datetime
    created_at: datetime
