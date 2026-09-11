"""数据库表结构同步（首次部署或升级后执行）。

    python -m scripts.migrate

说明：本项目采用「建表 + 幂等补列」的轻量迁移策略，适配 10 人规模的内网部署。
表结构发生破坏性变更时，请改用 Alembic 生成正式迁移脚本。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.seed import seed_initial_data  # noqa: E402
from app.db.session import engine, init_models  # noqa: E402
from app.db.base import Base  # noqa: E402


async def main() -> int:
    print("[1/2] 同步表结构 …")
    await init_models()

    async with engine.connect() as conn:
        from sqlalchemy import inspect

        def _tables(sync_conn):
            return inspect(sync_conn).get_table_names()

        existing = await conn.run_sync(_tables)

    expected = set(Base.metadata.tables.keys())
    missing = expected - set(existing)
    if missing:
        print(f"      注意：以下表未创建成功 {sorted(missing)}")
    else:
        print(f"      已完成，共 {len(expected)} 张表")

    print("[2/2] 同步初始数据 …")
    await seed_initial_data()
    print("      完成")

    await engine.dispose()
    print("[完成] 数据库迁移结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
