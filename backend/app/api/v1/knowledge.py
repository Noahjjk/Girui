"""知识库与文档管理。含 Windows 本地文件上传到 Ubuntu 服务器的接口。"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_admin
from app.db.session import get_db
from app.models.enums import DocumentStatus, LogStatus, UserRole, Visibility
from app.models.knowledge import KbDocument, KbFolder, KbPermission, KnowledgeBase
from app.models.user import User
from app.schemas.common import OkResponse, Page
from app.schemas.converters import document_to_out, kb_to_out
from app.schemas.knowledge import (
    BatchUploadResult,
    ChunkPreview,
    DocumentOut,
    DocumentUpdate,
    KnowledgeBaseCreate,
    KnowledgeBaseOut,
    KnowledgeBaseUpdate,
    UploadResult,
)
from app.services import audit, storage
from app.services.permission import PermissionService
from app.services.retriever import (
    RetrievalError,
    RetrievalNotConfigured,
    retrieval_client,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb", tags=["知识库"])

MAX_FILES_PER_REQUEST = 20


# ---------------------------------------------------------------- 内部工具


async def _get_kb(db: AsyncSession, kb_id: int) -> KnowledgeBase:
    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    return kb


async def _refresh_kb_counters(db: AsyncSession, kb: KnowledgeBase) -> None:
    row = (
        await db.execute(
            select(func.count(KbDocument.id), func.coalesce(func.sum(KbDocument.chunk_count), 0))
            .where(KbDocument.kb_id == kb.id, KbDocument.status != DocumentStatus.FAILED)
        )
    ).one()
    kb.doc_count = int(row[0] or 0)
    kb.chunk_count = int(row[1] or 0)


async def _ensure_dataset(db: AsyncSession, kb: KnowledgeBase) -> str:
    """知识库在检索侧没有 dataset 时自动创建（本地内核按需生成 ID）。"""
    if kb.ragflow_dataset_id:
        return kb.ragflow_dataset_id
    try:
        data = await retrieval_client.create_dataset(
            name=kb.name,
            description=kb.description or f"极睿知识库 - {kb.name}",
            embedding_model=kb.embedding_model,
            chunk_method=kb.chunk_method,
        )
    except RetrievalNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except RetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"创建知识库失败：{exc}"
        )

    dataset_id = (data or {}).get("id") if isinstance(data, dict) else None
    if not dataset_id:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="检索内核未返回知识库 ID")
    kb.ragflow_dataset_id = dataset_id
    await db.flush()
    return dataset_id


def _extract_uploaded_ids(data) -> List[dict]:
    """不同检索后端的上传返回结构略有差异，做一次兼容。"""
    if isinstance(data, dict):
        for key in ("documents", "data", "items"):
            if isinstance(data.get(key), list):
                return data[key]
        if data.get("id"):
            return [data]
        return []
    if isinstance(data, list):
        return data
    return []


# ---------------------------------------------------------------- 知识库 CRUD


@router.get("", response_model=Page[KnowledgeBaseOut], summary="我可访问的知识库")
async def list_knowledge_bases(
    keyword: Optional[str] = None,
    include_inactive: bool = False,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    perm = PermissionService(db)
    readable = set(await perm.readable_kb_ids(user))
    uploadable = set(await perm.uploadable_kb_ids(user))

    stmt = select(KnowledgeBase)
    count_stmt = select(func.count(KnowledgeBase.id))

    if user.role != UserRole.ADMIN:
        stmt = stmt.where(KnowledgeBase.id.in_(readable or [-1]))
        count_stmt = count_stmt.where(KnowledgeBase.id.in_(readable or [-1]))
    if not include_inactive:
        stmt = stmt.where(KnowledgeBase.is_active.is_(True))
        count_stmt = count_stmt.where(KnowledgeBase.is_active.is_(True))
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(KnowledgeBase.name.ilike(like))
        count_stmt = count_stmt.where(KnowledgeBase.name.ilike(like))

    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        await db.execute(
            stmt.order_by(KnowledgeBase.id).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    items = []
    for kb in rows:
        items.append(
            kb_to_out(
                kb,
                can_read=kb.id in readable,
                can_upload=kb.id in uploadable,
                can_manage=(user.role == UserRole.ADMIN or kb.id in uploadable),
            )
        )
    return Page[KnowledgeBaseOut](items=items, total=total, page=page, page_size=page_size)


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED, summary="新建知识库")
async def create_kb(
    payload: KnowledgeBaseCreate,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    exists = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == payload.name))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="同名知识库已存在")

    kb = KnowledgeBase(
        name=payload.name,
        code=payload.code,
        description=payload.description,
        embedding_model=payload.embedding_model,
        chunk_method=payload.chunk_method,
        owner_id=admin.id,
    )
    db.add(kb)
    await db.flush()

    try:
        await _ensure_dataset(db, kb)
    except HTTPException as exc:
        # 检索后端暂不可用时不阻塞建库，后续上传时再补建
        logger.warning("创建知识库 %s 时未能建立检索数据集：%s", kb.name, exc.detail)

    await audit.record(
        "kb.create", db=db, request=request, user=admin,
        resource_type="kb", resource_id=kb.id, resource_name=kb.name,
        detail={"dataset_id": kb.ragflow_dataset_id},
    )
    return kb_to_out(kb, True, True, True)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut, summary="知识库详情")
async def get_kb(
    kb_id: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    kb = await _get_kb(db, kb_id)
    cap = await PermissionService(db).kb_capabilities(user, kb_id)
    if not cap.can_read:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该知识库")
    return kb_to_out(kb, cap.can_read, cap.can_upload, cap.can_manage)


@router.patch("/{kb_id}", response_model=KnowledgeBaseOut, summary="修改知识库")
async def update_kb(
    kb_id: int,
    payload: KnowledgeBaseUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    await PermissionService(db).assert_kb_manage(user, kb_id)

    for field in ("name", "description", "embedding_model", "chunk_method", "is_active"):
        value = getattr(payload, field)
        if value is not None:
            setattr(kb, field, value)
    await db.flush()

    if kb.ragflow_dataset_id:
        try:
            await retrieval_client.update_dataset(
                kb.ragflow_dataset_id,
                name=kb.name,
                description=kb.description or "",
            )
        except RetrievalError as exc:
            logger.warning("同步知识库元数据失败：%s", exc)

    await audit.record(
        "kb.update", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb.id, resource_name=kb.name,
        detail=payload.model_dump(exclude_unset=True),
    )
    cap = await PermissionService(db).kb_capabilities(user, kb_id)
    return kb_to_out(kb, cap.can_read, cap.can_upload, cap.can_manage)


@router.delete("/{kb_id}", response_model=OkResponse, summary="删除知识库")
async def delete_kb(
    kb_id: int,
    request: Request,
    drop_remote: bool = Query(True, description="是否同时清理检索索引中的数据集"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    name = kb.name
    dataset_id = kb.ragflow_dataset_id

    if drop_remote and dataset_id:
        try:
            await retrieval_client.delete_datasets([dataset_id])
        except RetrievalError as exc:
            logger.warning("清理检索索引失败（继续删除本地记录）：%s", exc)

    await db.delete(kb)
    await audit.record(
        "kb.delete", db=db, request=request, user=admin,
        resource_type="kb", resource_id=kb_id, resource_name=name,
        detail={"dataset_id": dataset_id, "drop_remote": drop_remote},
    )
    return OkResponse(message=f"知识库「{name}」已删除")


# ---------------------------------------------------------------- 文档


@router.get("/{kb_id}/documents", response_model=Page[DocumentOut], summary="文档列表")
async def list_documents(
    kb_id: int,
    keyword: Optional[str] = None,
    doc_status: Optional[DocumentStatus] = Query(None, alias="status"),
    visibility: Optional[Visibility] = None,
    category: Optional[str] = None,
    folder_id: Optional[int] = Query(None, description="按文件夹过滤；0 表示只看根目录下的文档"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await PermissionService(db).assert_kb_read(user, kb_id)

    conditions = [KbDocument.kb_id == kb_id]
    if keyword:
        conditions.append(KbDocument.name.ilike(f"%{keyword.strip()}%"))
    if doc_status is not None:
        conditions.append(KbDocument.status == doc_status)
    if visibility is not None:
        conditions.append(KbDocument.visibility == visibility)
    if category:
        conditions.append(KbDocument.category == category)
    if folder_id is not None:
        if folder_id == 0:
            conditions.append(KbDocument.folder_id.is_(None))
        else:
            conditions.append(KbDocument.folder_id == folder_id)

    total = int(
        (await db.execute(select(func.count(KbDocument.id)).where(*conditions))).scalar() or 0
    )
    rows = (
        await db.execute(
            select(KbDocument)
            .where(*conditions)
            .order_by(KbDocument.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return Page[DocumentOut](
        items=[document_to_out(d) for d in rows], total=total, page=page, page_size=page_size
    )


@router.post(
    "/{kb_id}/documents/upload",
    response_model=BatchUploadResult,
    summary="上传知识文件（支持 Windows 本地多选文件）",
)
async def upload_documents(
    kb_id: int,
    request: Request,
    files: List[UploadFile] = File(..., description="支持多文件；单文件上限见 MAX_UPLOAD_SIZE_MB"),
    visibility: Visibility = Form(Visibility.PUBLIC),
    category: Optional[str] = Form(None),
    tags: Optional[str] = Form(None, description="逗号分隔的标签"),
    original_url: Optional[str] = Form(None, description="原文链接，如影刀社区帖子地址"),
    folder_id: Optional[int] = Form(None, description="归入的文件夹 ID；为空则挂在知识库根下"),
    auto_parse: bool = Form(True, description="上传后立即触发解析切片"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    await PermissionService(db).assert_kb_upload(user, kb_id)

    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未选择任何文件")
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"单次最多上传 {MAX_FILES_PER_REQUEST} 个文件",
        )

    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    dataset_id = await _ensure_dataset(db, kb)

    # 归入的文件夹必须属于本知识库（前端传了才校验）
    target_folder_id: Optional[int] = None
    if folder_id:
        target_folder_id = (
            await db.execute(
                select(KbFolder.id).where(KbFolder.id == folder_id, KbFolder.kb_id == kb_id)
            )
        ).scalar_one_or_none()
        if target_folder_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="目标文件夹不属于该知识库"
            )

    results: List[UploadResult] = []
    succeeded_docs: List[KbDocument] = []

    for upload in files:
        display_name = storage.safe_display_name(upload.filename or "unnamed")
        try:
            content = await upload.read()
            stored = await storage.save_upload(
                display_name, content, mime=upload.content_type or "", category=f"kb_{kb_id}"
            )
        except storage.UploadError as exc:
            results.append(UploadResult(success=False, name=display_name, message=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("保存上传文件失败")
            results.append(UploadResult(success=False, name=display_name, message=str(exc)))
            continue

        try:
            remote = await retrieval_client.upload_documents(
                dataset_id,
                [(stored.original_name, content, stored.mime)],
                metas=[
                    {
                        "name": stored.original_name,
                        "relative_path": stored.relative_path,
                        "ext": stored.ext,
                    }
                ],
            )
        except (RetrievalError, RetrievalNotConfigured) as exc:
            results.append(
                UploadResult(
                    success=False, name=display_name,
                    message=f"上传到检索内核失败：{exc}",
                )
            )
            continue

        items = _extract_uploaded_ids(remote)
        if not items:
            results.append(
                UploadResult(
                    success=False, name=display_name,
                    message="检索内核未返回文档标识",
                )
            )
            continue

        for item in items:
            ragflow_doc_id = str(item.get("id") or "")
            if not ragflow_doc_id:
                continue
            doc = KbDocument(
                kb_id=kb.id,
                ragflow_document_id=ragflow_doc_id,
                name=item.get("name") or stored.original_name,
                file_type=stored.ext,
                size_bytes=stored.size,
                category=category,
                source="upload",
                original_url=original_url,
                folder_id=target_folder_id,
                storage_path=stored.relative_path,
                checksum=stored.checksum,
                visibility=visibility,
                tags=tag_list,
                status=DocumentStatus.PENDING,
                uploaded_by=user.id,
            )
            db.add(doc)
            succeeded_docs.append(doc)
            results.append(
                UploadResult(
                    success=True, name=doc.name, ragflow_document_id=ragflow_doc_id,
                    message="上传成功",
                )
            )

    await db.flush()

    if auto_parse and succeeded_docs:
        try:
            await retrieval_client.parse_documents(
                dataset_id, [d.ragflow_document_id for d in succeeded_docs]
            )
            for doc in succeeded_docs:
                doc.status = DocumentStatus.PARSING
        except RetrievalError as exc:
            logger.warning("触发解析失败：%s", exc)
            for doc in succeeded_docs:
                doc.status = DocumentStatus.FAILED
                doc.error_message = f"触发解析失败：{exc}"

    await db.flush()
    for doc in succeeded_docs:
        results = [
            r.model_copy(update={"document_id": doc.id})
            if r.success and r.ragflow_document_id == doc.ragflow_document_id
            else r
            for r in results
        ]

    await _refresh_kb_counters(db, kb)

    ok_count = sum(1 for r in results if r.success)
    await audit.record(
        "kb.upload", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb.id, resource_name=kb.name,
        status=LogStatus.SUCCESS if ok_count else LogStatus.FAILED,
        detail={
            "total": len(files), "succeeded": ok_count, "failed": len(results) - ok_count,
            "files": [r.name for r in results],
            "visibility": visibility.value if hasattr(visibility, "value") else str(visibility),
            "tags": tag_list,
        },
    )

    return BatchUploadResult(
        total=len(results), succeeded=ok_count, failed=len(results) - ok_count, results=results
    )


@router.patch("/{kb_id}/documents/{document_id}", response_model=DocumentOut, summary="修改文档属性")
async def update_document(
    kb_id: int,
    document_id: int,
    payload: DocumentUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await PermissionService(db).assert_kb_upload(user, kb_id)
    doc = (
        await db.execute(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    for field in ("name", "category", "visibility", "tags", "original_url", "remark"):
        value = getattr(payload, field)
        if value is not None:
            setattr(doc, field, value)

    # 移动文档到文件夹（0 表示移回根目录）
    if "folder_id" in payload.model_fields_set:
        new_folder = payload.folder_id
        if new_folder in (0, None):
            doc.folder_id = None
        else:
            exists = (
                await db.execute(
                    select(KbFolder.id).where(
                        KbFolder.id == new_folder, KbFolder.kb_id == kb_id
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="目标文件夹不属于该知识库"
                )
            doc.folder_id = new_folder
    await db.flush()

    await audit.record(
        "doc.update", db=db, request=request, user=user,
        resource_type="document", resource_id=doc.id, resource_name=doc.name,
        detail=payload.model_dump(exclude_unset=True),
    )
    return document_to_out(doc)


@router.post("/{kb_id}/documents/{document_id}/parse", response_model=OkResponse, summary="重新解析切片")
async def parse_document(
    kb_id: int,
    document_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    await PermissionService(db).assert_kb_upload(user, kb_id)
    doc = (
        await db.execute(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    if not kb.ragflow_dataset_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="知识库尚未关联检索数据集")

    try:
        await retrieval_client.parse_documents(kb.ragflow_dataset_id, [doc.ragflow_document_id])
    except RetrievalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"触发解析失败：{exc}")

    doc.status = DocumentStatus.PARSING
    doc.error_message = None
    await db.flush()
    await audit.record(
        "doc.parse", db=db, request=request, user=user,
        resource_type="document", resource_id=doc.id, resource_name=doc.name,
    )
    return OkResponse(message="已触发重新解析")


@router.get("/{kb_id}/documents/{document_id}/chunks", response_model=List[ChunkPreview], summary="查看文档片段")
async def list_document_chunks(
    kb_id: int,
    document_id: int,
    keyword: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    await PermissionService(db).assert_kb_read(user, kb_id)
    doc = (
        await db.execute(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    if not kb.ragflow_dataset_id:
        return []

    try:
        chunks = await retrieval_client.list_all_chunks(
            kb.ragflow_dataset_id, doc.ragflow_document_id, max_chunks=limit
        )
    except RetrievalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"读取片段失败：{exc}")

    out: List[ChunkPreview] = []
    for ch in chunks:
        content = ch.get("content") or ch.get("content_with_weight") or ""
        if keyword and keyword not in content:
            continue
        out.append(
            ChunkPreview(
                chunk_id=str(ch.get("id") or ""),
                document_id=doc.ragflow_document_id,
                document_name=doc.name,
                content=content,
                index=ch.get("chunk_index") or ch.get("index"),
                token_count=ch.get("token_num") or ch.get("token_count"),
            )
        )
    return out


@router.post("/{kb_id}/documents/sync", response_model=OkResponse, summary="同步解析状态")
async def sync_documents(
    kb_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    await PermissionService(db).assert_kb_upload(user, kb_id)
    if not kb.ragflow_dataset_id:
        return OkResponse(success=False, message="知识库尚未关联数据集")

    try:
        remote = await retrieval_client.list_documents(kb.ragflow_dataset_id, page=1, page_size=1000)
    except RetrievalError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"同步失败：{exc}")

    remote_docs = remote.get("docs") or remote.get("documents") or []
    mapping = {str(d.get("id")): d for d in remote_docs if d.get("id")}

    local_docs = (
        await db.execute(select(KbDocument).where(KbDocument.kb_id == kb_id))
    ).scalars().all()

    changed = 0
    for doc in local_docs:
        info = mapping.get(doc.ragflow_document_id)
        if not info:
            continue
        run = str(info.get("run") or "").upper()
        progress = float(info.get("progress") or 0)
        doc.progress = progress
        doc.chunk_count = int(info.get("chunk_count") or info.get("chunk_num") or 0)
        doc.token_count = int(info.get("token_count") or info.get("token_num") or 0)
        if run == "DONE":
            doc.status = DocumentStatus.READY
            doc.error_message = None
        elif run in ("RUNNING", "UNSTART"):
            doc.status = DocumentStatus.PARSING
        elif run == "FAIL":
            doc.status = DocumentStatus.FAILED
            doc.error_message = info.get("progress_msg") or "解析失败"
        changed += 1

    await _refresh_kb_counters(db, kb)
    await db.flush()
    await audit.record(
        "kb.sync", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb.id, resource_name=kb.name,
        detail={"synced": changed},
    )
    return OkResponse(message=f"已同步 {changed} 个文档的状态")


@router.delete("/{kb_id}/documents/{document_id}", response_model=OkResponse, summary="删除文档")
async def delete_document(
    kb_id: int,
    document_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    kb = await _get_kb(db, kb_id)
    await PermissionService(db).assert_kb_upload(user, kb_id)
    doc = (
        await db.execute(
            select(KbDocument).where(KbDocument.id == document_id, KbDocument.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")

    name = doc.name
    if kb.ragflow_dataset_id:
        try:
            await retrieval_client.delete_documents(kb.ragflow_dataset_id, [doc.ragflow_document_id])
        except RetrievalError as exc:
            logger.warning("清理检索索引中的文档失败（继续删除本地记录）：%s", exc)

    await db.delete(doc)
    await _refresh_kb_counters(db, kb)
    await db.flush()

    await audit.record(
        "doc.delete", db=db, request=request, user=user,
        resource_type="document", resource_id=document_id, resource_name=name,
    )
    return OkResponse(message=f"文档「{name}」已删除")
