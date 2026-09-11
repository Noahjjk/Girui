"""轻量迁移：为「多级嵌套文件夹」补齐表与列。

为什么不用 Alembic：本项目生产形态是单文件 SQLite，引入完整迁移框架的维护成本
高于收益。但 `Base.metadata.create_all` 有个硬限制 —— **只建新表，不给已有表加列**。
所以「新增 kb_folders 表」能自动完成，而「给 kb_documents / chunk_acls 加 folder_id 列」
必须单独跑这个脚本。

脚本是幂等的：重复执行不会报错，也不会改动已有数据。

用法：
    cd backend && <venv>/python scripts/migrate_folders.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402

# (表名, 列名, 加列 DDL)
ADD_COLUMNS: list[tuple[str, str, str]] = [
    (
        "kb_documents",
        "folder_id",
        "ALTER TABLE kb_documents ADD COLUMN folder_id INTEGER "
        "REFERENCES kb_folders(id) ON DELETE SET NULL",
    ),
    (
        "chunk_acls",
        "folder_id",
        "ALTER TABLE chunk_acls ADD COLUMN folder_id INTEGER "
        "REFERENCES kb_folders(id) ON DELETE CASCADE",
    ),
]

# create_all 只对**新建**的表建索引；给已有表加完列后，索引得自己补
ADD_INDEXES: list[str] = [
    "CREATE INDEX IF NOT EXISTS ix_kb_documents_folder_id ON kb_documents (folder_id)",
    "CREATE INDEX IF NOT EXISTS ix_chunk_acls_folder_id ON chunk_acls (folder_id)",
    "CREATE INDEX IF NOT EXISTS ix_folder_kb_path ON kb_folders (kb_id, path)",
    "CREATE INDEX IF NOT EXISTS ix_folder_parent ON kb_folders (kb_id, parent_id)",
]


def _sqlite_path() -> str:
    url = settings.database_url
    if "///" not in url:
        raise SystemExit(f"无法从 DATABASE_URL 解析 SQLite 路径：{url!r}")
    return url.split("///", 1)[-1]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _run_sqlite() -> int:
    db_path = _sqlite_path()
    if not os.path.exists(db_path):
        print(f"[!] 数据库文件不存在：{db_path}")
        print("    create_all 会把它建出来，本脚本继续执行。")

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        changed = 0

        if not _table_exists(conn, "kb_folders"):
            print("[!] kb_folders 表建表失败，请检查后端日志。")
            return 2
        print("[ok] kb_folders 表已就绪")

        for table, column, ddl in ADD_COLUMNS:
            if not _table_exists(conn, table):
                print(f"[!] 跳过：表 {table} 不存在")
                continue
            if column in _columns(conn, table):
                print(f"[ok] {table}.{column} 已存在，无需变更")
                continue
            conn.execute(ddl)
            changed += 1
            print(f"[+] {table}.{column} 已新增")

        for ddl in ADD_INDEXES:
            conn.execute(ddl)
        print("[ok] 索引已就绪")

        conn.commit()

        # 数据体检：加列后老数据应当全部落到「根目录」（folder_id 为 NULL）
        doc_total = conn.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0]
        doc_root = conn.execute(
            "SELECT COUNT(*) FROM kb_documents WHERE folder_id IS NULL"
        ).fetchone()[0]
        folder_total = conn.execute("SELECT COUNT(*) FROM kb_folders").fetchone()[0]
        print()
        print("迁移结果：")
        print(f"  文档 {doc_total} 篇，其中挂在根目录 {doc_root} 篇（老数据应全部为根目录）")
        print(f"  文件夹 {folder_total} 个")
        print(f"  本次变更 {changed} 处")
        return 0
    finally:
        conn.close()


async def _ensure_new_tables() -> None:
    """让 create_all 把 kb_folders 建出来。

    它对已经存在的表是空操作，因此不会碰老数据 —— 加列的事交给下面的 ALTER。
    """
    from app.db.session import init_models

    await init_models()


def main() -> int:
    if not settings.is_sqlite:
        print("[!] 当前 DATABASE_URL 不是 SQLite，本脚本只处理 SQLite。")
        print("    PostgreSQL 请用 Alembic 或手工 ALTER TABLE 添加 folder_id 列与 kb_folders 表。")
        return 1
    asyncio.run(_ensure_new_tables())
    return _run_sqlite()


if __name__ == "__main__":
    raise SystemExit(main())
