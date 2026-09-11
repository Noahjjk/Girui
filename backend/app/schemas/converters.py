"""ORM 对象 → Pydantic 响应模型的显式转换。

不依赖 from_attributes 的隐式行为，因为若干字段需要计算或变形
（标签集合、API Key 脱敏、JSONB 空值兜底等）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from app.core.crypto import decrypt, mask
from app.models.chat import ChatMessage, ChatSession
from app.models.knowledge import ChunkAcl, KbDocument, KnowledgeBase, KbPermission
from app.models.llm import ModelProvider
from app.models.user import User
from app.schemas.auth import SessionInfo, UserBrief
from app.schemas.chat import MessageOut, ProviderOut, SessionOut
from app.schemas.knowledge import (
    ChunkAclOut,
    DocumentOut,
    KnowledgeBaseOut,
    PermissionOut,
)
from app.schemas.user import UserOut


# ---------------------------------------------------------------- 用户


def user_to_brief(user: User) -> UserBrief:
    return UserBrief(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        email=user.email,
        department=user.department,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        tags=user.tag_names,
        last_login_at=user.last_login_at,
    )


def user_to_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        phone=user.phone,
        department=user.department,
        role=user.role,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        locked_until=user.locked_until,
        failed_login_count=user.failed_login_count,
        last_login_at=user.last_login_at,
        last_login_ip=user.last_login_ip,
        remark=user.remark,
        created_at=user.created_at,
        tags=user.tag_names,
    )


def token_to_session_info(token) -> SessionInfo:
    d_name = token.device_name
    if d_name and "%" in d_name:
        try:
            d_name = unquote(d_name)
        except Exception:
            pass
    return SessionInfo(
        id=token.id,
        device_id=token.device_id,
        device_name=d_name,
        ip=token.ip,
        user_agent=token.user_agent,
        remember_me=token.remember_me,
        last_used_at=token.last_used_at,
        expires_at=token.expires_at,
        created_at=token.created_at,
    )


# ---------------------------------------------------------------- 知识库


def kb_to_out(
    kb: KnowledgeBase,
    can_read: bool = False,
    can_upload: bool = False,
    can_manage: bool = False,
) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=kb.id,
        name=kb.name,
        code=kb.code,
        description=kb.description,
        ragflow_dataset_id=kb.ragflow_dataset_id,
        embedding_model=kb.embedding_model,
        chunk_method=kb.chunk_method,
        is_active=kb.is_active,
        doc_count=kb.doc_count,
        chunk_count=kb.chunk_count,
        created_at=kb.created_at,
        my_can_read=can_read,
        my_can_upload=can_upload,
        my_can_manage=can_manage,
    )


def permission_to_out(perm: KbPermission, user: Optional[User] = None) -> PermissionOut:
    role = None
    if user is not None:
        role = user.role.value if hasattr(user.role, "value") else str(user.role)
    return PermissionOut(
        id=perm.id,
        user_id=perm.user_id,
        kb_id=perm.kb_id,
        can_read=perm.can_read,
        can_upload=perm.can_upload,
        can_manage=perm.can_manage,
        note=perm.note,
        username=user.username if user else None,
        display_name=user.display_name if user else None,
        role=role,
    )


def document_to_out(doc: KbDocument) -> DocumentOut:
    return DocumentOut(
        id=doc.id,
        kb_id=doc.kb_id,
        ragflow_document_id=doc.ragflow_document_id,
        name=doc.name,
        file_type=doc.file_type,
        size_bytes=doc.size_bytes,
        category=doc.category,
        source=doc.source,
        original_url=doc.original_url,
        visibility=doc.visibility,
        tags=list(doc.tags or []),
        chunk_count=doc.chunk_count,
        token_count=doc.token_count,
        progress=doc.progress,
        status=doc.status,
        error_message=doc.error_message,
        uploaded_by=doc.uploaded_by,
        remark=doc.remark,
        folder_id=doc.folder_id,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def acl_to_out(row: ChunkAcl) -> ChunkAclOut:
    return ChunkAclOut(
        id=row.id,
        subject_type=row.subject_type,
        subject_id=row.subject_id,
        kb_id=row.kb_id,
        ragflow_document_id=row.ragflow_document_id,
        chunk_id=row.chunk_id,
        effect=row.effect,
        scope=row.scope,
        note=row.note,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


# ---------------------------------------------------------------- 模型供应商


def provider_to_out(provider: ModelProvider, reveal_masked: bool = True) -> ProviderOut:
    raw = decrypt(provider.api_key_enc or "")
    return ProviderOut(
        id=provider.id,
        name=provider.name,
        provider=provider.provider,
        base_url=provider.base_url,
        model_name=provider.model_name,
        api_key_masked=mask(raw) if (raw and reveal_masked) else None,
        has_api_key=bool(raw),
        supports_vision=provider.supports_vision,
        supports_stream=provider.supports_stream,
        max_tokens=provider.max_tokens,
        temperature=provider.temperature,
        top_p=provider.top_p,
        timeout_seconds=provider.timeout_seconds,
        extra=provider.extra or {},
        enabled=provider.enabled,
        is_default=provider.is_default,
        sort_order=provider.sort_order,
        remark=provider.remark,
        created_at=provider.created_at,
    )


# ---------------------------------------------------------------- 会话


def session_to_out(session: ChatSession) -> SessionOut:
    return SessionOut(
        id=session.id,
        title=session.title,
        model_provider_id=session.model_provider_id,
        model_name=session.model_name,
        kb_ids=list(session.kb_ids or []),
        message_count=session.message_count,
        is_archived=session.is_archived,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def message_to_out(msg: ChatMessage) -> MessageOut:
    return MessageOut(
        id=msg.id,
        session_id=msg.session_id,
        role=msg.role,
        content=msg.content,
        citations=list(msg.citations or []),
        attachments=list(msg.attachments or []),
        model_name=msg.model_name,
        usage=msg.usage or {},
        latency_ms=msg.latency_ms,
        error=msg.error,
        feedback=msg.feedback,
        created_at=msg.created_at,
    )
