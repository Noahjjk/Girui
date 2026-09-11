"""操作日志、版本迭代、系统信息模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.enums import LogStatus
from app.schemas.common import ORMModel


class LogOut(ORMModel):
    id: int
    created_at: datetime
    user_id: Optional[int] = None
    username: Optional[str] = None
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None
    method: Optional[str] = None
    path: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    status: LogStatus
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class LogStats(BaseModel):
    total: int
    success: int
    failed: int
    denied: int
    by_action: List[Dict[str, Any]] = Field(default_factory=list)
    by_user: List[Dict[str, Any]] = Field(default_factory=list)


class VersionCreate(BaseModel):
    platform: str = "windows"
    version: str = Field(..., max_length=32)
    min_supported_version: Optional[str] = Field(None, max_length=32)
    notes: Optional[str] = None
    release_notes_url: Optional[str] = Field(None, max_length=512)
    download_url: str = Field(..., max_length=512)
    sha512: Optional[str] = Field(None, max_length=128)
    file_size: int = 0
    mandatory: bool = False
    published: bool = True
    released_at: Optional[datetime] = None


class VersionOut(ORMModel):
    id: int
    platform: str
    version: str
    min_supported_version: Optional[str] = None
    notes: Optional[str] = None
    release_notes_url: Optional[str] = None
    download_url: str
    sha512: Optional[str] = None
    file_size: int
    mandatory: bool
    published: bool
    released_at: Optional[datetime] = None
    created_at: datetime


class UpdateCheckResponse(BaseModel):
    """桌面端检查更新时使用的响应（兼容 electron-updater 的 generic 协议）。"""

    has_update: bool
    current_version: Optional[str] = None
    latest_version: Optional[str] = None
    mandatory: bool = False
    notes: Optional[str] = None
    download_url: Optional[str] = None
    sha512: Optional[str] = None
    file_size: Optional[int] = None


class SystemInfo(BaseModel):
    app_name: str
    app_version: str
    api_version: str
    server_time: datetime
    # 检索内核连通性。字段名刻意不带具体产品名：内核可插拔（内置轻量内核 / 外部服务），
    # 前端只关心「通不通」，不该关心是谁在背后做事。
    retrieval_connected: bool
    retrieval_version: Optional[str] = None
    embedding_connected: bool
    embedding_model: str
    embedding_dim: int
    database_ok: bool
    user_count: int
    kb_count: int
    document_count: int
    chunk_count: int
