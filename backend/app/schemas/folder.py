"""知识库文件夹（多级嵌套）的请求/响应模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.models.enums import AclEffect, AclSubjectType, Visibility
from app.schemas.common import ORMModel


class FolderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    parent_id: Optional[int] = Field(None, description="父文件夹 ID；为空表示建在知识库根下")
    visibility: Visibility = Visibility.PUBLIC
    description: Optional[str] = None
    sort_order: int = 0


class FolderUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    parent_id: Optional[int] = Field(None, description="改父级即移动；传 0 表示移到根下")
    visibility: Optional[Visibility] = None
    description: Optional[str] = None
    sort_order: Optional[int] = None
    # 用于区分「没传 parent_id」和「显式要求移到根下」
    move: bool = Field(False, description="是否执行移动操作")


class FolderOut(ORMModel):
    id: int
    kb_id: int
    parent_id: Optional[int] = None
    name: str
    path: str
    depth: int
    sort_order: int
    visibility: Visibility
    description: Optional[str] = None
    created_at: Optional[str] = None
    # 服务层补全
    doc_count: int = 0
    child_count: int = 0
    acl_count: int = 0
    my_visible: bool = True


class FolderNode(FolderOut):
    """树形节点。"""

    children: List["FolderNode"] = Field(default_factory=list)


FolderNode.model_rebuild()


class FolderTreeOut(BaseModel):
    """整棵目录树 + 未归入任何文件夹的根文档数。"""

    kb_id: int
    items: List[FolderNode] = Field(default_factory=list)
    root_doc_count: int = 0
    total_folders: int = 0


class FolderAclGrant(BaseModel):
    """给文件夹批量授权：把一个文件夹（及其全部后代）放开给若干主体。"""

    subject_type: AclSubjectType = AclSubjectType.USER
    subject_ids: List[str] = Field(..., description="用户ID / 角色名 / 标签名")
    effect: AclEffect = AclEffect.ALLOW
    note: Optional[str] = Field(None, max_length=255)


class DocumentMove(BaseModel):
    document_ids: List[int]
    folder_id: Optional[int] = Field(None, description="目标文件夹；为空表示移到根下")


class FolderAclOut(BaseModel):
    id: int
    subject_type: AclSubjectType
    subject_id: str
    kb_id: int
    folder_id: Optional[int] = None
    effect: AclEffect
    note: Optional[str] = None
    scope: str = "folder"
    created_at: Optional[str] = None
