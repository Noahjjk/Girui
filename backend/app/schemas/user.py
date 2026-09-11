"""用户与标签模型。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import ORMModel


class TagOut(ORMModel):
    id: int
    name: str
    description: Optional[str] = None


class TagCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: Optional[str] = Field(None, max_length=255)


class UserOut(ORMModel):
    id: int
    username: str
    display_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    department: Optional[str] = None
    role: UserRole
    is_active: bool
    must_change_password: bool
    locked_until: Optional[datetime] = None
    failed_login_count: int = 0
    last_login_at: Optional[datetime] = None
    last_login_ip: Optional[str] = None
    remark: Optional[str] = None
    created_at: datetime
    tags: List[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _tags(cls, v):
        if v is None:
            return []
        if isinstance(v, list) and v and hasattr(v[0], "name"):
            return [t.name for t in v]
        return v


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=64)
    email: Optional[str] = Field(None, max_length=128)
    phone: Optional[str] = Field(None, max_length=32)
    department: Optional[str] = Field(None, max_length=64)
    role: UserRole = UserRole.USER
    tags: List[str] = Field(default_factory=list)
    remark: Optional[str] = None
    must_change_password: bool = False


class UserUpdate(BaseModel):
    display_name: Optional[str] = Field(None, max_length=64)
    email: Optional[str] = Field(None, max_length=128)
    phone: Optional[str] = Field(None, max_length=32)
    department: Optional[str] = Field(None, max_length=64)
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    tags: Optional[List[str]] = None
    remark: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=8, max_length=128)
    revoke_sessions: bool = True
