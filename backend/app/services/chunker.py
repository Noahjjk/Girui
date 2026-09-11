"""切片器：把段落切成适合嵌入模型的片段。

设计取舍：
  按「字符数」而不是「token 数」控制长度 —— 中文 1 字约等于 1 token，
  用字符数控制简单且无需引入分词器。默认 400 字，明显低于
  bge-small-zh-v1.5 的 512 token 上限，避免截断丢信息。

  分隔符从强到弱回退（段落 → 换行 → 句号 → 逗号 → 硬切），
  保证尽量不把一个句子劈开。相邻片段保留重叠区，避免答案正好落在切口上。

  切片按 Section 进行，因此 PDF 的页码能准确继承到每个片段。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from app.core.config import settings
from app.services.parser import Section
from app.services.textutil import approx_tokens

logger = logging.getLogger(__name__)

# 从强到弱的分隔边界。中文标点优先，最后空串表示硬切。
SEPARATORS: Sequence[str] = (
    "\n\n", "\n",
    "。", "！", "？", "；", "……", "’", "”",
    ". ", "! ", "? ", "; ",
    "，", "、", ", ",
    " ", "",
)


@dataclass
class Chunk:
    content: str
    page: Optional[int]
    ordinal: int
    tokens: int


def _recursive_split(text: str, size: int, seps: Sequence[str]) -> List[str]:
    """把长文本按分隔符逐级切到不超过 size 字符。"""
    text = text.strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    if not seps:
        return [text[i : i + size] for i in range(0, len(text), size)]

    sep, rest = seps[0], seps[1:]
    if not sep:
        # 空分隔符表示「硬切」。必须显式处理：Python 里 text.split("")
        # 会直接抛 ValueError（empty separator），而 "" in text 又恒为真，
        # 不拦住就会让「长段无标点文本」把解析整个搞崩。
        return [text[i : i + size] for i in range(0, len(text), size)]
    if sep not in text:
        return _recursive_split(text, size, rest)

    parts = text.split(sep)
    out: List[str] = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        # 把分隔符留在前一段末尾，保留标点
        seg = part + (sep if i < last else "")
        if not seg:
            continue
        if len(seg) <= size:
            out.append(seg)
        else:
            out.extend(_recursive_split(seg, size, rest))
    return out


def _merge(pieces: List[str], size: int, overlap: int, min_size: int) -> List[str]:
    """把小片贪心合并到大片，并在相邻片之间保留重叠。"""
    chunks: List[str] = []
    cur = ""
    for piece in pieces:
        if not piece:
            continue
        if not cur or len(cur) + len(piece) <= size:
            cur += piece
            continue

        chunks.append(cur)
        tail = cur[-overlap:] if overlap > 0 else ""
        if len(tail) + len(piece) > size:
            tail = ""  # 加上重叠会超长就放弃重叠，不硬塞
        cur = tail + piece

    if cur:
        chunks.append(cur)

    # 尾片过短则并入上一片，避免产生「半句话」的碎片
    if len(chunks) >= 2 and len(chunks[-1]) < min_size:
        chunks[-2] = chunks[-2] + chunks[-1]
        chunks.pop()

    return [c.strip() for c in chunks if c.strip()]


def split_text(
    text: str,
    size: Optional[int] = None,
    overlap: Optional[int] = None,
    min_size: Optional[int] = None,
) -> List[str]:
    size = size or settings.CHUNK_SIZE
    overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP
    min_size = min_size or settings.CHUNK_MIN_SIZE
    pieces = _recursive_split(text, size, SEPARATORS)
    return _merge(pieces, size, overlap, min_size)


def chunk_sections(
    sections: Sequence[Section],
    size: Optional[int] = None,
    overlap: Optional[int] = None,
    min_size: Optional[int] = None,
) -> List[Chunk]:
    """把解析出来的段落切成带页码的片段。

    片段 id 由调用方按 `{document_id}-{ordinal:04d}` 生成（见 index_store），
    因此 ordinal 必须稳定 —— 同一份文件两次解析要产出同样的序号。
    """
    size = size or settings.CHUNK_SIZE
    overlap = overlap if overlap is not None else settings.CHUNK_OVERLAP
    min_size = min_size or settings.CHUNK_MIN_SIZE

    out: List[Chunk] = []
    ordinal = 0
    for sec in sections:
        if not sec.text.strip():
            continue
        for text in split_text(sec.text, size, overlap, min_size):
            out.append(
                Chunk(
                    content=text,
                    page=sec.page,
                    ordinal=ordinal,
                    tokens=approx_tokens(text),
                )
            )
            ordinal += 1
    return out
