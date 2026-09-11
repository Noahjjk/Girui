"""跨数据库可移植的 JSON 列类型。

PostgreSQL 用 JSONB（二进制、可索引），SQLite 只有通用 JSON 类型。
用 ``with_variant`` 让同一份模型定义在两边都能建表：
轻量部署走 SQLite，将来想切回 PostgreSQL 时不用改任何模型代码。
"""
from __future__ import annotations

from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

# PostgreSQL 下落到 JSONB，其余方言用通用 JSON
JSONType = JSON().with_variant(JSONB(), "postgresql")

__all__ = ["JSONType"]
