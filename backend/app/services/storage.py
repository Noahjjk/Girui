"""上传文件落盘。

安全要求：后缀白名单 + 强制重命名 + 路径穿越防护 + 大小限制。
文件名一律重命名为 uuid，原始名只存入数据库，避免任何路径注入面。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import aiofiles

from app.core.config import settings

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^\w\-. ]", re.UNICODE)


class UploadError(ValueError):
    pass


@dataclass
class StoredFile:
    original_name: str
    stored_name: str
    relative_path: str
    absolute_path: str
    size: int
    mime: str
    checksum: str
    ext: str


def safe_display_name(filename: str) -> str:
    """仅用于入库展示，去掉路径分隔与控制字符。"""
    base = os.path.basename(filename or "").strip() or "unnamed"
    base = base.replace("\x00", "")
    base = _UNSAFE.sub("_", base)
    return base[:200]


def get_ext(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower().lstrip(".")


def validate_extension(filename: str, *, image: bool = False) -> str:
    ext = get_ext(filename)
    if not ext:
        raise UploadError(f"文件「{filename}」缺少扩展名，无法识别类型")
    allowed = settings.allowed_image_ext_set if image else settings.allowed_upload_ext_set
    if ext not in allowed:
        raise UploadError(
            f"不支持的文件类型 .{ext}，允许：{', '.join(sorted(allowed))}"
        )
    return ext


def validate_size(size: int) -> None:
    limit = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if size <= 0:
        raise UploadError("文件内容为空")
    if size > limit:
        raise UploadError(
            f"文件大小 {size / 1024 / 1024:.1f}MB 超出上限 {settings.MAX_UPLOAD_SIZE_MB}MB"
        )


async def save_upload(
    filename: str,
    content: bytes,
    *,
    mime: str = "",
    category: str = "documents",
    image: bool = False,
) -> StoredFile:
    original = safe_display_name(filename)
    ext = validate_extension(original, image=image)
    validate_size(len(content))

    day = datetime.now().strftime("%Y%m%d")
    subdir = os.path.join(category, day)
    target_dir = os.path.join(settings.upload_dir, subdir)
    os.makedirs(target_dir, exist_ok=True)

    stored_name = f"{uuid.uuid4().hex}.{ext}"
    absolute_path = os.path.join(target_dir, stored_name)

    # 二次确认最终路径未逃逸出上传根目录
    upload_root = os.path.realpath(settings.upload_dir)
    if not os.path.realpath(absolute_path).startswith(upload_root):
        raise UploadError("文件路径非法")

    async with aiofiles.open(absolute_path, "wb") as fh:
        await fh.write(content)

    checksum = hashlib.sha256(content).hexdigest()
    relative = os.path.relpath(absolute_path, settings.upload_dir).replace("\\", "/")

    logger.info("文件已保存 %s (%d bytes)", relative, len(content))
    return StoredFile(
        original_name=original,
        stored_name=stored_name,
        relative_path=relative,
        absolute_path=absolute_path,
        size=len(content),
        mime=mime or _guess_mime(ext),
        checksum=checksum,
        ext=ext,
    )


def resolve_upload_path(relative_path: str) -> Optional[str]:
    if not relative_path:
        return None
    root = os.path.realpath(settings.upload_dir)
    absolute = os.path.realpath(os.path.join(root, relative_path))
    if not absolute.startswith(root) or not os.path.isfile(absolute):
        return None
    return absolute


def to_data_uri(relative_path: str, mime: Optional[str] = None) -> Optional[str]:
    """把上传的图片转成 data URI，供支持视觉的模型直接消费。"""
    import base64

    absolute = resolve_upload_path(relative_path)
    if not absolute:
        return None
    try:
        with open(absolute, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if len(data) > 8 * 1024 * 1024:
        return None
    guessed = mime or _guess_mime(get_ext(absolute))
    return f"data:{guessed};base64,{base64.b64encode(data).decode()}"


_MIME_MAP = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "html": "text/html",
    "htm": "text/html",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "epub": "application/epub+zip",
}


def _guess_mime(ext: str) -> str:
    return _MIME_MAP.get((ext or "").lower().lstrip("."), "application/octet-stream")


def extract_text(absolute_path: str, ext: str, limit: int = 20000) -> str:
    """从纯文本类附件抽取内容，供提示词使用。

    仅处理能直接读的文本格式；附件里的 pdf/docx 不在这里解析，
    此处的抽取是「随问随附」的轻量补充。
    """
    text_exts = {"txt", "md", "markdown", "csv", "json", "html", "htm"}
    if (ext or "").lower() not in text_exts:
        return ""
    try:
        with open(absolute_path, "rb") as fh:
            raw = fh.read(limit * 4)
    except OSError:
        return ""
    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        return ""
    if ext in {"html", "htm"}:
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
    return text[:limit]
