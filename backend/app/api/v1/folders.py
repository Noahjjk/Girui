"""知识库文件夹：多级嵌套目录树 + 文件夹级批量授权。

设计要点（对应「方案 B」）：

* 文件夹是**分组与批量授权的载体**，本身不持有向量数据 —— 一个知识库仍然只有一个索引文件。
* 给父文件夹配一条 allow，其下所有后代文件夹一并放行；文档自身单独配的规则优先级更高，
  因此「整个文件夹收起来、只放行其中一篇」也能做到（详见 services/permission.py）。
* 删除文件夹采取「非空即拒绝」策略：只要里面还有子文件夹或文档，就必须先清空。
  这是刻意的保守设计 —— 权限层级操作不应该有隐式的连带删除。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.enums import AclSubjectType, Visibility
from app.models.knowledge import ChunkAcl, KbDocument, KbFolder, KnowledgeBase
from app.models.user import User
from app.schemas.common import OkResponse
from app.schemas.folder import (
    DocumentMove,
    FolderAclGrant,
    FolderAclOut,
    FolderCreate,
    FolderNode,
    FolderOut,
    FolderTreeOut,
    FolderUpdate,
)
from app.services import audit
from app.services.permission import PermissionService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/kb", tags=["文件夹"])


# ---------------------------------------------------------------- 内部工具


async def _get_kb(db: AsyncSession, kb_id: int) -> KnowledgeBase:
    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    ).scalar_one_or_none()
    if kb is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    return kb


async def _assert_manage(db: AsyncSession, user: User, kb_id: int) -> None:
    await PermissionService(db).assert_kb_manage(user, kb_id)


async def _get_folder(db: AsyncSession, kb_id: int, folder_id: int) -> KbFolder:
    folder = (
        await db.execute(
            select(KbFolder).where(KbFolder.id == folder_id, KbFolder.kb_id == kb_id)
        )
    ).scalar_one_or_none()
    if folder is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件夹不存在")
    return folder


def _parent_path(parent: Optional[KbFolder]) -> str:
    """新节点的 path = 父节点的 path + 父节点 id。

    约定：path 是**带前导斜杠**的 id 串，根级文件夹的 path 为 `"/"`。
    例如 id=3 的文件夹挂在 id=7 下，其 path 为 `"/7/"`，self_path 为 `"/7/3/"`。
    这样「某文件夹及其全部后代」就是一条 `path LIKE '/7/3/%'`。
    """
    if parent is None:
        return "/"
    return f"{parent.path}{parent.id}/"


def _folder_to_out(
    folder: KbFolder,
    doc_count: int = 0,
    child_count: int = 0,
    acl_count: int = 0,
    my_visible: bool = True,
) -> FolderOut:
    return FolderOut(
        id=folder.id,
        kb_id=folder.kb_id,
        parent_id=folder.parent_id,
        name=folder.name,
        path=folder.path,
        depth=folder.depth,
        sort_order=folder.sort_order,
        visibility=folder.visibility,
        description=folder.description,
        created_at=folder.created_at.isoformat() if folder.created_at else None,
        doc_count=doc_count,
        child_count=child_count,
        acl_count=acl_count,
        my_visible=my_visible,
    )


# ---------------------------------------------------------------- 目录树


@router.get("/{kb_id}/folders", response_model=FolderTreeOut, summary="知识库目录树")
async def get_folder_tree(
    kb_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """返回整棵目录树。

    有管理权的用户看到完整结构；普通用户只看到自己可见的分支
    （父级不可见时，整棵子树都不下发，避免通过目录名泄漏结构）。
    """
    await _get_kb(db, kb_id)
    perm = PermissionService(db)
    cap = await perm.kb_capabilities(user, kb_id)
    if not cap.can_read:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该知识库")

    folders = (
        await db.execute(
            select(KbFolder)
            .where(KbFolder.kb_id == kb_id)
            .order_by(KbFolder.depth, KbFolder.sort_order, KbFolder.id)
        )
    ).scalars().all()

    doc_rows = (
        await db.execute(
            select(KbDocument.folder_id, func.count(KbDocument.id))
            .where(KbDocument.kb_id == kb_id)
            .group_by(KbDocument.folder_id)
        )
    ).all()
    doc_count_by_folder: Dict[Optional[int], int] = {fid: int(cnt) for fid, cnt in doc_rows}

    acl_rows = (
        await db.execute(
            select(ChunkAcl.folder_id, func.count(ChunkAcl.id))
            .where(ChunkAcl.kb_id == kb_id, ChunkAcl.folder_id.is_not(None))
            .group_by(ChunkAcl.folder_id)
        )
    ).all()
    acl_count_by_folder: Dict[int, int] = {int(fid): int(cnt) for fid, cnt in acl_rows}

    child_count_by_parent: Dict[Optional[int], int] = {}
    for f in folders:
        child_count_by_parent[f.parent_id] = child_count_by_parent.get(f.parent_id, 0) + 1

    # 普通用户：先算出可见文件夹集合，再做可见性剪枝
    visible_folders: Optional[set] = None
    if not cap.can_manage:
        scope = await perm.resolve_scope(user, [kb_id])
        visible_folders = set(scope.visible_folders)

    nodes: Dict[int, FolderNode] = {}
    for f in folders:
        my_visible = True if visible_folders is None else f.id in visible_folders
        base = _folder_to_out(
            f,
            doc_count=doc_count_by_folder.get(f.id, 0),
            child_count=child_count_by_parent.get(f.id, 0),
            acl_count=acl_count_by_folder.get(f.id, 0),
            my_visible=my_visible,
        )
        nodes[f.id] = FolderNode(**base.model_dump(), children=[])

    roots: List[FolderNode] = []
    for f in folders:
        node = nodes[f.id]
        if visible_folders is not None and not node.my_visible:
            continue  # 剪枝：不可见分支整棵不下发
        parent = nodes.get(f.parent_id) if f.parent_id else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)

    return FolderTreeOut(
        kb_id=kb_id,
        items=roots,
        root_doc_count=doc_count_by_folder.get(None, 0),
        total_folders=len(folders),
    )


# ---------------------------------------------------------------- 增删改


@router.post(
    "/{kb_id}/folders",
    response_model=FolderOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建文件夹",
)
async def create_folder(
    kb_id: int,
    payload: FolderCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_kb(db, kb_id)
    await _assert_manage(db, user, kb_id)

    parent: Optional[KbFolder] = None
    if payload.parent_id:
        parent = await _get_folder(db, kb_id, payload.parent_id)

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件夹名不能为空")

    dup = (
        await db.execute(
            select(KbFolder.id).where(
                KbFolder.kb_id == kb_id,
                KbFolder.parent_id == (parent.id if parent else None),
                KbFolder.name == name,
            )
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="同级下已存在同名文件夹"
        )

    folder = KbFolder(
        kb_id=kb_id,
        parent_id=parent.id if parent else None,
        name=name,
        path=_parent_path(parent),
        depth=(parent.depth + 1) if parent else 0,
        sort_order=payload.sort_order,
        visibility=payload.visibility,
        description=payload.description,
        created_by=user.id,
    )
    db.add(folder)
    await db.flush()

    await audit.record(
        "folder.create", db=db, request=request, user=user,
        resource_type="folder", resource_id=folder.id, resource_name=folder.name,
        detail={"kb_id": kb_id, "parent_id": folder.parent_id, "visibility": folder.visibility.value},
    )
    return _folder_to_out(folder)


@router.patch(
    "/{kb_id}/folders/{folder_id}", response_model=FolderOut, summary="重命名/改可见性/移动文件夹"
)
async def update_folder(
    kb_id: int,
    folder_id: int,
    payload: FolderUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    folder = await _get_folder(db, kb_id, folder_id)

    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="文件夹名不能为空")
        folder.name = name
    if payload.visibility is not None:
        folder.visibility = payload.visibility
    if payload.description is not None:
        folder.description = payload.description
    if payload.sort_order is not None:
        folder.sort_order = payload.sort_order

    if payload.move:
        await _move_folder(db, kb_id, folder, payload.parent_id)

    await db.flush()
    await audit.record(
        "folder.update", db=db, request=request, user=user,
        resource_type="folder", resource_id=folder.id, resource_name=folder.name,
        detail=payload.model_dump(exclude_unset=True),
    )
    return _folder_to_out(folder)


async def _move_folder(
    db: AsyncSession, kb_id: int, folder: KbFolder, new_parent_id: Optional[int]
) -> None:
    """移动文件夹到新父级，并同步修正其全部后代的物化路径。"""
    target_parent: Optional[KbFolder] = None
    if new_parent_id:
        if new_parent_id == folder.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="不能把文件夹移动到自身")
        target_parent = await _get_folder(db, kb_id, new_parent_id)
        # 防环：新父级不能落在自己子树里
        if target_parent.path.startswith(folder.self_path) or target_parent.id == folder.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="不能把文件夹移动到自己的子目录下"
            )

    old_prefix = folder.self_path
    new_parent_path = _parent_path(target_parent)
    new_prefix = f"{new_parent_path}{folder.id}/"
    new_depth = (target_parent.depth + 1) if target_parent else 0
    delta = new_depth - folder.depth

    descendants = (
        await db.execute(
            select(KbFolder).where(
                KbFolder.kb_id == kb_id, KbFolder.path.like(f"{old_prefix}%")
            )
        )
    ).scalars().all()
    for child in descendants:
        child.path = new_prefix + child.path[len(old_prefix):]
        child.depth += delta

    folder.path = new_parent_path
    folder.parent_id = target_parent.id if target_parent else None
    folder.depth = new_depth


@router.delete("/{kb_id}/folders/{folder_id}", response_model=OkResponse, summary="删除文件夹")
async def delete_folder(
    kb_id: int,
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """仅允许删除空文件夹 —— 里面有东西就必须先清空，避免误删连带知识内容。"""
    await _assert_manage(db, user, kb_id)
    folder = await _get_folder(db, kb_id, folder_id)

    child_count = int(
        (
            await db.execute(
                select(func.count(KbFolder.id)).where(
                    KbFolder.kb_id == kb_id, KbFolder.parent_id == folder.id
                )
            )
        ).scalar()
        or 0
    )
    doc_count = int(
        (
            await db.execute(
                select(func.count(KbDocument.id)).where(
                    KbDocument.kb_id == kb_id, KbDocument.folder_id == folder.id
                )
            )
        ).scalar()
        or 0
    )
    if child_count or doc_count:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"文件夹非空（{child_count} 个子文件夹、{doc_count} 篇文档），请先清空后再删除",
        )

    acls = (
        await db.execute(select(ChunkAcl).where(ChunkAcl.folder_id == folder.id))
    ).scalars().all()
    for row in acls:
        await db.delete(row)

    name = folder.name
    await db.delete(folder)
    await audit.record(
        "folder.delete", db=db, request=request, user=user,
        resource_type="folder", resource_id=folder_id, resource_name=name,
        detail={"kb_id": kb_id, "removed_acls": len(acls)},
    )
    return OkResponse(message=f"文件夹「{name}」已删除")


# ---------------------------------------------------------------- 文件夹级批量授权


@router.get(
    "/{kb_id}/folders/{folder_id}/acls",
    response_model=List[FolderAclOut],
    summary="文件夹的授权规则",
)
async def list_folder_acls(
    kb_id: int,
    folder_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    await _get_folder(db, kb_id, folder_id)
    rows = (
        await db.execute(
            select(ChunkAcl)
            .where(ChunkAcl.kb_id == kb_id, ChunkAcl.folder_id == folder_id)
            .order_by(ChunkAcl.id.desc())
        )
    ).scalars().all()
    return [
        FolderAclOut(
            id=r.id,
            subject_type=r.subject_type,
            subject_id=r.subject_id,
            kb_id=r.kb_id,
            folder_id=r.folder_id,
            effect=r.effect,
            note=r.note,
            scope="folder",
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in rows
    ]


@router.post(
    "/{kb_id}/folders/{folder_id}/acls",
    response_model=OkResponse,
    summary="给文件夹批量授权",
)
async def grant_folder_acl(
    kb_id: int,
    folder_id: int,
    payload: FolderAclGrant,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """把一个文件夹（含其全部后代）放开/收口给若干主体。

    对同一主体同一文件夹，先删旧规则再插新规则，保证幂等（不会累积重复规则）。
    """
    await _assert_manage(db, user, kb_id)
    folder = await _get_folder(db, kb_id, folder_id)

    subject_ids = [str(s).strip() for s in payload.subject_ids if str(s).strip()]
    if not subject_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未选择授权对象")

    existing = (
        await db.execute(
            select(ChunkAcl).where(
                ChunkAcl.kb_id == kb_id,
                ChunkAcl.folder_id == folder_id,
                ChunkAcl.subject_type == payload.subject_type,
                ChunkAcl.subject_id.in_(subject_ids),
            )
        )
    ).scalars().all()
    for row in existing:
        await db.delete(row)
    await db.flush()

    for sid in subject_ids:
        db.add(
            ChunkAcl(
                subject_type=payload.subject_type,
                subject_id=sid,
                kb_id=kb_id,
                folder_id=folder_id,
                effect=payload.effect,
                note=payload.note,
                created_by=user.id,
            )
        )
    await db.flush()

    await audit.record(
        "folder.acl_grant", db=db, request=request, user=user,
        resource_type="folder", resource_id=folder_id, resource_name=folder.name,
        detail={
            "kb_id": kb_id,
            "subject_type": payload.subject_type.value,
            "subject_ids": subject_ids,
            "effect": payload.effect.value,
        },
    )
    verb = "放开" if payload.effect.value == "allow" else "收口"
    return OkResponse(message=f"已{verb}文件夹「{folder.name}」给 {len(subject_ids)} 个对象")


@router.delete(
    "/{kb_id}/folders/{folder_id}/acls/{acl_id}",
    response_model=OkResponse,
    summary="撤销文件夹授权规则",
)
async def revoke_folder_acl(
    kb_id: int,
    folder_id: int,
    acl_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _assert_manage(db, user, kb_id)
    row = (
        await db.execute(
            select(ChunkAcl).where(
                ChunkAcl.id == acl_id,
                ChunkAcl.kb_id == kb_id,
                ChunkAcl.folder_id == folder_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="规则不存在")

    detail = row.to_dict()
    await db.delete(row)
    await audit.record(
        "folder.acl_revoke", db=db, request=request, user=user,
        resource_type="folder", resource_id=folder_id, detail=detail,
    )
    return OkResponse(message="规则已撤销")


# ---------------------------------------------------------------- 文档归入文件夹


@router.post("/{kb_id}/documents/move", response_model=OkResponse, summary="批量移动文档到文件夹")
async def move_documents(
    kb_id: int,
    payload: DocumentMove,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await PermissionService(db).assert_kb_upload(user, kb_id)

    if payload.folder_id is not None:
        await _get_folder(db, kb_id, payload.folder_id)

    if not payload.document_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="未选择文档")

    docs = (
        await db.execute(
            select(KbDocument).where(
                KbDocument.kb_id == kb_id, KbDocument.id.in_(payload.document_ids)
            )
        )
    ).scalars().all()
    if not docs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到可移动的文档")

    for doc in docs:
        doc.folder_id = payload.folder_id
    await db.flush()

    await audit.record(
        "doc.move", db=db, request=request, user=user,
        resource_type="kb", resource_id=kb_id,
        detail={"folder_id": payload.folder_id, "document_ids": [d.id for d in docs]},
    )
    return OkResponse(message=f"已移动 {len(docs)} 篇文档")
