"""系统配置与版本迭代清单。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.json_type import JSONType


class SystemConfig(Base, TimestampMixin):
    __tablename__ = "system_configs"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[Any]] = mapped_column(JSONType)
    description: Mapped[Optional[str]] = mapped_column(String(255))


class AppVersion(Base):
    """桌面端版本清单。electron-updater 从此处产出的更新源拉取。"""

    __tablename__ = "app_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), default="windows", index=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    min_supported_version: Mapped[Optional[str]] = mapped_column(String(32))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    release_notes_url: Mapped[Optional[str]] = mapped_column(String(512))

    download_url: Mapped[str] = mapped_column(String(512), nullable=False)
    sha512: Mapped[Optional[str]] = mapped_column(String(128))
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    mandatory: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
