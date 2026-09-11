"""中文文本工具：清洗与 FTS 分词。

为什么需要单独一层分词：
  SQLite 内置的分词器（unicode61 / trigram）都不认识中文词边界。
  unicode61 会把一整句中文当成一个 token，trigram 则无法处理 2 字查询。
  这里统一用 jieba 的搜索引擎模式切词，索引与查询走同一套逻辑；
  jieba 不可用时退化为「单字 + 双字组合」，保证仍然可用。

索引与查询必须使用同一种分词模式，否则召回归零，因此模式会写进索引元数据，
不一致时给出明确告警（见 index_store）。
"""
from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 取决于部署环境
    import jieba

    jieba.setLogLevel(logging.WARNING)
    _TOKEN_MODE = "jieba"
except Exception:  # noqa: BLE001
    jieba = None  # type: ignore[assignment]
    _TOKEN_MODE = "bigram"

# 控制字符（保留 \n 与 \t）
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028\u2029\ufeff]")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_MULTI_SPACE_RE = re.compile(r"[ \t\u3000]{2,}")
_HAS_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# FTS5 的 MATCH 语法字符，查询词里出现会报语法错误
_FTS_SPECIAL_RE = re.compile(r'["\*\(\)\:\^\-]')
_WORD_RE = re.compile(r"[0-9A-Za-z_]+")


def token_mode() -> str:
    return _TOKEN_MODE


def clean_text(text: str) -> str:
    """归一化文本：统一换行、去掉控制字符与零宽字符、压缩空白。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    text = _MULTI_SPACE_RE.sub(" ", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()


def _bigrams(s: str) -> list[str]:
    return [s[i : i + 2] for i in range(len(s) - 1)] if len(s) > 1 else ([s] if s else [])


def fts_tokens(text: str) -> str:
    """把原文转成 FTS5 可索引的 token 串（空格分隔）。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []

    if _TOKEN_MODE == "jieba" and jieba is not None:
        for tok in jieba.cut_for_search(text):
            tok = tok.strip()
            if not tok or not _HAS_CJK_RE.search(tok) and not _WORD_RE.fullmatch(tok):
                # 标点、空白等直接丢弃
                continue
            if _HAS_CJK_RE.search(tok):
                out.append(tok)
                # 长词再补上双字组合，提升「报销制度」这类查询的召回
                if len(tok) > 2:
                    out.extend(_bigrams(tok))
            else:
                out.append(tok.lower())
    else:
        # 退化模式：中文取双字，英文数字取整词
        for chunk in re.split(r"(\s+)", text):
            if not chunk.strip():
                continue
            if _HAS_CJK_RE.search(chunk):
                out.extend(_bigrams(chunk))
            else:
                out.extend(w.lower() for w in _WORD_RE.findall(chunk))

    # 去重但保序
    seen: set[str] = set()
    uniq: list[str] = []
    for tok in out:
        if tok not in seen:
            seen.add(tok)
            uniq.append(tok)
    return " ".join(uniq)


def fts_query(question: str) -> str:
    """把用户问题转成 FTS5 MATCH 表达式。

    使用 OR 连接，让关键词命中的片段不会被完全漏掉；
    相关性由 bm25() 排序 + 向量分混合决定。
    """
    tokens = fts_tokens(question).split()
    if not tokens:
        return ""
    # 转义双引号并整体加引号，避免特殊字符造成 MATCH 语法错误
    safe = [t.replace('"', "") for t in tokens if t and not _FTS_SPECIAL_RE.fullmatch(t)]
    safe = [t for t in safe if t]
    if not safe:
        return ""
    # 中文词通常更关键，限制 token 数避免查询过长
    return " OR ".join(f'"{t}"' for t in safe[:32])


def approx_tokens(text: str) -> int:
    """粗略估算 token 数：中文按字，英文按 4 字符 1 token。"""
    if not text:
        return 0
    cjk = len(_HAS_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + max(0, other // 4)
