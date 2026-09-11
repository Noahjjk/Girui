"""知识库、文档映射、片段级 ACL。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, EnumValue, TimestampMixin
from app.db.json_type import JSONType
from app.models.enums import AclEffect, AclSubjectType, DocumentStatus, Visibility


class KnowledgeBase(Base, TimestampMixin):
    """业务侧知识库，1:1 映射检索后端的一个 dataset。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    code: Mapped[Optional[str]] = mapped_column(String(64), unique=True)
    description: Mapped[Optional[str]] = mapped_column(Text)

    # 检索侧标识（本地内核为自生成 hex，RAGFlow 为远端 dataset id）
    ragflow_dataset_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    embedding_model: Mapped[str] = mapped_column(String(64), default="bge-m3")
    chunk_method: Mapped[str] = mapped_column(String(32), default="naive")

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    owner_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    permissions: Mapped[List["KbPermission"]] = relationship(
        back_populates="kb", cascade="all, delete-orphan"
    )
    documents: Mapped[List["KbDocument"]] = relationship(
        back_populates="kb", cascade="all, delete-orphan"
    )
    folders: Mapped[List["KbFolder"]] = relationship(
        back_populates="kb", cascade="all, delete-orphan"
    )


class KbPermission(Base, TimestampMixin):
    """知识库级授权：决定用户能否触达某个库。"""

    __tablename__ = "kb_permissions"
    __table_args__ = (UniqueConstraint("user_id", "kb_id", name="uq_kb_perm_user_kb"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )

    can_read: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_upload: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_manage: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    granted_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[Optional[str]] = mapped_column(String(255))

    kb: Mapped["KnowledgeBase"] = relationship(back_populates="permissions")


class KbDocument(Base, TimestampMixin):
    """业务侧文档记录，持有检索侧 document_id 的映射与可见性元数据。"""

    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("kb_id", "ragflow_document_id", name="uq_doc_kb_ragflow"),
        Index("ix_doc_kb_visibility", "kb_id", "visibility"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )

    ragflow_document_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_type: Mapped[Optional[str]] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    # 业务归属
    category: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(32), default="upload")  # upload / import
    original_url: Mapped[Optional[str]] = mapped_column(String(1024))
    storage_path: Mapped[Optional[str]] = mapped_column(String(512))
    checksum: Mapped[Optional[str]] = mapped_column(String(128))

    # 可见性
    visibility: Mapped[Visibility] = mapped_column(
        EnumValue(Visibility), default=Visibility.PUBLIC, nullable=False, index=True
    )
    tags: Mapped[Optional[List[str]]] = mapped_column(JSONType, default=list)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    progress: Mapped[float] = mapped_column(default=0.0, nullable=False)

    status: Mapped[DocumentStatus] = mapped_column(
        EnumValue(DocumentStatus), default=DocumentStatus.PENDING, nullable=False, index=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    uploaded_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    remark: Mapped[Optional[str]] = mapped_column(Text)

    # 所属文件夹；为空表示直接挂在知识库根下（老数据全都是这种，向后兼容）。
    # 用 SET NULL 而不是 CASCADE 是刻意为之：删除文件夹走的是「内部非空则拒绝删除」的
    # 路径（见 api/v1/folders.py）。万一将来有别的路径绕过那层校验，宁可让文档掉回根目录，
    # 也绝不能把知识内容连带删掉。
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("kb_folders.id", ondelete="SET NULL"), index=True
    )

    kb: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    folder: Mapped[Optional["KbFolder"]] = relationship(back_populates="documents")


class ChunkAcl(Base, TimestampMixin):
    """权限黑白名单 —— 「权限粒度控制到用户可检索到的知识片段」的落点。

    规则可以挂在四个层级上，越具体越优先：

      1. chunk 级    —— 单条片段（chunk_id 非空）
      2. 文档级      —— 整篇文档（ragflow_document_id 非空）
      3. 文件夹级    —— 该文件夹及其所有后代文件夹内的文档（folder_id 非空）
      4. 知识库级    —— 整个库（上面三个都为空）

    文件夹级规则沿祖先链向下继承；链上任一环节出现 deny 即整体拒绝（deny 绝对优先）。
    """

    __tablename__ = "chunk_acls"
    __table_args__ = (
        Index("ix_acl_lookup", "kb_id", "ragflow_document_id", "chunk_id"),
        Index("ix_acl_subject", "subject_type", "subject_id"),
        Index("ix_acl_folder", "folder_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    subject_type: Mapped[AclSubjectType] = mapped_column(
        EnumValue(AclSubjectType), nullable=False
    )
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)  # 用户ID / 角色名 / 标签名

    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ragflow_document_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    chunk_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    folder_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("kb_folders.id", ondelete="CASCADE")
    )

    effect: Mapped[AclEffect] = mapped_column(
        EnumValue(AclEffect), default=AclEffect.ALLOW, nullable=False
    )
    note: Mapped[Optional[str]] = mapped_column(String(255))
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    @property
    def scope(self) -> str:
        if self.chunk_id:
            return "chunk"
        if self.ragflow_document_id:
            return "document"
        if self.folder_id:
            return "folder"
        return "knowledge_base"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subject_type": self.subject_type.value
            if hasattr(self.subject_type, "value")
            else str(self.subject_type),
            "subject_id": self.subject_id,
            "kb_id": self.kb_id,
            "ragflow_document_id": self.ragflow_document_id,
            "chunk_id": self.chunk_id,
            "folder_id": self.folder_id,
            "effect": self.effect.value if hasattr(self.effect, "value") else str(self.effect),
            "scope": self.scope,
            "note": self.note,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class KbFolder(Base, TimestampMixin):
    """知识库内的文件夹节点，支持任意层级嵌套。

    层级用**物化路径** `path` 表达（形如 `/3/7/12/`，即从根到父节点的 id 串）。
    这样「某文件夹及其全部后代」只需一条 `path LIKE '/3/7/12/%'` 就能查出，
    不必递归查询、也不必担心层级加深后 SQL 变复杂。

    文件夹是**批量授权的载体**：给它配一条 allow，其下所有文档一并放行；
    但文档自身若单独配了规则，文档级规则优先。文件夹本身不持有向量数据 ——
    一个知识库仍然只有一个索引文件，文件夹只是权限与分组的一层元数据。
    """

    __tablename__ = "kb_folders"
    __table_args__ = (
        Index("ix_folder_kb_path", "kb_id", "path"),
        Index("ix_folder_parent", "kb_id", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True, nullable=False
    )
    parent_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("kb_folders.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 物化路径：从根到**父节点**的 id 链，形如 "/3/7/"（根级文件夹为 "/"）
    path: Mapped[str] = mapped_column(String(512), default="/", nullable=False)
    depth: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 文件夹自身可见性。RESTRICTED 时其内文档默认不可见，需文件夹级或文档级 allow 放行；
    # 子文件夹沿祖先链继承（父级 RESTRICTED 则子级也按 RESTRICTED 处理）。
    visibility: Mapped[Visibility] = mapped_column(
        EnumValue(Visibility), default=Visibility.PUBLIC, nullable=False
    )
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    kb: Mapped["KnowledgeBase"] = relationship(back_populates="folders")
    documents: Mapped[List["KbDocument"]] = relationship(back_populates="folder")

    @property
    def self_path(self) -> str:
        """含自身的路径前缀，用于匹配「本文件夹及其所有后代」。"""
        return f"{self.path}{self.id}/"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kb_id": self.kb_id,
            "parent_id": self.parent_id,
            "name": self.name,
            "path": self.path,
            "depth": self.depth,
            "sort_order": self.sort_order,
            "visibility": self.visibility.value
            if hasattr(self.visibility, "value")
            else str(self.visibility),
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
