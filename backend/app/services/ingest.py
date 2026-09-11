"""文档入库流水线：解析 → 切片 → 嵌入 → 落索引。

为什么是后台单工作者而不是在请求里同步做：
  嵌入是最吃 CPU 的一步（约 25 ms/片，一篇长文档可能要几十秒）。
  在 2 核机器上如果放在请求里同步跑，一个上传就能把接口全堵住。
  这里用「单队列 + 单工作者」串行处理，既不打满 CPU，也不阻塞接口。

进程重启后 pending/parsing 的文档会被重新入队，不会卡在中途。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.mem import release_memory
from app.services import parser as doc_parser
from app.services import storage
from app.services.chunker import chunk_sections
from app.services.embedding import get_embedder
from app.services.index_store import get_index_store

logger = logging.getLogger(__name__)


class IngestManager:
    def __init__(self) -> None:
        self._queue: "asyncio.Queue[str]" = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._current: Optional[str] = None
        self._done = 0
        self._failed = 0
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        store = get_index_store()
        await store.connect()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="jirui-ingest")

        pending = await store.pending_documents()
        for doc in pending:
            await self._queue.put(doc["id"])
        if pending:
            logger.info("发现 %d 篇未完成解析的文档，已重新入队", len(pending))

    async def stop(self) -> None:
        if self._worker and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._worker = None

    # ------------------------------------------------------------ 入队

    async def enqueue(self, document_id: str) -> None:
        await self._queue.put(document_id)

    async def enqueue_many(self, document_ids: List[str]) -> None:
        for did in document_ids:
            await self._queue.put(did)

    def status(self) -> Dict[str, Any]:
        return {
            "queued": self._queue.qsize(),
            "current": self._current,
            "done": self._done,
            "failed": self._failed,
            "last_error": self._last_error,
            "worker_alive": bool(self._worker and not self._worker.done()),
        }

    # ------------------------------------------------------------ 工作者

    async def _run(self) -> None:
        logger.info("文档入库工作者已启动")
        while True:
            document_id = await self._queue.get()
            self._current = document_id
            try:
                await self._process(document_id)
                self._done += 1
            except asyncio.CancelledError:
                self._current = None
                raise
            except Exception as exc:  # noqa: BLE001
                self._failed += 1
                self._last_error = str(exc)
                logger.exception("文档解析失败 document=%s", document_id)
                await self._mark_failed(document_id, str(exc))
            finally:
                self._current = None
                self._queue.task_done()
                # 每份文档处理完就把堆还给系统。理由见 app/core/mem.py：
                # glibc 不会主动归还空闲堆页，RSS 会停在高水位不降，
                # 文档一多就会撞上 cgroup 的 MemoryMax 被 OOM 杀掉。
                await asyncio.to_thread(release_memory)

    async def _process(self, document_id: str) -> None:
        store = get_index_store()
        info = await store.get_document(document_id)
        if info is None:
            logger.warning("索引库中没有该文档记录，跳过：%s", document_id)
            return
        if info.get("state") == "done" and info.get("chunk_count"):
            logger.info("文档已解析过，跳过：%s", info.get("name"))
            return

        raw_path = info.get("source_path")
        if not raw_path:
            raise RuntimeError("索引库缺少文件路径，无法解析")
        # 索引库存的是相对路径（相对 DATA_DIR/uploads），
        # 这样整体搬迁数据目录后索引依然有效
        path = storage.resolve_upload_path(raw_path) or raw_path
        if not os.path.isfile(path):
            raise RuntimeError(f"源文件已不存在或不可读：{raw_path}")

        dataset_id = info["dataset_id"]
        name = info.get("name") or document_id

        await store.set_document_state(document_id, "parsing", progress=0.1, message="正在解析文档")
        t0 = time.time()

        parsed = await asyncio.to_thread(doc_parser.parse_path, path, info.get("ext"))
        await store.set_document_state(
            document_id, "parsing", progress=0.35,
            message=f"已提取 {parsed.char_count} 字，正在切片",
        )

        chunks = chunk_sections(parsed.sections)
        if not chunks:
            raise RuntimeError("切片结果为空")

        await store.set_document_state(
            document_id, "parsing", progress=0.5,
            message=f"共 {len(chunks)} 个片段，正在生成向量",
        )

        # 自动识别本知识库指定的嵌入模型
        kb_embedding_model = None
        try:
            from sqlalchemy import select
            from app.db.session import SessionLocal
            from app.models.knowledge import KnowledgeBase
            async with SessionLocal() as db:
                kb_obj = (
                    await db.execute(
                        select(KnowledgeBase).where(KnowledgeBase.ragflow_dataset_id == dataset_id)
                    )
                ).scalar_one_or_none()
                if kb_obj:
                    kb_embedding_model = kb_obj.embedding_model
        except Exception:
            pass

        embedder = get_embedder(kb_embedding_model)
        vectors = await asyncio.to_thread(
            embedder.encode, [c.content for c in chunks]
        )

        await store.set_document_state(document_id, "parsing", progress=0.9, message="正在写入索引")
        await store.replace_document_chunks(dataset_id, document_id, chunks, vectors)

        token_total = sum(c.tokens for c in chunks)
        await store.set_document_state(
            document_id, "done",
            chunk_count=len(chunks), token_count=token_total,
            char_count=parsed.char_count, progress=1.0, message=None,
        )
        await self._sync_business(
            document_id, state="done",
            chunk_count=len(chunks), token_count=token_total,
        )
        logger.info(
            "文档解析完成 name=%s 片段=%d 字数=%d 耗时=%.1fs",
            name, len(chunks), parsed.char_count, time.time() - t0,
        )

    async def _mark_failed(self, document_id: str, message: str) -> None:
        try:
            store = get_index_store()
            await store.set_document_state(
                document_id, "failed", progress=0.0, message=message[:1000]
            )
        except Exception:  # noqa: BLE001
            logger.exception("写入失败状态时出错")
        await self._sync_business(document_id, state="failed", message=message[:1000])

    # ------------------------------------------------------------ 回写业务库

    async def _sync_business(
        self,
        document_id: str,
        *,
        state: str,
        chunk_count: int = 0,
        token_count: int = 0,
        message: Optional[str] = None,
    ) -> None:
        """把解析结果写回业务库，让前端状态自动变成「已就绪」。

        刻意用独立会话，不依赖请求上下文 —— 工作者在请求之外运行。
        """
        try:
            from sqlalchemy import func, select

            from app.db.session import SessionLocal
            from app.models.enums import DocumentStatus
            from app.models.knowledge import KbDocument, KnowledgeBase

            async with SessionLocal() as db:
                doc = (
                    await db.execute(
                        select(KbDocument).where(
                            KbDocument.ragflow_document_id == document_id
                        )
                    )
                ).scalar_one_or_none()
                if doc is None:
                    return

                if state == "done":
                    doc.status = DocumentStatus.READY
                    doc.progress = 1.0
                    doc.chunk_count = chunk_count
                    doc.token_count = token_count
                    doc.error_message = None
                else:
                    doc.status = DocumentStatus.FAILED
                    doc.error_message = message or "解析失败"
                await db.flush()

                kb = (
                    await db.execute(
                        select(KnowledgeBase).where(KnowledgeBase.id == doc.kb_id)
                    )
                ).scalar_one_or_none()
                if kb is not None:
                    row = (
                        await db.execute(
                            select(
                                func.count(KbDocument.id),
                                func.coalesce(func.sum(KbDocument.chunk_count), 0),
                            ).where(
                                KbDocument.kb_id == kb.id,
                                KbDocument.status != DocumentStatus.FAILED,
                            )
                        )
                    ).one()
                    kb.doc_count = int(row[0] or 0)
                    kb.chunk_count = int(row[1] or 0)
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("回写业务库状态失败 document=%s", document_id)


_manager: Optional[IngestManager] = None


def get_ingest_manager() -> IngestManager:
    global _manager
    if _manager is None:
        _manager = IngestManager()
    return _manager
