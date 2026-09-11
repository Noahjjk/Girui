"""操作日志（审计）。仅追加，无删除接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, EnumValue
from app.db.json_type import JSONType
from app.models.enums import LogStatus


class OperationLog(Base):
    __tablename__ = "operation_logs"
    __table_args__ = (
        Index("ix_log_user_created", "user_id", "created_at"),
        Index("ix_log_action_created", "action", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    username: Mapped[Optional[str]] = mapped_column(String(64), index=True)

    # 动作编码，如 login / chat / upload / user.create / perm.grant
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(32))
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    resource_name: Mapped[Optional[str]] = mapped_column(String(255))

    detail: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType, default=dict)

    method: Mapped[Optional[str]] = mapped_column(String(8))
    path: Mapped[Optional[str]] = mapped_column(String(512))
    ip: Mapped[Optional[str]] = mapped_column(String(64))
    user_agent: Mapped[Optional[str]] = mapped_column(String(512))
    device_id: Mapped[Optional[str]] = mapped_column(String(128))

    status: Mapped[LogStatus] = mapped_column(
        EnumValue(LogStatus), default=LogStatus.SUCCESS, nullable=False, index=True
    )
    error: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
