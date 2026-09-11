"""问答编排：检索 → 权限过滤 → 引用构造。

这是「不同用户检索可见的知识范围相互独立」的执行点：
权限过滤发生在构造提示词之前，不可见片段不会进入大模型上下文。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.services.permission import PermissionScope, PermissionService
from app.services.prompt import build_messages
from app.services.retriever import (
    RetrievalError,
    RetrievalNotConfigured,
    retrieval_client,
)

logger = logging.getLogger(__name__)


@dataclass
class RetrievalOutcome:
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    citations: List[Dict[str, Any]] = field(default_factory=list)
    raw_count: int = 0
    dropped_by_permission: int = 0
    scope_stats: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def has_context(self) -> bool:
        return bool(self.chunks)


async def retrieve(
    db: AsyncSession,
    user: User,
    question: str,
    kb_ids: Optional[Sequence[int]] = None,
    top_n: Optional[int] = None,
) -> RetrievalOutcome:
    outcome = RetrievalOutcome()
    question = (question or "").strip()

    perm = PermissionService(db)
    scope: PermissionScope = await perm.resolve_scope(user, kb_ids)
    outcome.scope_stats = scope.stats()

    if not scope.dataset_ids:
        outcome.reason = "当前账号未被授权访问任何知识库，请联系管理员分配权限。"
        return outcome
    if not scope.search_docs:
        outcome.reason = "所选知识库中当前账号没有任何可见文档。"
        return outcome
    if not question:
        outcome.reason = "没有可检索的问题内容。"
        return outcome

    try:
        data = await retrieval_client.retrieval(
            question=question,
            dataset_ids=scope.dataset_ids,
            # 用 search_docs 而非 visible_docs：restricted 文档里被显式 allow 的片段
            # 也必须参与召回，否则片段级白名单授权永远生效不了。
            document_ids=sorted(scope.search_docs),
            top_k=settings.RETRIEVAL_TOP_K,
        )
    except RetrievalNotConfigured as exc:
        outcome.error = str(exc)
        return outcome
    except RetrievalError as exc:
        outcome.error = f"检索服务不可用：{exc}"
        return outcome

    raw_chunks = list(data.get("chunks") or [])
    outcome.raw_count = len(raw_chunks)

    # ---- 关键：逐条复核权限，不可见片段在此处被丢弃 ----
    visible_chunks = scope.filter_chunks(raw_chunks)
    outcome.dropped_by_permission = len(raw_chunks) - len(visible_chunks)

    # 去重（同一片段可能被多个知识库召回）
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for ch in visible_chunks:
        cid = ch.get("id")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)
        deduped.append(ch)

    deduped.sort(key=lambda c: float(c.get("similarity") or 0), reverse=True)
    # 过滤掉相似度低于阈值的低相关片段，避免在原文溯源中出现
    threshold = settings.SIMILARITY_THRESHOLD
    filtered_chunks = [ch for ch in deduped if float(ch.get("similarity") or 0) >= threshold]
    limit = top_n or settings.RETRIEVAL_TOP_N
    selected = filtered_chunks[:limit]

    chunks: List[Dict[str, Any]] = []
    citations: List[Dict[str, Any]] = []
    for idx, ch in enumerate(selected, start=1):
        did = ch.get("document_id") or ""
        meta = scope.doc_meta.get(did, {})
        content = (ch.get("content") or "").strip()
        citation = {
            "index": idx,
            "chunk_id": ch.get("id") or "",
            "document_id": did,
            "document_name": meta.get("name") or ch.get("document_keyword") or "未命名文档",
            "kb_id": meta.get("kb_id"),
            "kb_name": meta.get("kb_name"),
            "content": content,
            "similarity": round(float(ch.get("similarity") or 0), 4),
            "original_url": meta.get("original_url"),
            "author": meta.get("author"),
            "page": _extract_page(ch.get("positions")),
        }
        citations.append(citation)
        chunks.append(citation)

    outcome.chunks = chunks
    outcome.citations = citations

    if not chunks:
        outcome.reason = "知识库中未检索到与问题相关的内容。"

    logger.info(
        "检索完成 user=%s 候选=%d 过滤后=%d 丢弃=%d 问题=%r",
        user.username, outcome.raw_count, len(chunks), outcome.dropped_by_permission, question[:60],
    )
    return outcome


def _extract_page(positions: Any) -> Optional[int]:
    """RAGFlow 的 positions 形如 [[0, 120, 3, 1]] 或 "[[...]]"，末位为页码。"""
    import json

    if not positions:
        return None
    try:
        data = json.loads(positions) if isinstance(positions, str) else positions
        if isinstance(data, list) and data and isinstance(data[0], list) and len(data[0]) >= 4:
            page = data[0][3]
            return int(page) if page else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    return None


# ---------------------------------------------------------------- 历史消息


async def build_history(
    session_messages: Sequence[Any],
    rounds: Optional[int] = None,
) -> List[Dict[str, str]]:
    """把历史消息裁剪为模型可用的对话记录，并剥离引用标记以外的噪音。"""
    rounds = rounds or settings.CHAT_HISTORY_ROUNDS
    history: List[Dict[str, str]] = []
    tail = list(session_messages)[-(rounds * 2):]
    for msg in tail:
        role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        content = (msg.content or "").strip()
        if not content:
            continue
        if role == "user":
            # 附件不该重复塞进历史，避免上下文膨胀
            history.append({"role": "user", "content": content[:4000]})
        else:
            history.append({"role": "assistant", "content": content[:4000]})
    return history


def build_answer_prompt(
    question: str,
    outcome: RetrievalOutcome,
    history: Optional[Sequence[Dict[str, str]]] = None,
    attachments: Optional[Sequence[Dict[str, Any]]] = None,
    image_data_uris: Optional[Sequence[str]] = None,
    supports_vision: bool = False,
) -> List[Dict[str, Any]]:
    return build_messages(
        question=question,
        chunks=outcome.chunks,
        history=history,
        attachments=attachments,
        image_data_uris=image_data_uris,
        supports_vision=supports_vision,
    )
