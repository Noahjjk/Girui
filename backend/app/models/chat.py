"""问答会话与消息。"""
from __future__ import annotations

from typing import Any, List, Optional

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, EnumValue, TimestampMixin
from app.db.json_type import JSONType
from app.models.enums import MessageRole


class ChatSession(Base, TimestampMixin):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(255), default="新对话")
    model_provider_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("model_providers.id", ondelete="SET NULL")
    )
    model_name: Mapped[Optional[str]] = mapped_column(String(128))

    # 该会话启用的知识库范围（前端可勾选）
    kb_ids: Mapped[Optional[List[int]]] = mapped_column(JSONType, default=list)
    is_archived: Mapped[bool] = mapped_column(default=False, nullable=False)

    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    messages: Mapped[List["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )


class ChatMessage(Base, TimestampMixin):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )

    role: Mapped[MessageRole] = mapped_column(EnumValue(MessageRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")

    # 引用溯源：[{index, doc_id, doc_name, kb_id, kb_name, chunk_id, content,
    #            similarity, original_url, author, page}]
    citations: Mapped[Optional[List[dict]]] = mapped_column(JSONType, default=list)

    # 附件：[{type: image|file, name, path, size, mime}]
    attachments: Mapped[Optional[List[dict]]] = mapped_column(JSONType, default=list)
    # 该轮实际命中的片段 ID，便于事后审计「用户看到了哪些片段」
    hit_chunk_ids: Mapped[Optional[List[str]]] = mapped_column(JSONType, default=list)

    model_name: Mapped[Optional[str]] = mapped_column(String(128))
    usage: Mapped[Optional[dict]] = mapped_column(JSONType, default=dict)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error: Mapped[Optional[str]] = mapped_column(Text)
    feedback: Mapped[Optional[str]] = mapped_column(String(16))  # up / down

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
