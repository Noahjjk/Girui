"""问答接口：会话管理、流式回答、引用溯源、检索预览、附件上传。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models.chat import ChatMessage, ChatSession
from app.models.enums import LogStatus, MessageRole
from app.models.user import User
from app.schemas.chat import (
    Attachment,
    ChatRequest,
    FeedbackRequest,
    MessageOut,
    RetrievalPreviewRequest,
    SessionCreate,
    SessionDetail,
    SessionOut,
    SessionUpdate,
)
from app.schemas.common import OkResponse, Page
from app.schemas.converters import message_to_out, session_to_out
from app.services import audit, chat as chat_service, llm as llm_service, storage
from app.services.prompt import build_title
from app.services.retriever import RetrievalError, RetrievalNotConfigured

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["问答"])


# ---------------------------------------------------------------- 会话


async def _get_session(db: AsyncSession, session_id: int, user: User) -> ChatSession:
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id, ChatSession.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return session


@router.get("/sessions", response_model=Page[SessionOut], summary="我的会话列表")
async def list_sessions(
    keyword: Optional[str] = None,
    include_archived: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    conditions = [ChatSession.user_id == user.id]
    if not include_archived:
        conditions.append(ChatSession.is_archived.is_(False))
    if keyword:
        conditions.append(ChatSession.title.ilike(f"%{keyword.strip()}%"))

    total = int(
        (await db.execute(select(func.count(ChatSession.id)).where(*conditions))).scalar() or 0
    )
    rows = (
        await db.execute(
            select(ChatSession)
            .where(*conditions)
            .order_by(ChatSession.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return Page[SessionOut](
        items=[session_to_out(s) for s in rows], total=total, page=page, page_size=page_size
    )


@router.post("/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED, summary="新建会话")
async def create_session(
    payload: SessionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    default_provider = await llm_service.get_default_provider_id(db)
    session = ChatSession(
        user_id=user.id,
        title=payload.title or "新对话",
        kb_ids=payload.kb_ids or [],
        model_provider_id=payload.model_provider_id or default_provider,
    )
    db.add(session)
    await db.flush()
    return session_to_out(session)


@router.get("/sessions/{session_id}", response_model=SessionDetail, summary="会话详情（含消息）")
async def get_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session(db, session_id, user)
    messages = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id)
        )
    ).scalars().all()
    detail = SessionDetail(**session_to_out(session).model_dump())
    detail.messages = [message_to_out(m) for m in messages]
    return detail


@router.patch("/sessions/{session_id}", response_model=SessionOut, summary="修改会话")
async def update_session(
    session_id: int,
    payload: SessionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session(db, session_id, user)
    for field in ("title", "kb_ids", "model_provider_id", "is_archived"):
        value = getattr(payload, field)
        if value is not None:
            setattr(session, field, value)
    await db.flush()
    return session_to_out(session)


@router.delete("/sessions/{session_id}", response_model=OkResponse, summary="删除会话")
async def delete_session(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await _get_session(db, session_id, user)
    await db.delete(session)
    await audit.record(
        "chat.session_delete", db=db, request=request, user=user,
        resource_type="session", resource_id=session_id,
    )
    return OkResponse(message="会话已删除")


@router.post("/messages/{message_id}/feedback", response_model=OkResponse, summary="回答评价")
async def feedback(
    message_id: int,
    payload: FeedbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    msg = (
        await db.execute(
            select(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(ChatMessage.id == message_id, ChatSession.user_id == user.id)
        )
    ).scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")
    msg.feedback = payload.feedback
    await db.flush()
    return OkResponse(message="感谢反馈")


# ---------------------------------------------------------------- 附件上传


@router.post("/upload", response_model=List[Attachment], summary="上传提问附件（图片/文件）")
async def upload_attachment(
    files: List[UploadFile] = File(...),
    user: User = Depends(get_current_user),
):
    if len(files) > 10:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="单次最多上传 10 个附件")

    out: List[Attachment] = []
    for upload in files:
        name = storage.safe_display_name(upload.filename or "unnamed")
        ext = storage.get_ext(name)
        is_image = ext in settings.allowed_image_ext_set
        try:
            content = await upload.read()
            stored = await storage.save_upload(
                name, content,
                mime=upload.content_type or "",
                category="attachments",
                image=is_image,
            )
        except storage.UploadError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

        text = "" if is_image else storage.extract_text(stored.absolute_path, stored.ext)
        out.append(
            Attachment(
                type="image" if is_image else "file",
                name=stored.original_name,
                path=stored.relative_path,
                mime=stored.mime,
                size=stored.size,
                text=text or None,
            )
        )
    return out


# ---------------------------------------------------------------- 检索预览


@router.post("/retrieval-preview", summary="仅查看检索与权限过滤结果（不调用大模型）")
async def retrieval_preview(
    payload: RetrievalPreviewRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    outcome = await chat_service.retrieve(
        db, user, payload.question, payload.kb_ids, payload.top_n
    )
    return {
        "raw_candidates": outcome.raw_count,
        "dropped_by_permission": outcome.dropped_by_permission,
        "kept": len(outcome.chunks),
        "scope": outcome.scope_stats,
        "reason": outcome.reason,
        "error": outcome.error,
        "citations": outcome.citations,
    }


# ---------------------------------------------------------------- 问答主流程


async def _prepare(
    db: AsyncSession, user: User, payload: ChatRequest
) -> Dict[str, Any]:
    """公共准备：会话、消息落库、检索、提示词、模型。"""
    started = time.perf_counter()

    # ---- 会话 ----
    if payload.session_id:
        session = await _get_session(db, payload.session_id, user)
    else:
        default_provider = await llm_service.get_default_provider_id(db)
        session = ChatSession(
            user_id=user.id,
            title=build_title(payload.question),
            kb_ids=payload.kb_ids or [],
            model_provider_id=payload.model_provider_id or default_provider,
        )
        db.add(session)
        await db.flush()

    if payload.kb_ids is not None:
        session.kb_ids = payload.kb_ids
    if payload.model_provider_id:
        session.model_provider_id = payload.model_provider_id

    # ---- 用户消息落库 ----
    user_msg = ChatMessage(
        session_id=session.id,
        role=MessageRole.USER,
        content=payload.question,
        attachments=[a.model_dump() for a in payload.attachments],
    )
    db.add(user_msg)
    await db.flush()

    # ---- 图片转 data URI（供视觉模型使用） ----
    image_uris: List[str] = []
    for att in payload.attachments:
        if att.type == "image" and att.path:
            uri = storage.to_data_uri(att.path, att.mime)
            if uri:
                image_uris.append(uri)

    # ---- 检索 + 权限过滤 ----
    outcome = await chat_service.retrieve(
        db, user, payload.question, session.kb_ids, payload.top_n
    )

    # ---- 历史 ----
    history_msgs = (
        await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session.id, ChatMessage.id < user_msg.id)
            .order_by(ChatMessage.id)
        )
    ).scalars().all()
    history = await chat_service.build_history(history_msgs)

    # ---- 模型 ----
    provider = await llm_service.get_provider(db, session.model_provider_id or payload.model_provider_id)
    session.model_provider_id = provider.id
    session.model_name = provider.name

    messages = chat_service.build_answer_prompt(
        question=payload.question,
        outcome=outcome,
        history=history,
        attachments=[a.model_dump() for a in payload.attachments],
        image_data_uris=image_uris,
        supports_vision=provider.supports_vision,
    )

    return {
        "session": session,
        "user_msg": user_msg,
        "outcome": outcome,
        "provider": provider,
        "messages": messages,
        "started": started,
    }


@router.post("/completions", summary="发起问答（流式 SSE 或一次性返回）")
async def completions(
    payload: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not payload.question.strip() and not payload.attachments:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="请输入问题或上传附件")

    try:
        prep = await _prepare(db, user, payload)
    except llm_service.LLMError as exc:
        # 系统里一个模型都没配时走的就是这条路。这是配置缺失而非服务故障，
        # 必须给出「去哪儿配」的可操作提示，不能让它冒成 500。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    session: ChatSession = prep["session"]
    outcome = prep["outcome"]
    provider = prep["provider"]
    user_msg: ChatMessage = prep["user_msg"]

    await db.commit()

    # 如果有检索到知识库内容，或虽然未检索到知识库内容但用户提出了通用问题，正常调用大模型作答
    # 仅当检索层发生后端错误时抛出异常
    if outcome.error:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=outcome.error)

    client = llm_service.build_client(provider)

    # ---------------- 非流式 ----------------
    if not payload.stream:
        try:
            result = await client.complete(
                prep["messages"], temperature=payload.temperature
            )
        except llm_service.LLMError as exc:
            await audit.record(
                "chat.complete", user=user, request=request,
                status=LogStatus.FAILED, error=str(exc),
                detail={"session_id": session.id, "model": provider.name},
            )
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"模型调用失败：{exc}")

        assistant = ChatMessage(
            session_id=session.id, role=MessageRole.ASSISTANT,
            content=result.content, citations=outcome.citations,
            hit_chunk_ids=[c["chunk_id"] for c in outcome.citations],
            model_name=provider.name, usage=result.usage,
            latency_ms=int((time.perf_counter() - prep["started"]) * 1000),
        )
        db.add(assistant)
        session.message_count = (session.message_count or 0) + 2
        await db.flush()

        await audit.record(
            "chat.complete", db=db, request=request, user=user,
            resource_type="session", resource_id=session.id,
            detail={
                "model": provider.name, "question": payload.question[:500],
                "raw_candidates": outcome.raw_count,
                "dropped_by_permission": outcome.dropped_by_permission,
                "citations": len(outcome.citations),
                "scope": outcome.scope_stats,
            },
            duration_ms=assistant.latency_ms,
        )
        return {
            "session_id": session.id,
            "message_id": assistant.id,
            "content": result.content,
            "citations": outcome.citations,
            "model": provider.name,
            "usage": result.usage,
            "latency_ms": assistant.latency_ms,
        }

    # ---------------- 流式 SSE ----------------
    return StreamingResponse(
        _stream_answer(
            session_id=session.id,
            title=session.title,
            provider=provider,
            client=client,
            messages=prep["messages"],
            citations=outcome.citations,
            question=payload.question,
            temperature=payload.temperature,
            user_id=user.id,
            username=user.username,
            started=prep["started"],
            meta={
                "raw_candidates": outcome.raw_count,
                "dropped_by_permission": outcome.dropped_by_permission,
                "scope": outcome.scope_stats,
            },
            request=request,
        ),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


def _sse(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_headers() -> Dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


async def _stream_answer(
    *,
    session_id: int,
    title: str,
    provider,
    client,
    messages: List[Dict[str, Any]],
    citations: List[Dict[str, Any]],
    question: str,
    temperature: Optional[float],
    user_id: int,
    username: str,
    started: float,
    meta: Dict[str, Any],
    request: Request,
) -> AsyncIterator[str]:
    """流式生成回答，期间用独立会话落库，避免请求级会话提前关闭。"""
    buffer: List[str] = []
    usage: Dict[str, Any] = {}
    error: Optional[str] = None
    message_id: Optional[int] = None

    yield _sse("meta", {
        "session_id": session_id,
        "title": title,
        "model": provider.name,
        "citations": citations,
        "scope": meta.get("scope"),
        "dropped_by_permission": meta.get("dropped_by_permission"),
        "has_context": bool(citations),
    })

    # 注意：不再硬编码在前端输出 notice 提示，避免与大模型内部生成的说明重复出现两遍。
    # 大模型已在系统提示词引导下自然作答，无需前置拼接重复 notice。

    try:
        async for chunk in client.stream(messages, temperature=temperature):
            ctype = chunk.get("type")
            if ctype == "delta":
                text = chunk.get("text") or ""
                buffer.append(text)
                yield _sse("delta", {"text": text})
            elif ctype == "usage":
                usage = chunk.get("usage") or {}
            elif ctype == "finish":
                pass
    except llm_service.LLMError as exc:
        error = str(exc)
        logger.warning("模型流式调用失败：%s", exc)
        yield _sse("error", {"message": f"模型调用失败：{exc}"})
    except asyncio.CancelledError:
        error = "客户端中断"
        raise
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        logger.exception("流式生成异常")
        yield _sse("error", {"message": f"服务异常：{exc}"})
    finally:
        latency = int((time.perf_counter() - started) * 1000)
        content = "".join(buffer)
        try:
            async with SessionLocal() as s:
                assistant = ChatMessage(
                    session_id=session_id,
                    role=MessageRole.ASSISTANT,
                    content=content,
                    citations=citations,
                    hit_chunk_ids=[c.get("chunk_id") for c in citations if c.get("chunk_id")],
                    model_name=provider.name,
                    usage=usage,
                    latency_ms=latency,
                    error=error,
                )
                s.add(assistant)
                sess = (
                    await s.execute(select(ChatSession).where(ChatSession.id == session_id))
                ).scalar_one_or_none()
                if sess is not None:
                    sess.message_count = (sess.message_count or 0) + 2
                await s.commit()
                await s.refresh(assistant)
                message_id = assistant.id
                if usage:
                    yield _sse("usage", {"usage": usage, "latency_ms": latency})
                yield _sse("done", {"message_id": message_id, "session_id": session_id})
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存回答失败：%s", exc)
            yield _sse("done", {"message_id": None, "session_id": session_id})

        await audit.record(
            "chat.complete",
            request=request,
            username=username,
            status=LogStatus.FAILED if error else LogStatus.SUCCESS,
            error=error,
            duration_ms=latency,
            resource_type="session",
            resource_id=session_id,
            detail={
                "user_id": user_id,
                "model": provider.name,
                "question": question[:500],
                "raw_candidates": meta.get("raw_candidates"),
                "dropped_by_permission": meta.get("dropped_by_permission"),
                "citations": len(citations),
                "scope": meta.get("scope"),
                "usage": usage,
                "answer_chars": len(content),
            },
        )
