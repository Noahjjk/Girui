"""检索后端统一入口。

对外暴露的方法表面与 RAGFlow 客户端完全一致，因此
knowledge.py / chat.py / system.py 这些调用方不需要知道
底层到底是「本地 SQLite 轻量内核」还是「外部 RAGFlow 服务」。

切换方式：环境变量 RETRIEVAL_BACKEND=local | ragflow
  local  —— 内置轻量内核，SQLite + 本地 ONNX 嵌入，适配小内存服务器
  ragflow —— 外部 RAGFlow 服务，需要 4 核 / 16 GB 级别机器

本地后端的 dataset_id / document_id 都是自己生成的 32 位 hex，
对上层（含权限层）来说仍然是不透明字符串，所以权限逻辑零改动。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.services.errors import (  # noqa: F401  (转出给调用方使用)
    RetrievalBackendError,
    RetrievalError,
    RetrievalNotConfigured,
)
from app.services.ingest import get_ingest_manager
from app.services.index_store import get_index_store
from app.services.embedding import get_embedder

logger = logging.getLogger(__name__)

# RAGFlow 侧的解析状态 → 我们统一的状态词
_STATE_TO_RUN = {
    "pending": "UNSTART",
    "parsing": "RUNNING",
    "done": "DONE",
    "failed": "FAIL",
}


def new_id() -> str:
    return uuid.uuid4().hex


class LocalRetriever:
    """基于 SQLite + 本地 ONNX 嵌入的轻量检索内核。"""

    # ------------------------------------------------------------ 连通性

    async def ping(self) -> bool:
        try:
            store = get_index_store()
            await store.connect()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("轻量检索内核不可用：%s", exc)
            return False

    async def version(self) -> Optional[str]:
        return f"local-sqlite/{settings.EMBEDDING_MODEL}"

    async def stats(self) -> Dict[str, Any]:
        try:
            return await get_index_store().stats()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    def queue_status(self) -> Dict[str, Any]:
        return get_ingest_manager().status()

    # ------------------------------------------------------------ 知识库（dataset）

    async def create_dataset(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "",
        chunk_method: str = "naive",
        permission: str = "me",
    ) -> Dict[str, Any]:
        await get_index_store().connect()
        return {"id": new_id(), "name": name, "description": description or f"极睿知识库 - {name}"}

    async def list_datasets(self, page: int = 1, page_size: int = 100) -> List[Dict[str, Any]]:
        return []

    async def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        return None

    async def update_dataset(self, dataset_id: str, **fields: Any) -> Any:
        # 本地内核的知识库元数据以业务库为准，这里无需同步
        return {"id": dataset_id}

    async def delete_datasets(self, dataset_ids: List[str]) -> Any:
        store = get_index_store()
        removed = 0
        for ds in dataset_ids:
            removed += await store.delete_dataset(ds)
        logger.info("已删除本地索引数据集 %s，清理片段 %d 条", dataset_ids, removed)
        return {"deleted": len(dataset_ids), "chunks_removed": removed}

    # ------------------------------------------------------------ 文档

    async def upload_documents(
        self,
        dataset_id: str,
        files: Sequence[Tuple[str, bytes, str]],
        metas: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """登记文档并落索引库，真正的解析交给后台工作者。

        与 RAGFlow 不同，这里上传即拿到最终 document_id，
        后续 parse_documents 只负责触发解析。
        """
        store = get_index_store()
        await store.connect()

        out: List[Dict[str, Any]] = []
        metas = list(metas or [])
        for idx, (name, _content, _mime) in enumerate(files):
            meta = metas[idx] if idx < len(metas) else {}
            doc_id = new_id()
            await store.upsert_document(
                doc_id,
                dataset_id,
                meta.get("name") or name,
                state="pending",
                source_path=meta.get("relative_path") or meta.get("storage_path"),
                ext=meta.get("ext"),
            )
            out.append({"id": doc_id, "name": meta.get("name") or name})
        return out

    async def list_documents(
        self,
        dataset_id: str,
        page: int = 1,
        page_size: int = 100,
        keywords: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows, total = await get_index_store().list_documents(dataset_id, page, page_size, keywords)
        docs = [
            {
                "id": r["id"],
                "name": r["name"],
                "run": _STATE_TO_RUN.get(r["state"], "UNSTART"),
                "progress": float(r.get("progress") or 0.0),
                "chunk_count": int(r.get("chunk_count") or 0),
                "token_count": int(r.get("token_count") or 0),
                "progress_msg": r.get("message") or "",
            }
            for r in rows
        ]
        return {"docs": docs, "total": total}

    async def delete_documents(self, dataset_id: str, document_ids: List[str]) -> Any:
        store = get_index_store()
        removed = 0
        for did in document_ids:
            removed += await store.delete_document(did)
        return {"deleted": len(document_ids), "chunks_removed": removed}

    async def parse_documents(self, dataset_id: str, document_ids: List[str]) -> Any:
        """把文档交给后台工作者解析。"""
        store = get_index_store()
        await store.connect()
        manager = get_ingest_manager()

        accepted: List[str] = []
        for did in document_ids:
            info = await store.get_document(did)
            if info is None:
                logger.warning("索引库中不存在该文档，跳过解析：%s", did)
                continue
            # 显式重置状态：工作者会跳过已完成且非空的文档，重新解析必须清掉
            await store.set_document_state(
                did, "pending", progress=0.0, chunk_count=0, token_count=0, message="排队中"
            )
            accepted.append(did)

        await manager.enqueue_many(accepted)
        return {"queued": len(accepted)}

    async def stop_parse(self, dataset_id: str, document_ids: List[str]) -> Any:
        for did in document_ids:
            await get_index_store().set_document_state(
                did, "failed", progress=0.0, message="已取消"
            )
        return {"stopped": len(document_ids)}

    # ------------------------------------------------------------ 片段

    async def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        page: int = 1,
        page_size: int = 100,
        keywords: Optional[str] = None,
    ) -> Dict[str, Any]:
        rows, total = await get_index_store().list_chunks(
            dataset_id, document_id, page, page_size, keywords
        )
        chunks = [
            {
                "id": r["id"],
                "content": r["content"],
                "document_id": r["document_id"],
                "chunk_index": r["ordinal"],
                "token_num": r["tokens"],
                "page": r.get("page"),
            }
            for r in rows
        ]
        return {"chunks": chunks, "total": total}

    async def list_all_chunks(
        self, dataset_id: str, document_id: str, max_chunks: int = 5000
    ) -> List[Dict[str, Any]]:
        return await get_index_store().all_chunks(dataset_id, document_id, max_chunks)

    # ------------------------------------------------------------ 检索

    async def retrieval(
        self,
        question: str,
        dataset_ids: List[str],
        document_ids: Optional[List[str]] = None,
        top_k: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
        vector_similarity_weight: Optional[float] = None,
        rerank_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """向量 + 全文混合检索。

        document_ids 是权限收敛的关键入口：不可见文档的片段根本不参与召回；
        调用方（chat.py）随后还会做一次片段级复核，双保险。
        """
        if not dataset_ids or not document_ids:
            return {"chunks": [], "doc_aggs": [], "total": 0}

        question = (question or "").strip()
        if not question:
            return {"chunks": [], "doc_aggs": [], "total": 0}

        # 优先读取目标知识库指定的嵌入模型
        kb_embedding_model = None
        try:
            from sqlalchemy import select
            from app.db.session import SessionLocal
            from app.models.knowledge import KnowledgeBase
            async with SessionLocal() as db:
                first_dataset = dataset_ids[0] if dataset_ids else None
                if first_dataset:
                    kb_obj = (
                        await db.execute(
                            select(KnowledgeBase).where(KnowledgeBase.ragflow_dataset_id == first_dataset)
                        )
                    ).scalar_one_or_none()
                    if kb_obj:
                        kb_embedding_model = kb_obj.embedding_model
        except Exception:
            pass

        embedder = get_embedder(kb_embedding_model)
        try:
            qvec = await asyncio.to_thread(embedder.encode_one, question, True)
        except Exception as exc:  # noqa: BLE001
            raise RetrievalBackendError(f"本地嵌入模型不可用：{exc}") from exc

        chunks = await get_index_store().search(
            question=question,
            dataset_ids=list(dataset_ids),
            document_ids=list(document_ids),
            query_vector=qvec,
            top_k=top_k or settings.RETRIEVAL_TOP_K,
            vector_weight=settings.HYBRID_VECTOR_WEIGHT,
            threshold=similarity_threshold if similarity_threshold is not None
            else settings.SIMILARITY_THRESHOLD,
        )
        return {"chunks": chunks, "doc_aggs": [], "total": len(chunks)}


local_retriever = LocalRetriever()


def _select_backend():
    if settings.use_local_retrieval:
        logger.info("检索后端：本地轻量内核（SQLite + %s）", settings.EMBEDDING_MODEL)
        return local_retriever
    logger.info("检索后端：外部 RAGFlow（%s）", settings.RAGFLOW_BASE_URL)
    from app.services.ragflow import ragflow_client

    return ragflow_client


retrieval_client = _select_backend()

__all__ = [
    "LocalRetriever",
    "local_retriever",
    "retrieval_client",
    "RetrievalError",
    "RetrievalNotConfigured",
    "RetrievalBackendError",
]
