"""大模型供应商配置。API Key 加密存储。"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, EnumValue, TimestampMixin
from app.db.json_type import JSONType
from app.models.enums import ProviderKind


class ModelProvider(Base, TimestampMixin):
    """一条记录 = 前端模型下拉里的一个可选项。

    统一走 OpenAI 兼容协议，因此 DeepSeek / 通义千问 / vLLM / Ollama / 任意自建端点
    都只是 base_url + model_name 的差别。
    """

    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # 展示名，例如「DeepSeek V3」
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[ProviderKind] = mapped_column(
        EnumValue(ProviderKind), default=ProviderKind.OPENAI_COMPATIBLE, nullable=False
    )

    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    api_key_enc: Mapped[Optional[str]] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)

    supports_vision: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_stream: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    max_tokens: Mapped[int] = mapped_column(Integer, default=4096, nullable=False)
    temperature: Mapped[float] = mapped_column(default=0.3, nullable=False)
    top_p: Mapped[float] = mapped_column(default=0.9, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)

    # 兜底参数，例如 Ollama 需要 {"num_ctx": 8192}
    extra: Mapped[Optional[Dict[str, Any]]] = mapped_column(JSONType, default=dict)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    remark: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
