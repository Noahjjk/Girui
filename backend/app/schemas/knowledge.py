"""知识库、文档、权限、片段 ACL 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from app.models.enums import AclEffect, AclSubjectType, DocumentStatus, Visibility
from app.schemas.common import ORMModel

# ---------------------------------------------------------------- 知识库


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    code: Optional[str] = Field(None, max_length=64)
    description: Optional[str] = None
    embedding_model: str = "bge-m3"
    chunk_method: str = "naive"


class KnowledgeBaseUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = None
    embedding_model: Optional[str] = None
    chunk_method: Optional[str] = None
    is_active: Optional[bool] = None


class KnowledgeBaseOut(ORMModel):
    id: int
    name: str
    code: Optional[str] = None
    description: Optional[str] = None
    ragflow_dataset_id: Optional[str] = None
    embedding_model: str
    chunk_method: str
    is_active: bool
    doc_count: int
    chunk_count: int
    created_at: datetime
    # 当前请求者对该库的权限（由服务层填充）
    my_can_read: bool = False
    my_can_upload: bool = False
    my_can_manage: bool = False


# ---------------------------------------------------------------- 权限


class PermissionGrant(BaseModel):
    user_id: int
    can_read: bool = True
    can_upload: bool = False
    can_manage: bool = False
    note: Optional[str] = Field(None, max_length=255)


class PermissionBatchGrant(BaseModel):
    user_ids: List[int]
    can_read: bool = True
    can_upload: bool = False
    can_manage: bool = False


class PermissionOut(ORMModel):
    id: int
    user_id: int
    kb_id: int
    can_read: bool
    can_upload: bool
    can_manage: bool
    note: Optional[str] = None
    # 由服务层补全
    username: Optional[str] = None
    display_name: Optional[str] = None
    role: Optional[str] = None


# ---------------------------------------------------------------- 文档


class DocumentOut(ORMModel):
    id: int
    kb_id: int
    ragflow_document_id: str
    name: str
    file_type: Optional[str] = None
    size_bytes: int
    category: Optional[str] = None
    source: str
    original_url: Optional[str] = None
    visibility: Visibility
    tags: List[str] = Field(default_factory=list)
    chunk_count: int
    token_count: int
    progress: float
    status: DocumentStatus
    error_message: Optional[str] = None
    uploaded_by: Optional[int] = None
    remark: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _defaults(self):
        if self.tags is None:
            self.tags = []
        return self


class DocumentUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=512)
    category: Optional[str] = Field(None, max_length=64)
    visibility: Optional[Visibility] = None
    tags: Optional[List[str]] = None
    original_url: Optional[str] = Field(None, max_length=1024)
    remark: Optional[str] = None
    folder_id: Optional[int] = Field(None, description="移动到指定文件夹；0 表示移回根目录")


class UploadResult(BaseModel):
    success: bool
    document_id: Optional[int] = None
    ragflow_document_id: Optional[str] = None
    name: str
    message: Optional[str] = None


class BatchUploadResult(BaseModel):
    total: int
    succeeded: int
    failed: int
    results: List[UploadResult]


# ---------------------------------------------------------------- 片段 ACL


class ChunkAclCreate(BaseModel):
    subject_type: AclSubjectType
    subject_id: str = Field(..., max_length=64, description="用户ID / 角色名 / 标签名")
    kb_id: int
    ragflow_document_id: Optional[str] = Field(None, max_length=64)
    chunk_id: Optional[str] = Field(None, max_length=64)
    effect: AclEffect = AclEffect.ALLOW
    note: Optional[str] = Field(None, max_length=255)


class ChunkAclOut(BaseModel):
    id: int
    subject_type: AclSubjectType
    subject_id: str
    kb_id: int
    ragflow_document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    effect: AclEffect
    scope: str
    note: Optional[str] = None
    created_at: Optional[str] = None


class ChunkPreview(BaseModel):
    """从 RAGFlow 拉取的片段预览，用于管理员挑选要授权的片段。"""

    chunk_id: str
    document_id: str
    document_name: Optional[str] = None
    content: str
    index: Optional[int] = None
    token_count: Optional[int] = None
    visible_to_me: bool = True


class EffectivePermission(BaseModel):
    """诊断用：某用户对某知识库的最终可见范围。"""

    user_id: int
    username: str
    kb_id: int
    kb_name: str
    can_read: bool
    can_upload: bool
    can_manage: bool
    visible_document_ids: List[str]
    denied_document_ids: List[str]
    allowed_chunk_count: int
    denied_chunk_count: int
    detail: Dict[str, Any] = Field(default_factory=dict)
