"""SQLAlchemy 声明基类与通用混入。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import String, TypeDecorator


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。与 app.core.security.utcnow 语义一致，
    这里单独放一份是为了避免 db 层反向依赖 core 层。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """创建/更新时间。

    时间戳一律用 **Python 侧** 默认值与 onupdate，不用 func.now() 这类 SQL 侧表达式。
    原因：SQL 侧 onupdate 的值由数据库算出，SQLAlchemy 在 UPDATE 之后无法得知新值，
    只能把该属性标记为 expired，等下次访问再去查库。在异步会话里这个「下次访问」
    就发生在响应序列化阶段，属性懒加载需要 greenlet 上下文，直接抛
    MissingGreenlet -> HTTP 500。改成 Python 侧后，值在 flush 时就写进对象，
    永不 expire，也就没有这个坑。

    server_default 仍保留，作为裸 SQL 写入时的兜底，且不改变既有表结构。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
        nullable=False,
    )


class EnumValue(TypeDecorator):
    """把 Python 枚举以「值」而非「成员名」存入数据库。

    默认 SQLAlchemy 存的是成员名 (ADMIN)，本类型存值 (admin)，便于直接读库排查。
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_cls, length: int = 32):
        self.enum_cls = enum_cls
        super().__init__(length=length)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return value.value if isinstance(value, self.enum_cls) else str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return self.enum_cls(value)
        except ValueError:
            return value
