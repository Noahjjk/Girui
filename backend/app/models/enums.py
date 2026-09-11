"""枚举类型集中定义。"""
from __future__ import annotations

import enum


class UserRole(str, enum.Enum):
    ADMIN = "admin"                # 管理员：全部权限
    COLLABORATOR = "collaborator"  # 协作者：可上传知识（须被授权到具体知识库）
    USER = "user"                  # 普通用户：仅问答


ROLE_LABELS = {
    UserRole.ADMIN: "管理员",
    UserRole.COLLABORATOR: "协作者",
    UserRole.USER: "普通用户",
}


class AclSubjectType(str, enum.Enum):
    USER = "user"
    ROLE = "role"
    TAG = "tag"


class AclEffect(str, enum.Enum):
    ALLOW = "allow"
    DENY = "deny"


class Visibility(str, enum.Enum):
    PUBLIC = "public"          # 拥有知识库读取权的用户均可检索到该文档
    RESTRICTED = "restricted"  # 仅显式授权者（或标签匹配者）可检索


class DocumentStatus(str, enum.Enum):
    PENDING = "pending"
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


class LogStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"
    DENIED = "denied"


class MessageRole(str, enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ProviderKind(str, enum.Enum):
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    OPENAI = "openai"
    VLLM = "vllm"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


PROVIDER_LABELS = {
    ProviderKind.DEEPSEEK: "DeepSeek",
    ProviderKind.QWEN: "通义千问",
    ProviderKind.OPENAI: "OpenAI",
    ProviderKind.VLLM: "本地 vLLM",
    ProviderKind.OLLAMA: "本地 Ollama",
    ProviderKind.OPENAI_COMPATIBLE: "OpenAI 兼容端点",
}
