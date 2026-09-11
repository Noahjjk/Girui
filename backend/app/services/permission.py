"""知识隔离权限中枢。

需求原文：「权限粒度控制到用户可检索到的知识片段」「不同用户检索可见的知识范围相互独立」

实现为「检索前收敛 + 检索后复核」两段式：

  检索前 —— 计算用户可见的 dataset_id / document_id 集合，传给检索内核，
             让不可见的文档根本不参与召回。
  检索后 —— 对内核返回的每条 chunk 逐条判定，
             不可见的 chunk 在进入提示词之前就被丢弃。

判定优先级（deny 绝对优先）：

  chunk 级 deny        → 拒绝
  chunk 级 allow       → 通过
  该文档存在 chunk 白名单 → 不在白名单内则拒绝（白名单模式）
  文档级 deny          → 拒绝
  文档级 allow         → 通过
  文件夹级 deny        → 拒绝（沿祖先链，链上任一 deny 即拒绝）
  文件夹级 allow       → 通过（沿祖先链，链上任一 allow 即放行）
  文档可见性           → public 通过 / restricted 拒绝

文件夹是**批量授权的载体**：给父文件夹配一条 allow，其下所有后代文件夹里的文档一并放行。
但文档自己单独配的规则优先级更高（更具体者优先），所以「文件夹整体授权 + 个别文档单独收口」
可以并存。文件夹不持有向量数据，一个知识库仍然只有一个索引文件。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AclEffect, AclSubjectType, UserRole, Visibility
from app.models.knowledge import ChunkAcl, KbDocument, KbFolder, KbPermission, KnowledgeBase
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 数据结构


@dataclass
class KbCapability:
    kb_id: int
    can_read: bool = False
    can_upload: bool = False
    can_manage: bool = False


@dataclass
class FolderInfo:
    """文件夹的最小元信息快照，用于判定继承链。"""

    id: int
    kb_id: int
    parent_id: Optional[int]
    name: str
    path: str  # 从根到**父节点**的 id 链，形如 "/3/7/"
    depth: int
    visibility: str  # public / restricted

    @property
    def self_path(self) -> str:
        return f"{self.path}{self.id}/"

    @property
    def name_path(self) -> str:
        return self.path


@dataclass
class PermissionScope:
    """一次问答请求的完整可见范围快照。"""

    user_id: int
    role: UserRole
    is_admin: bool
    tag_names: List[str] = field(default_factory=list)

    kb_ids: List[int] = field(default_factory=list)
    dataset_ids: List[str] = field(default_factory=list)
    kb_name_by_id: Dict[int, str] = field(default_factory=dict)
    kb_id_by_dataset: Dict[str, int] = field(default_factory=dict)

    # ragflow_document_id -> 业务文档信息
    doc_kb: Dict[str, int] = field(default_factory=dict)
    doc_name: Dict[str, str] = field(default_factory=dict)
    doc_meta: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    visible_docs: Set[str] = field(default_factory=set)
    doc_visible_cache: Dict[str, bool] = field(default_factory=dict)

    allowed_chunks: Set[str] = field(default_factory=set)
    denied_chunks: Set[str] = field(default_factory=set)
    docs_with_chunk_whitelist: Set[str] = field(default_factory=set)
    # 有「片段级 allow」命中的文档。
    #
    # 这些文档本身可能是 restricted（不在 visible_docs 里），但既然管理员显式放行了
    # 其中某几个片段，检索前置过滤就必须把这些文档也带上，否则那几条被放行的片段
    # 连召回的机会都没有，allow 规则形同虚设。
    # 安全性由 is_chunk_visible() 收口：白名单模式下非放行片段一律丢弃。
    chunk_allowed_docs: Set[str] = field(default_factory=set)

    # ---- 文件夹（多级嵌套）----
    folders: Dict[int, FolderInfo] = field(default_factory=dict)
    # document_id -> folder_id（None = 挂在知识库根下）
    doc_folder: Dict[str, Optional[int]] = field(default_factory=dict)
    # 该用户可见的文件夹（已折算过继承与 deny）
    visible_folders: Set[int] = field(default_factory=set)
    # 因「文件夹级 allow」而被批量放行的文件夹
    granted_folders: Set[int] = field(default_factory=set)

    @property
    def search_docs(self) -> Set[str]:
        """检索前置过滤应携带的文档范围 = 可见文档 ∪ 有片段级 allow 的文档。"""
        return self.visible_docs | self.chunk_allowed_docs

    @property
    def is_empty(self) -> bool:
        return not self.dataset_ids or not self.search_docs

    # -------------------------------------------------- 文件夹

    def folder_chain(self, folder_id: Optional[int]) -> List[int]:
        """从某文件夹一路向上到根，返回 [自身, 父, 祖父, ...]。"""
        chain: List[int] = []
        seen: Set[int] = set()
        cur = folder_id
        while cur is not None and cur not in seen:
            seen.add(cur)
            chain.append(cur)
            info = self.folders.get(cur)
            cur = info.parent_id if info else None
        return chain

    def is_folder_visible(self, folder_id: Optional[int]) -> bool:
        """根目录（None）恒可见 —— 它代表「未归入任何文件夹」的老文档。"""
        if folder_id is None:
            return True
        return folder_id in self.visible_folders

    def folder_label(self, folder_id: Optional[int]) -> str:
        return self.folders[folder_id].name if folder_id in self.folders else "根目录"

    # -------------------------------------------------- chunk 级判定

    def is_doc_visible(self, document_id: str) -> bool:
        if document_id in self.doc_visible_cache:
            return self.doc_visible_cache[document_id]
        result = document_id in self.visible_docs
        self.doc_visible_cache[document_id] = result
        return result

    def is_chunk_visible(self, chunk_id: Optional[str], document_id: str) -> bool:
        if chunk_id and chunk_id in self.denied_chunks:
            return False
        if chunk_id and chunk_id in self.allowed_chunks:
            return True
        if document_id in self.docs_with_chunk_whitelist:
            # 白名单模式：该文档只放行显式授权的片段
            return False
        return self.is_doc_visible(document_id)

    def filter_chunks(self, chunks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """丢弃不可见片段。同时修正被前一条规则误伤的情况。"""
        out: List[Dict[str, Any]] = []
        dropped = 0
        for ch in chunks:
            cid = ch.get("id") or ch.get("chunk_id")
            did = ch.get("document_id") or ch.get("documentId") or ""
            if self.is_chunk_visible(cid, did):
                out.append(ch)
            else:
                dropped += 1
        if dropped:
            logger.debug("权限过滤：丢弃 %d 条不可见片段", dropped)
        return out

    def stats(self) -> Dict[str, Any]:
        return {
            "kb_count": len(self.kb_ids),
            "dataset_count": len(self.dataset_ids),
            "visible_documents": len(self.visible_docs),
            "searchable_documents": len(self.search_docs),
            "total_documents": len(self.doc_kb),
            "chunk_allow_rules": len(self.allowed_chunks),
            "chunk_deny_rules": len(self.denied_chunks),
            "documents_in_whitelist_mode": len(self.docs_with_chunk_whitelist),
            "total_folders": len(self.folders),
            "visible_folders": len(self.visible_folders),
            "folder_granted": len(self.granted_folders),
        }


# ---------------------------------------------------------------- 服务


class PermissionService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------ 知识库可达性

    async def kb_capabilities(self, user: User, kb_id: int) -> KbCapability:
        if user.role == UserRole.ADMIN:
            return KbCapability(kb_id, True, True, True)
        row = (
            await self.db.execute(
                select(KbPermission).where(
                    KbPermission.user_id == user.id, KbPermission.kb_id == kb_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return KbCapability(kb_id)
        return KbCapability(kb_id, row.can_read, row.can_upload, row.can_manage)

    async def readable_kb_ids(self, user: User) -> List[int]:
        stmt = select(KnowledgeBase.id).where(KnowledgeBase.is_active.is_(True))
        if user.role == UserRole.ADMIN:
            return list((await self.db.execute(stmt)).scalars().all())

        stmt = (
            select(KbPermission.kb_id)
            .join(KnowledgeBase, KnowledgeBase.id == KbPermission.kb_id)
            .where(
                KbPermission.user_id == user.id,
                KbPermission.can_read.is_(True),
                KnowledgeBase.is_active.is_(True),
            )
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def uploadable_kb_ids(self, user: User) -> List[int]:
        if user.role == UserRole.ADMIN:
            return list(
                (
                    await self.db.execute(
                        select(KnowledgeBase.id).where(KnowledgeBase.is_active.is_(True))
                    )
                ).scalars().all()
            )
        if user.role != UserRole.COLLABORATOR:
            return []
        stmt = (
            select(KbPermission.kb_id)
            .join(KnowledgeBase, KnowledgeBase.id == KbPermission.kb_id)
            .where(
                KbPermission.user_id == user.id,
                KbPermission.can_upload.is_(True),
                KnowledgeBase.is_active.is_(True),
            )
        )
        return list((await self.db.execute(stmt)).scalars().all())

    # ------------------------------------------------ 完整范围解析

    async def resolve_scope(
        self, user: User, requested_kb_ids: Optional[Sequence[int]] = None
    ) -> PermissionScope:
        """计算该用户本次请求的可见范围。

        requested_kb_ids 为用户在前端勾选的知识库，会与用户实际可读库求交集，
        因此前端伪造 kb_ids 无法越权。
        """
        is_admin = user.role == UserRole.ADMIN
        scope = PermissionScope(
            user_id=user.id,
            role=user.role,
            is_admin=is_admin,
            tag_names=user.tag_names,
        )

        readable = set(await self.readable_kb_ids(user))
        if requested_kb_ids:
            readable &= {int(k) for k in requested_kb_ids}
        if not readable:
            return scope

        kbs = (
            await self.db.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.id.in_(readable), KnowledgeBase.is_active.is_(True)
                )
            )
        ).scalars().all()

        scope.kb_ids = [kb.id for kb in kbs]
        scope.kb_name_by_id = {kb.id: kb.name for kb in kbs}
        scope.kb_id_by_dataset = {
            kb.ragflow_dataset_id: kb.id for kb in kbs if kb.ragflow_dataset_id
        }
        scope.dataset_ids = [d for d in scope.kb_id_by_dataset.keys()]

        await self._load_folders(scope)

        documents = (
            await self.db.execute(select(KbDocument).where(KbDocument.kb_id.in_(scope.kb_ids)))
        ).scalars().all()

        for doc in documents:
            scope.doc_kb[doc.ragflow_document_id] = doc.kb_id
            scope.doc_name[doc.ragflow_document_id] = doc.name
            scope.doc_folder[doc.ragflow_document_id] = doc.folder_id
            folder = scope.folders.get(doc.folder_id) if doc.folder_id else None
            scope.doc_meta[doc.ragflow_document_id] = {
                "id": doc.id,
                "name": doc.name,
                "kb_id": doc.kb_id,
                "kb_name": scope.kb_name_by_id.get(doc.kb_id),
                "category": doc.category,
                "original_url": doc.original_url,
                "tags": list(doc.tags or []),
                "visibility": doc.visibility.value
                if hasattr(doc.visibility, "value")
                else str(doc.visibility),
                "author": (doc.remark or None),
                "folder_id": doc.folder_id,
                "folder_name": folder.name if folder else None,
            }

        # ---- 管理员：默认全量可见，但仍尊重显式 deny（便于临时封禁某片段 / 某文件夹） ----
        if is_admin:
            await self._apply_acl_rules(scope, user, documents, admin_override=True)
            return scope

        # ---- 普通用户 / 协作者 ----
        await self._apply_acl_rules(scope, user, documents, admin_override=False)
        return scope

    async def _apply_acl_rules(
        self,
        scope: PermissionScope,
        user: User,
        documents: Sequence[KbDocument],
        admin_override: bool,
    ) -> None:
        subjects: List[Tuple[AclSubjectType, str]] = [(AclSubjectType.USER, str(user.id))]
        role_value = user.role.value if hasattr(user.role, "value") else str(user.role)
        subjects.append((AclSubjectType.ROLE, role_value))
        subjects.extend((AclSubjectType.TAG, name) for name in user.tag_names)

        conditions = [
            and_(ChunkAcl.subject_type == stype, ChunkAcl.subject_id == sid)
            for stype, sid in subjects
        ]
        acl_rows = (
            await self.db.execute(
                select(ChunkAcl).where(
                    ChunkAcl.kb_id.in_(scope.kb_ids), or_(*conditions)
                )
            )
        ).scalars().all()

        allow_kb: Set[int] = set()
        deny_kb: Set[int] = set()
        allow_doc: Set[str] = set()
        deny_doc: Set[str] = set()
        doc_has_whitelist: Set[str] = set()
        allow_folder: Set[int] = set()
        deny_folder: Set[int] = set()

        for row in acl_rows:
            effect = row.effect.value if hasattr(row.effect, "value") else str(row.effect)
            is_allow = effect == AclEffect.ALLOW.value
            if row.chunk_id:
                # 片段级规则：allow 既放行该片段，也把整篇文档标记为「白名单模式」，
                # 使该文档的其余片段默认不可见。
                if is_allow:
                    scope.allowed_chunks.add(row.chunk_id)
                    if row.ragflow_document_id:
                        doc_has_whitelist.add(row.ragflow_document_id)
                        scope.chunk_allowed_docs.add(row.ragflow_document_id)
                else:
                    scope.denied_chunks.add(row.chunk_id)
                continue
            if row.ragflow_document_id:
                # 文档级规则：整篇文档放行/拒绝。
                # 注意这里 **不能** 把它塞进 doc_has_whitelist —— 白名单模式只由片段级
                # allow 触发；否则「给用户授权整篇文档」反而会让他一个片段都看不到。
                (allow_doc if is_allow else deny_doc).add(row.ragflow_document_id)
                continue
            if row.folder_id:
                # 文件夹级规则：作用于该文件夹及其所有后代文件夹内的文档（批量授权）。
                (allow_folder if is_allow else deny_folder).add(row.folder_id)
                continue
            (allow_kb if is_allow else deny_kb).add(row.kb_id)

        scope.docs_with_chunk_whitelist = doc_has_whitelist

        # ---- 文件夹可见性：先折算继承链，文档判定才有闸门可用 ----
        self._resolve_folder_visibility(scope, allow_folder, deny_folder, admin_override)

        # ---- 文档级可见性：从「最具体」到「最宽泛」依次判定 ----
        for doc in documents:
            did = doc.ragflow_document_id
            fid = doc.folder_id

            # 1) 文档级 deny —— 最具体，优先级最高
            if did in deny_doc or doc.kb_id in deny_kb:
                continue
            # 2) 文档级 allow —— 与 deny 同级，能盖过文件夹级规则。
            #    这是刻意设计：文件夹负责批量授权，个别文档仍可单独开口子，
            #    否则「整个文件夹收起来、只放行其中一篇」就做不到了。
            if did in allow_doc or doc.kb_id in allow_kb:
                scope.visible_docs.add(did)
                continue
            # 3) 文件夹级 deny —— 沿祖先链命中即拒绝
            if fid is not None and self._chain_hits(scope, fid, deny_folder):
                continue
            # 4) 管理员：未命中任何显式 deny 即全量可见（不受文件夹 restricted 约束）
            if admin_override:
                scope.visible_docs.add(did)
                continue
            # 5) 文件夹闸门
            if fid is not None:
                if fid not in scope.visible_folders:
                    continue  # 文件夹整体不可见 → 里面文档一律不可见
                if fid in scope.granted_folders:
                    scope.visible_docs.add(did)  # 文件夹级 allow = 批量放行
                    continue
            # 6) 回落到文档自身可见性

            visibility = (
                doc.visibility.value if hasattr(doc.visibility, "value") else str(doc.visibility)
            )
            if visibility == Visibility.PUBLIC.value:
                # public 文档：库内可见即可读。
                # 若该文档开启了片段白名单模式，可见性仍为真，但最终只有白名单片段能进入提示词
                # —— 由 is_chunk_visible() 的 whitelist 分支收口。
                scope.visible_docs.add(did)
            # RESTRICTED 且无任何 allow → 不可见

        logger.debug("权限范围解析完成 %s", scope.stats())

    # ------------------------------------------------ 文件夹（多级嵌套）

    async def _load_folders(self, scope: PermissionScope) -> None:
        """把本次涉及的库下所有文件夹读进快照，供继承链判定。"""
        rows = (
            await self.db.execute(
                select(KbFolder)
                .where(KbFolder.kb_id.in_(scope.kb_ids))
                .order_by(KbFolder.depth, KbFolder.sort_order, KbFolder.id)
            )
        ).scalars().all()
        for f in rows:
            scope.folders[f.id] = FolderInfo(
                id=f.id,
                kb_id=f.kb_id,
                parent_id=f.parent_id,
                name=f.name,
                path=f.path or "",
                depth=f.depth,
                visibility=f.visibility.value
                if hasattr(f.visibility, "value")
                else str(f.visibility),
            )

    @staticmethod
    def _chain_hits(scope: PermissionScope, folder_id: int, rules: Set[int]) -> bool:
        """该文件夹的祖先链（含自身）是否命中给定规则集合。"""
        return any(fid in rules for fid in scope.folder_chain(folder_id))

    def _resolve_folder_visibility(
        self,
        scope: PermissionScope,
        allow_folder: Set[int],
        deny_folder: Set[int],
        admin_override: bool,
    ) -> None:
        """把「本文件夹的规则 + 祖先链」折算成一个可见集合。

        - deny 绝对优先：链上任一环节 deny → 整体不可见
        - 其次链上任一 allow → 可见，并记入 granted_folders（用于批量放行其中文档）
        - 都没有规则时回落到 visibility；且**父级 restricted 会传染子级**
        """
        for fid in scope.folders:
            chain = scope.folder_chain(fid)
            if any(c in deny_folder for c in chain):
                continue
            if any(c in allow_folder for c in chain):
                scope.visible_folders.add(fid)
                scope.granted_folders.add(fid)
                continue
            if admin_override:
                scope.visible_folders.add(fid)
                continue
            if any(
                scope.folders[c].visibility == Visibility.RESTRICTED.value
                for c in chain
                if c in scope.folders
            ):
                continue  # 自身或任一祖先 restricted
            scope.visible_folders.add(fid)

    # ------------------------------------------------ 单库范围（管理后台用）

    async def resolve_kb_scope(self, user: User, kb_id: int) -> PermissionScope:
        return await self.resolve_scope(user, [kb_id])

    async def assert_kb_read(self, user: User, kb_id: int) -> None:
        cap = await self.kb_capabilities(user, kb_id)
        if not cap.can_read:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权访问该知识库"
            )

    async def assert_kb_upload(self, user: User, kb_id: int) -> None:
        cap = await self.kb_capabilities(user, kb_id)
        if not cap.can_upload:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权向该知识库上传知识"
            )

    async def assert_kb_manage(self, user: User, kb_id: int) -> None:
        cap = await self.kb_capabilities(user, kb_id)
        if not cap.can_manage:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="无权管理该知识库"
            )

    # ------------------------------------------------ 诊断

    async def diagnose(self, target: User, kb_id: int) -> Dict[str, Any]:
        """管理员排查「某人为什么搜不到/能看到某片段」。"""
        scope = await self.resolve_scope(target, [kb_id])
        all_docs = [d for d, k in scope.doc_kb.items() if k == kb_id]
        return {
            "visible_documents": sorted(scope.visible_docs & set(all_docs)),
            "hidden_documents": sorted(set(all_docs) - scope.visible_docs),
            "allowed_chunk_rules": sorted(scope.allowed_chunks),
            "denied_chunk_rules": sorted(scope.denied_chunks),
            "whitelist_mode_documents": sorted(scope.docs_with_chunk_whitelist),
            "visible_folders": sorted(scope.visible_folders),
            "hidden_folders": sorted(set(scope.folders) - scope.visible_folders),
            "granted_folders": sorted(scope.granted_folders),
            "tag_names": scope.tag_names,
            "role": scope.role.value if hasattr(scope.role, "value") else str(scope.role),
            "stats": scope.stats(),
        }


def get_permission_service(db: AsyncSession) -> PermissionService:
    return PermissionService(db)
