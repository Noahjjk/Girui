"""批量导入知识文件（公司规范、影刀社区帖子等）。

    # 导入单个目录下的所有文件到「公司规范」知识库
    python -m scripts.import_documents --kb 公司规范 --path ./docs/公司规范

    # 指定标签与来源链接（社区帖子）
    python -m scripts.import_documents --kb 影刀社区 --path ./docs/posts \\
        --tags 影刀,RPA --source-url-prefix https://www.yingdao.com/community/detail

    # 只预览不写入
    python -m scripts.import_documents --kb 公司规范 --path ./docs --dry-run

说明：文件会写入检索索引与业务库，随后自动触发解析切片。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import SessionLocal, init_models  # noqa: E402
from app.models.enums import DocumentStatus, Visibility  # noqa: E402
from app.models.knowledge import KbDocument, KnowledgeBase  # noqa: E402
from app.services import storage  # noqa: E402
from app.services.retriever import RetrievalError, retrieval_client  # noqa: E402

SUPPORTED = set(settings.allowed_upload_ext_set)


def collect_files(root: Path) -> List[Path]:
    if root.is_file():
        return [root]
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower().lstrip(".") in SUPPORTED
    )


async def import_one(
    kb: KnowledgeBase,
    path: Path,
    tags: List[str],
    source_url: Optional[str],
    visibility: Visibility,
    dry_run: bool,
) -> bool:
    size_mb = path.stat().st_size / 1024 / 1024
    if dry_run:
        print(f"  [预览] {path.name}  ({size_mb:.2f} MB)")
        return True

    try:
        stored = await storage.save_upload(
            path.name, path.read_bytes(), category=f"kb_{kb.id}"
        )
    except storage.UploadError as exc:
        print(f"  [跳过] {path.name}：{exc}")
        return False

    try:
        remote = await retrieval_client.upload_documents(
            kb.ragflow_dataset_id or "",
            [(stored.original_name, path.read_bytes(), stored.mime)],
        )
    except RetrievalError as exc:
        print(f"  [失败] {path.name}：{exc}")
        return False

    items = remote if isinstance(remote, list) else (
        remote.get("documents") if isinstance(remote, dict) else []
    ) or []
    if not items:
        print(f"  [失败] {path.name}：检索内核未返回文档标识")
        return False

    async with SessionLocal() as db:
        for item in items:
            doc_id = str(item.get("id") or "")
            if not doc_id:
                continue
            db.add(
                KbDocument(
                    kb_id=kb.id,
                    ragflow_document_id=doc_id,
                    name=item.get("name") or stored.original_name,
                    file_type=stored.ext,
                    size_bytes=stored.size,
                    source="import",
                    original_url=source_url,
                    storage_path=stored.relative_path,
                    checksum=stored.checksum,
                    visibility=visibility,
                    tags=tags,
                    status=DocumentStatus.PARSING,
                )
            )
        await db.commit()

    try:
        await retrieval_client.parse_documents(
            kb.ragflow_dataset_id or "", [str(i.get("id")) for i in items if i.get("id")]
        )
    except RetrievalError as exc:
        print(f"  [警告] {path.name} 解析触发失败：{exc}")

    print(f"  [成功] {path.name}  ({size_mb:.2f} MB)")
    return True


async def main(args) -> int:
    await init_models()

    async with SessionLocal() as db:
        kb = (
            await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == args.kb))
        ).scalar_one_or_none()
        if kb is None:
            names = (await db.execute(select(KnowledgeBase.name))).scalars().all()
            print(f"[错误] 知识库「{args.kb}」不存在。现有：{list(names)}")
            return 1
        if not kb.ragflow_dataset_id and not args.dry_run:
            print("[错误] 该知识库尚未关联检索数据集，请先在后台创建完成初始化")
            return 1
        await db.refresh(kb)

    root = Path(args.path).expanduser().resolve()
    if not root.exists():
        print(f"[错误] 路径不存在：{root}")
        return 1

    files = collect_files(root)
    if not files:
        print(f"[错误] 目录中没有受支持的文件（支持：{', '.join(sorted(SUPPORTED))}）")
        return 1

    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    visibility = Visibility(args.visibility)

    print(f"知识库：{args.kb}  文件数：{len(files)}  标签：{tags or '无'}")
    print("-" * 60)

    ok = 0
    for idx, path in enumerate(files, 1):
        print(f"[{idx}/{len(files)}]", end=" ")
        url = (
            f"{args.source_url_prefix.rstrip('/')}/{path.stem}"
            if args.source_url_prefix
            else None
        )
        if await import_one(kb, path, tags, url, visibility, args.dry_run):
            ok += 1

    print("-" * 60)
    print(f"完成：成功 {ok} / 共 {len(files)}")

    if not args.dry_run and ok:
        print("\n下一步：等待解析完成后，在后台执行「同步状态」以刷新片段数。")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量导入知识文件到极睿知识库")
    parser.add_argument("--kb", required=True, help="目标知识库名称")
    parser.add_argument("--path", required=True, help="文件或目录路径")
    parser.add_argument("--tags", default="", help="逗号分隔的标签")
    parser.add_argument("--source-url-prefix", default=None, help="原文链接前缀（社区帖子用）")
    parser.add_argument(
        "--visibility", default="public", choices=["public", "restricted"],
        help="public=库内可见者可读；restricted=需显式授权",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅预览不写入")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
