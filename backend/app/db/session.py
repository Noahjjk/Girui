"""异步数据库会话。

同一份代码同时支持 SQLite（轻量部署）与 PostgreSQL（大内存部署），
差异集中在引擎参数：SQLite 不认识 pool_size / max_overflow，
并且需要显式开启 WAL 才能让读写并发不互相阻塞。
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.base import Base

logger = logging.getLogger(__name__)

_URL = settings.database_url
_IS_SQLITE = settings.is_sqlite

if _IS_SQLITE:
    engine = create_async_engine(
        _URL,
        echo=settings.DB_ECHO,
        # SQLite 单文件：连接池交给默认实现，避免多写者互相锁死
        connect_args={"check_same_thread": False, "timeout": 30},
    )
else:
    engine = create_async_engine(
        _URL,
        echo=settings.DB_ECHO,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_recycle=1800,
    )


if _IS_SQLITE:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        """WAL + 外键约束 + 更宽松的同步级别。

        WAL 让读不被写阻塞（问答是读多写少）；
        SQLite 默认不校验外键，必须显式打开，否则 ondelete 形同虚设。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA cache_size=-16000")  # 约 16 MB 页缓存
        cur.close()


SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_models() -> None:
    """开发/首次部署时建表。生产环境建议改走 Alembic 迁移。"""
    # 导入所有模型，确保它们注册到 Base.metadata
    from app import models  # noqa: F401

    if _IS_SQLITE:
        from pathlib import Path

        db_file = _URL.split("///", 1)[-1]
        Path(db_file).parent.mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("数据库表结构已同步（%s）", "SQLite" if _IS_SQLITE else "PostgreSQL")
