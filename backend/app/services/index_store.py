"""轻量检索索引：SQLite 存片段、向量与全文索引。

为什么用 SQLite 而不是「向量数据库」：
  目标机器只有 1.6 GB 内存，起不了 Elasticsearch / Milvus 这类服务。
  SQLite 是单文件、零进程、库自带，写进 WAL 模式后读写并发也够用。
  向量以 float32 原始字节存 BLOB，查询时在内存里做矩阵乘法 ——
  512 维、几万片规模下，一次检索是毫秒级，完全够用。

三个表：
  idx_documents  文档解析状态（对应 RAGFlow 侧的 document）
  idx_chunks     片段正文与页码
  idx_chunks_fts FTS5 全文索引（jieba 分词后的 token 串）
  idx_vectors    片段向量

片段 id = f"{document_id}-{ordinal:04d}"，确定性生成，
这样重新解析同一份文件时片段 id 不变，管理员配的片段级权限不会失效。
（注意：若文件内容改动导致切片边界变化，序号会平移，这是本方案的已知限制。）
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

try:
    import aiosqlite
except ImportError as exc:  # pragma: no cover
    raise ImportError("缺少 aiosqlite 依赖，请 pip install aiosqlite") from exc

from app.core.config import settings
from app.services.chunker import Chunk
from app.services.textutil import fts_query, fts_tokens, token_mode

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_INSERT_BATCH = 400  # SQLite 变量个数上限保护


def make_chunk_id(document_id: str, ordinal: int) -> str:
    return f"{document_id}-{ordinal:04d}"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS idx_meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS idx_documents (
  id          TEXT PRIMARY KEY,
  dataset_id  TEXT NOT NULL,
  name        TEXT NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending',
  progress    REAL NOT NULL DEFAULT 0,
  chunk_count INTEGER NOT NULL DEFAULT 0,
  token_count INTEGER NOT NULL DEFAULT 0,
  char_count  INTEGER NOT NULL DEFAULT 0,
  source_path TEXT,
  ext         TEXT,
  message     TEXT,
  updated_at  REAL
);
CREATE INDEX IF NOT EXISTS ix_idx_documents_dataset ON idx_documents(dataset_id);

CREATE TABLE IF NOT EXISTS idx_chunks (
  id          TEXT PRIMARY KEY,
  dataset_id  TEXT NOT NULL,
  document_id TEXT NOT NULL,
  ordinal     INTEGER NOT NULL,
  content     TEXT NOT NULL,
  page        INTEGER,
  tokens      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_idx_chunks_doc ON idx_chunks(document_id);
CREATE INDEX IF NOT EXISTS ix_idx_chunks_dataset ON idx_chunks(dataset_id);

CREATE VIRTUAL TABLE IF NOT EXISTS idx_chunks_fts USING fts5(
  content,
  chunk_id UNINDEXED,
  tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS idx_vectors (
  chunk_id TEXT PRIMARY KEY,
  dim      INTEGER NOT NULL,
  vec      BLOB NOT NULL
);
"""

_TEMP_TABLES = """
CREATE TEMP TABLE IF NOT EXISTS tp_docs (document_id TEXT PRIMARY KEY);
CREATE TEMP TABLE IF NOT EXISTS tp_datasets (dataset_id TEXT PRIMARY KEY);
"""


class IndexStore:
    """索引库。单连接 + 异步锁，避免 SQLite 写锁竞争。"""

    def __init__(self, path: Optional[str] = None):
        self.path = path or settings.index_db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._ready = False
        self._mode_warned = False

    # ------------------------------------------------------------ 生命周期

    async def connect(self) -> None:
        if self._db is not None:
            return
        from pathlib import Path

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("PRAGMA temp_store=MEMORY")
        await db.execute("PRAGMA cache_size=-16000")
        await db.executescript(_SCHEMA)
        await db.executescript(_TEMP_TABLES)
        await db.commit()
        self._db = db
        await self._check_token_mode()
        logger.info("检索索引库就绪 path=%s 分词=%s", self.path, token_mode())

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            await self.connect()
        assert self._db is not None
        return self._db

    async def _check_token_mode(self) -> None:
        """分词模式变了会让已有索引的召回质量下降，必须显式告警。"""
        db = await self._conn()
        row = await (await db.execute(
            "SELECT value FROM idx_meta WHERE key='token_mode'"
        )).fetchone()
        current = token_mode()
        if row is None:
            await db.execute(
                "INSERT OR REPLACE INTO idx_meta(key, value) VALUES('token_mode', ?)", (current,)
            )
            await db.execute(
                "INSERT OR REPLACE INTO idx_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            await db.commit()
        elif row["value"] != current:
            logger.warning(
                "索引分词模式已从 %s 变为 %s，历史片段的全文索引可能不准，"
                "建议在后台对文档执行一次「重新解析」。",
                row["value"], current,
            )

    # ------------------------------------------------------------ 文档状态

    async def upsert_document(
        self,
        document_id: str,
        dataset_id: str,
        name: str,
        state: str = "pending",
        message: Optional[str] = None,
        source_path: Optional[str] = None,
        ext: Optional[str] = None,
    ) -> None:
        async with self._lock:
            db = await self._conn()
            await db.execute(
                """
                INSERT INTO idx_documents
                    (id, dataset_id, name, state, message, source_path, ext, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, state=excluded.state, message=excluded.message,
                    source_path=COALESCE(excluded.source_path, idx_documents.source_path),
                    ext=COALESCE(excluded.ext, idx_documents.ext),
                    updated_at=excluded.updated_at
                """,
                (document_id, dataset_id, name, state, message, source_path, ext, time.time()),
            )
            await db.commit()

    async def pending_documents(self) -> List[Dict[str, Any]]:
        """未完成解析的文档，用于进程重启后续跑。"""
        db = await self._conn()
        rows = await (await db.execute(
            "SELECT * FROM idx_documents WHERE state IN ('pending','parsing') ORDER BY updated_at"
        )).fetchall()
        return [dict(r) for r in rows]

    async def set_document_state(
        self,
        document_id: str,
        state: str,
        *,
        chunk_count: Optional[int] = None,
        token_count: Optional[int] = None,
        char_count: Optional[int] = None,
        progress: Optional[float] = None,
        message: Optional[str] = None,
    ) -> None:
        async with self._lock:
            db = await self._conn()
            await db.execute(
                """
                UPDATE idx_documents SET
                    state = ?,
                    chunk_count = COALESCE(?, chunk_count),
                    token_count = COALESCE(?, token_count),
                    char_count  = COALESCE(?, char_count),
                    progress    = COALESCE(?, progress),
                    message     = ?,
                    updated_at  = ?
                WHERE id = ?
                """,
                (state, chunk_count, token_count, char_count, progress, message,
                 time.time(), document_id),
            )
            await db.commit()

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        db = await self._conn()
        row = await (await db.execute(
            "SELECT * FROM idx_documents WHERE id = ?", (document_id,)
        )).fetchone()
        return dict(row) if row else None

    async def list_documents(
        self,
        dataset_id: str,
        page: int = 1,
        page_size: int = 100,
        keywords: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        db = await self._conn()
        where = "WHERE dataset_id = ?"
        params: List[Any] = [dataset_id]
        if keywords:
            where += " AND name LIKE ?"
            params.append(f"%{keywords}%")
        total = int((await (await db.execute(
            f"SELECT COUNT(*) AS c FROM idx_documents {where}", params
        )).fetchone())["c"])
        rows = await (await db.execute(
            f"SELECT * FROM idx_documents {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )).fetchall()
        return [dict(r) for r in rows], total

    async def delete_document(self, document_id: str) -> int:
        """删除一篇文档的全部片段、向量与全文索引，返回删除的片段数。"""
        async with self._lock:
            db = await self._conn()
            ids = [r["id"] for r in (await (await db.execute(
                "SELECT id FROM idx_chunks WHERE document_id = ?", (document_id,)
            )).fetchall())]
            await self._delete_chunk_rows(db, ids)
            await db.execute("DELETE FROM idx_documents WHERE id = ?", (document_id,))
            await db.commit()
            return len(ids)

    async def delete_dataset(self, dataset_id: str) -> int:
        async with self._lock:
            db = await self._conn()
            ids = [r["id"] for r in (await (await db.execute(
                "SELECT id FROM idx_chunks WHERE dataset_id = ?", (dataset_id,)
            )).fetchall())]
            await self._delete_chunk_rows(db, ids)
            await db.execute("DELETE FROM idx_documents WHERE dataset_id = ?", (dataset_id,))
            await db.commit()
            return len(ids)

    async def _delete_chunk_rows(self, db: aiosqlite.Connection, chunk_ids: Sequence[str]) -> None:
        if not chunk_ids:
            return
        for i in range(0, len(chunk_ids), _INSERT_BATCH):
            batch = chunk_ids[i : i + _INSERT_BATCH]
            marks = ",".join("?" * len(batch))
            await db.execute(f"DELETE FROM idx_chunks WHERE id IN ({marks})", batch)
            await db.execute(f"DELETE FROM idx_vectors WHERE chunk_id IN ({marks})", batch)
            await db.execute(f"DELETE FROM idx_chunks_fts WHERE chunk_id IN ({marks})", batch)

    # ------------------------------------------------------------ 写入片段

    async def replace_document_chunks(
        self,
        dataset_id: str,
        document_id: str,
        chunks: Sequence[Chunk],
        embeddings: np.ndarray,
    ) -> None:
        """整篇替换：先清旧片段再写新片段，保证不会出现新旧混杂。"""
        if len(chunks) != len(embeddings):
            raise ValueError("片段数与向量数不一致")

        async with self._lock:
            db = await self._conn()
            old = [r["id"] for r in (await (await db.execute(
                "SELECT id FROM idx_chunks WHERE document_id = ?", (document_id,)
            )).fetchall())]
            await self._delete_chunk_rows(db, old)

            chunk_rows: List[Tuple] = []
            fts_rows: List[Tuple] = []
            vec_rows: List[Tuple] = []
            for ch, vec in zip(chunks, embeddings):
                cid = make_chunk_id(document_id, ch.ordinal)
                chunk_rows.append(
                    (cid, dataset_id, document_id, ch.ordinal, ch.content, ch.page, ch.tokens)
                )
                fts_rows.append((fts_tokens(ch.content), cid))
                vec_rows.append((cid, int(vec.shape[0]), np.asarray(vec, dtype=np.float32).tobytes()))

            await db.executemany(
                "INSERT OR REPLACE INTO idx_chunks"
                "(id, dataset_id, document_id, ordinal, content, page, tokens)"
                " VALUES (?,?,?,?,?,?,?)",
                chunk_rows,
            )
            await db.executemany(
                "INSERT INTO idx_chunks_fts(content, chunk_id) VALUES (?,?)", fts_rows
            )
            await db.executemany(
                "INSERT OR REPLACE INTO idx_vectors(chunk_id, dim, vec) VALUES (?,?,?)", vec_rows
            )
            await db.commit()
            logger.debug("写入片段 document=%s 数量=%d", document_id, len(chunk_rows))

    # ------------------------------------------------------------ 读取片段

    async def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        page: int = 1,
        page_size: int = 100,
        keywords: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        db = await self._conn()
        where = "WHERE dataset_id = ? AND document_id = ?"
        params: List[Any] = [dataset_id, document_id]
        if keywords:
            where += " AND content LIKE ?"
            params.append(f"%{keywords}%")
        total = int((await (await db.execute(
            f"SELECT COUNT(*) AS c FROM idx_chunks {where}", params
        )).fetchone())["c"])
        rows = await (await db.execute(
            f"SELECT id, document_id, ordinal, content, page, tokens FROM idx_chunks "
            f"{where} ORDER BY ordinal LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        )).fetchall()
        return [dict(r) for r in rows], total

    async def all_chunks(
        self, dataset_id: str, document_id: str, limit: int = 5000
    ) -> List[Dict[str, Any]]:
        db = await self._conn()
        rows = await (await db.execute(
            "SELECT id, document_id, ordinal, content, page, tokens FROM idx_chunks "
            "WHERE dataset_id = ? AND document_id = ? ORDER BY ordinal LIMIT ?",
            (dataset_id, document_id, limit),
        )).fetchall()
        return [dict(r) for r in rows]

    async def count_chunks(self, dataset_id: Optional[str] = None) -> int:
        db = await self._conn()
        if dataset_id:
            sql = "SELECT COUNT(*) AS c FROM idx_chunks WHERE dataset_id = ?"
            params: Sequence[Any] = (dataset_id,)
        else:
            sql, params = "SELECT COUNT(*) AS c FROM idx_chunks", ()
        return int((await (await db.execute(sql, params)).fetchone())["c"])

    async def stats(self) -> Dict[str, Any]:
        db = await self._conn()
        docs = int((await (await db.execute(
            "SELECT COUNT(*) AS c FROM idx_documents"
        )).fetchone())["c"])
        chunks = await self.count_chunks()
        size = 0
        from pathlib import Path

        for suffix in ("", "-wal", "-shm"):
            p = Path(self.path + suffix)
            if p.exists():
                size += p.stat().st_size
        return {
            "documents": docs,
            "chunks": chunks,
            "db_bytes": size,
            "token_mode": token_mode(),
            "path": self.path,
        }

    # ------------------------------------------------------------ 检索

    async def _load_scope(
        self,
        db: aiosqlite.Connection,
        dataset_ids: Sequence[str],
        document_ids: Sequence[str],
    ) -> None:
        """把可见范围塞进临时表。

        不用 IN (?,?,...) 是因为文档数可能上千，会撞上 SQLite 的变量个数上限。
        临时表在单连接下由 _lock 串行保护，不存在互相踩踏。
        """
        await db.execute("DELETE FROM tp_docs")
        await db.execute("DELETE FROM tp_datasets")
        if dataset_ids:
            ds = [(d,) for d in dataset_ids]
            for i in range(0, len(ds), _INSERT_BATCH):
                await db.executemany("INSERT OR IGNORE INTO tp_datasets VALUES (?)", ds[i : i + _INSERT_BATCH])
        if document_ids:
            docs = [(d,) for d in document_ids]
            for i in range(0, len(docs), _INSERT_BATCH):
                await db.executemany("INSERT OR IGNORE INTO tp_docs VALUES (?)", docs[i : i + _INSERT_BATCH])

    async def search(
        self,
        question: str,
        dataset_ids: Sequence[str],
        document_ids: Sequence[str],
        query_vector: np.ndarray,
        top_k: Optional[int] = None,
        vector_weight: Optional[float] = None,
        threshold: Optional[float] = None,
        fts_limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """向量 + 全文混合检索。

        返回结构对齐 RAGFlow 的 retrieval 响应，字段名保持一致：
        id / content / document_id / document_keyword / similarity / positions
        """
        top_k = top_k or settings.RETRIEVAL_TOP_K
        vector_weight = (
            vector_weight if vector_weight is not None else settings.HYBRID_VECTOR_WEIGHT
        )
        threshold = threshold if threshold is not None else settings.SIMILARITY_THRESHOLD
        if not dataset_ids or not document_ids:
            return []

        db = await self._conn()
        qvec = np.asarray(query_vector, dtype=np.float32).reshape(-1)

        async with self._lock:
            await self._load_scope(db, dataset_ids, document_ids)

            # ---------- 全文候选 ----------
            kw_score: Dict[str, float] = {}
            expr = fts_query(question)
            if expr:
                try:
                    rows = await (await db.execute(
                        """
                        SELECT idx_chunks_fts.chunk_id AS cid,
                               -bm25(idx_chunks_fts)   AS score
                        FROM idx_chunks_fts
                        JOIN idx_chunks ON idx_chunks.id = idx_chunks_fts.chunk_id
                        WHERE idx_chunks_fts MATCH ?
                          AND idx_chunks.document_id IN (SELECT document_id FROM tp_docs)
                        ORDER BY score DESC
                        LIMIT ?
                        """,
                        (expr, fts_limit),
                    )).fetchall()
                    kw_score = {r["cid"]: float(r["score"] or 0.0) for r in rows}
                except Exception as exc:  # noqa: BLE001
                    logger.warning("全文检索失败（降级为纯向量检索）：%s", exc)
                    kw_score = {}

            # ---------- 向量候选 ----------
            vec_score: Dict[str, float] = {}
            total = int((await (await db.execute(
                """
                SELECT COUNT(*) AS c
                FROM idx_vectors v
                JOIN idx_chunks c ON c.id = v.chunk_id
                WHERE c.document_id IN (SELECT document_id FROM tp_docs)
                """
            )).fetchone())["c"])

            batch = 2000
            for offset in range(0, total, batch):
                rows = await (await db.execute(
                    """
                    SELECT v.chunk_id AS cid, v.dim AS dim, v.vec AS vec
                    FROM idx_vectors v
                    JOIN idx_chunks c ON c.id = v.chunk_id
                    WHERE c.document_id IN (SELECT document_id FROM tp_docs)
                    ORDER BY v.chunk_id
                    LIMIT ? OFFSET ?
                    """,
                    (batch, offset),
                )).fetchall()
                if not rows:
                    break
                dim = int(rows[0]["dim"])
                mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32)
                if mat.size != len(rows) * dim:
                    logger.warning("向量数据长度异常，跳过该批次")
                    continue
                mat = mat.reshape(len(rows), dim)
                sims = mat @ qvec
                for cid, sim in zip((r["cid"] for r in rows), sims.tolist()):
                    vec_score[cid] = float(sim)

            # ---------- 混合排序 ----------
            candidates = set(kw_score) | set(vec_score)
            if not candidates:
                return []

            vecs = np.array([vec_score.get(c, 0.0) for c in candidates], dtype=np.float32)
            kws = np.array([kw_score.get(c, 0.0) for c in candidates], dtype=np.float32)

            def _norm(arr: np.ndarray) -> np.ndarray:
                lo, hi = float(arr.min()), float(arr.max())
                if hi - lo < 1e-9:
                    return np.zeros_like(arr)
                return (arr - lo) / (hi - lo)

            final = vector_weight * _norm(vecs) + (1.0 - vector_weight) * _norm(kws)

            cid_list = list(candidates)
            order = np.argsort(-final)
            picked: List[Tuple[str, float]] = []
            for idx in order:
                cid = cid_list[int(idx)]
                vsim = vec_score.get(cid, 0.0)
                khit = kw_score.get(cid, 0.0)
                # 既没有语义相关、也没有关键词命中 → 丢弃，避免噪音进提示词
                if vsim < threshold and khit <= 0:
                    continue
                picked.append((cid, vsim))
                if len(picked) >= max(top_k, 1):
                    break

            if not picked:
                return []

            # ---------- 回表取正文 ----------
            ids = [c for c, _ in picked]
            info: Dict[str, Dict[str, Any]] = {}
            for i in range(0, len(ids), _INSERT_BATCH):
                part = ids[i : i + _INSERT_BATCH]
                marks = ",".join("?" * len(part))
                rows = await (await db.execute(
                    f"SELECT id, document_id, content, page, ordinal FROM idx_chunks "
                    f"WHERE id IN ({marks})",
                    part,
                )).fetchall()
                for r in rows:
                    info[r["id"]] = dict(r)

        out: List[Dict[str, Any]] = []
        for cid, vsim in picked:
            row = info.get(cid)
            if not row:
                continue
            page = row["page"]
            out.append(
                {
                    "id": cid,
                    "content": row["content"],
                    "document_id": row["document_id"],
                    "document_keyword": "",
                    "similarity": round(float(vsim), 6),
                    "chunk_index": row["ordinal"],
                    "token_num": 0,
                    # 对齐 RAGFlow 的位置字段，末位是页码（chat.py 的 _extract_page 依赖它）
                    "positions": f"[[0, 0, 0, {page}]]" if page else None,
                }
            )
        return out


_store: Optional[IndexStore] = None


def get_index_store() -> IndexStore:
    global _store
    if _store is None:
        _store = IndexStore()
    return _store
