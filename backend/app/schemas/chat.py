"""问答与模型供应商模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.models.enums import MessageRole, ProviderKind
from app.schemas.common import ORMModel

# ---------------------------------------------------------------- 问答


class Attachment(BaseModel):
    type: str = Field(..., pattern="^(image|file)$")
    name: str
    path: Optional[str] = None        # 服务端相对路径，上传接口返回
    mime: Optional[str] = None
    size: Optional[int] = None
    text: Optional[str] = None        # 文本类附件已抽取的内容


class ChatRequest(BaseModel):
    question: str = Field("", max_length=20000)
    session_id: Optional[int] = None
    kb_ids: Optional[List[int]] = None            # 不传则用会话已选范围，再退化为用户全部可读库
    model_provider_id: Optional[int] = None       # 不传则用默认模型
    attachments: List[Attachment] = Field(default_factory=list)
    top_n: Optional[int] = Field(None, ge=1, le=20)
    stream: bool = True
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)


class Citation(BaseModel):
    index: int
    chunk_id: str
    document_id: str
    document_name: str
    kb_id: Optional[int] = None
    kb_name: Optional[str] = None
    content: str
    similarity: float = 0.0
    original_url: Optional[str] = None
    author: Optional[str] = None
    page: Optional[int] = None


class MessageOut(ORMModel):
    id: int
    session_id: int
    role: MessageRole
    content: str
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    attachments: List[Dict[str, Any]] = Field(default_factory=list)
    model_name: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    feedback: Optional[str] = None
    created_at: datetime


class SessionOut(ORMModel):
    id: int
    title: str
    model_provider_id: Optional[int] = None
    model_name: Optional[str] = None
    kb_ids: List[int] = Field(default_factory=list)
    message_count: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class SessionDetail(SessionOut):
    messages: List[MessageOut] = Field(default_factory=list)


class SessionCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    kb_ids: Optional[List[int]] = None
    model_provider_id: Optional[int] = None


class SessionUpdate(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    kb_ids: Optional[List[int]] = None
    model_provider_id: Optional[int] = None
    is_archived: Optional[bool] = None


class FeedbackRequest(BaseModel):
    feedback: Optional[str] = Field(None, pattern="^(up|down)$")


class RetrievalPreviewRequest(BaseModel):
    """不调用大模型，仅看检索与权限过滤结果，便于调试知识库覆盖情况。"""

    question: str
    kb_ids: Optional[List[int]] = None
    top_n: int = Field(8, ge=1, le=50)


# ---------------------------------------------------------------- 模型供应商


class ProviderOut(ORMModel):
    id: int
    name: str
    provider: ProviderKind
    base_url: str
    model_name: str
    api_key_masked: Optional[str] = None
    has_api_key: bool = False
    supports_vision: bool
    supports_stream: bool
    max_tokens: int
    temperature: float
    top_p: float
    timeout_seconds: int
    extra: Optional[Dict[str, Any]] = None
    enabled: bool
    is_default: bool
    sort_order: int
    remark: Optional[str] = None
    created_at: datetime


class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    provider: ProviderKind = ProviderKind.OPENAI_COMPATIBLE
    base_url: str = Field(..., min_length=1, max_length=512)
    model_name: str = Field(..., min_length=1, max_length=128)
    api_key: Optional[str] = None
    supports_vision: bool = False
    supports_stream: bool = True
    max_tokens: int = Field(4096, ge=1, le=200000)
    temperature: float = Field(0.3, ge=0.0, le=2.0)
    top_p: float = Field(0.9, ge=0.0, le=1.0)
    timeout_seconds: int = Field(120, ge=5, le=1800)
    extra: Optional[Dict[str, Any]] = None
    enabled: bool = True
    is_default: bool = False
    sort_order: int = 0
    remark: Optional[str] = None


class ProviderUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    provider: Optional[ProviderKind] = None
    base_url: Optional[str] = Field(None, max_length=512)
    model_name: Optional[str] = Field(None, max_length=128)
    api_key: Optional[str] = None      # 传空字符串表示清除，None 表示不改
    supports_vision: Optional[bool] = None
    supports_stream: Optional[bool] = None
    max_tokens: Optional[int] = Field(None, ge=1, le=200000)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    timeout_seconds: Optional[int] = Field(None, ge=5, le=1800)
    extra: Optional[Dict[str, Any]] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    sort_order: Optional[int] = None
    remark: Optional[str] = None


class ProviderTestResult(BaseModel):
    success: bool
    latency_ms: Optional[int] = None
    reply: Optional[str] = None
    message: Optional[str] = None
