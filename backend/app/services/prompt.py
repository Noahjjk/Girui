"""提示词构造。

原则：把「只能依据给定片段作答」写死在系统提示词里，
避免模型用自己的先验知识编造公司规范。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

MAX_CONTEXT_CHARS = 24000

SYSTEM_PROMPT = """你是「极睿知识库」的企业知识助手，服务于公司内部员工。

回答规则（必须严格遵守）：
1. 只依据下方【参考片段】作答。片段中没有的信息，直接说明「知识库中未收录相关内容」，不要凭常识或推断补充。
2. 在引用了某条片段的句子末尾标注编号，格式为 [1]、[2]，可连续标注如 [1][3]。
3. 若多条片段相互矛盾，指出矛盾并说明各自来源，不要擅自取舍。
4. 回答使用简体中文，条理清晰。涉及操作步骤时用有序列表，涉及参数或代码时用代码块。
5. 若用户问题与知识库内容无关，礼貌说明并引导其提出知识库范围内的问题。
6. 不要输出"根据参考片段"这类元话术，直接给结论并标注编号即可。

公司规范类内容优先级高于社区帖子；若两者冲突，以公司规范为准并明确指出。"""

ATTACHMENT_NOTE = """
用户随问题提供了附件，附件内容已作为【参考片段】的补充一并给出。
若附件与知识库片段冲突，优先采用附件内容，并说明依据来自用户提供的附件。"""


def build_context_block(chunks: Sequence[Dict[str, Any]]) -> str:
    """把通过权限过滤的片段编号后拼成上下文。"""
    if not chunks:
        return "（本轮没有检索到任何可用片段）"

    lines: List[str] = []
    used = 0
    for item in chunks:
        content = (item.get("content") or "").strip()
        if not content:
            continue
        if used + len(content) > MAX_CONTEXT_CHARS:
            content = content[: max(0, MAX_CONTEXT_CHARS - used)]
        used += len(content)

        idx = item.get("index")
        name = item.get("document_name") or "未命名文档"
        kb = item.get("kb_name") or ""
        origin = f"来源：{name}" + (f"（{kb}）" if kb else "")
        lines.append(f"[{idx}] {origin}\n{content}")

        if used >= MAX_CONTEXT_CHARS:
            break

    return "\n\n---\n\n".join(lines)


def _text_part(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def _image_part(data_uri: str) -> Dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": data_uri}}


def build_messages(
    question: str,
    chunks: Sequence[Dict[str, Any]],
    history: Optional[Sequence[Dict[str, str]]] = None,
    attachments: Optional[Sequence[Dict[str, Any]]] = None,
    image_data_uris: Optional[Sequence[str]] = None,
    supports_vision: bool = False,
) -> List[Dict[str, Any]]:
    """构造发给大模型的 messages。

    image_data_uris: 已编码为 data:image/...;base64 的图片，仅当模型支持视觉时传入。
    """
    attachments = attachments or []
    text_attachments = [a for a in attachments if a.get("type") == "file" and a.get("text")]

    system_parts = [SYSTEM_PROMPT]
    if attachments:
        system_parts.append(ATTACHMENT_NOTE)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": "\n".join(system_parts)}]

    if history:
        messages.extend(history)

    context_block = build_context_block(chunks)

    text_parts: List[str] = []
    if text_attachments:
        blocks = []
        for a in text_attachments:
            blocks.append(f"[附件] {a.get('name')}\n{(a.get('text') or '')[:8000]}")
        text_parts.append("【用户附加内容】\n" + "\n\n".join(blocks))

    text_parts.append(f"【参考片段】\n{context_block}")
    text_parts.append(f"【用户问题】\n{question or '（用户仅上传了附件，请依据附件内容说明）'}")

    prompt_text = "\n\n".join(text_parts)

    if supports_vision and image_data_uris:
        content: List[Dict[str, Any]] = [_text_part(prompt_text)]
        content.extend(_image_part(uri) for uri in image_data_uris)
        messages.append({"role": "user", "content": content})
    else:
        if image_data_uris:
            prompt_text += (
                "\n\n（注意：本次提问包含图片，但当前所选模型不支持图像识别，"
                "请基于文字内容作答，并在必要时说明无法查看图片。）"
            )
        messages.append({"role": "user", "content": prompt_text})

    return messages


def should_use_history(question: str) -> bool:
    """极短且含指代词的追问才需要历史，避免无关历史污染检索。"""
    if len(question) > 30:
        return True
    markers = ["它", "这个", "那个", "上述", "刚才", "继续", "还有", "他", "该"]
    return any(m in question for m in markers)


def build_title(question: str, limit: int = 30) -> str:
    text = " ".join((question or "").split())
    if not text:
        return "新对话"
    return text[:limit] + ("…" if len(text) > limit else "")
