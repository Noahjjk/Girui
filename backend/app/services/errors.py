"""检索后端的异常层次。

单独成模块是为了避免循环导入：RAGFlow 客户端与本地内核都要用这套异常，
而 retriever.py 在「后端=RAGFlow」时又要导入 RAGFlow 客户端。

层次：
    RetrievalError
      ├── RetrievalNotConfigured   后端缺少必要配置（例如 RAGFlow API Key）
      └── RetrievalBackendError    后端返回业务错误或网络异常
调用方一律捕获 RetrievalError，不感知底层是哪个后端。
"""
from __future__ import annotations

from typing import Optional


class RetrievalError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.status = status


class RetrievalNotConfigured(RetrievalError):
    """后端未就绪或缺少配置。"""


class RetrievalBackendError(RetrievalError):
    """后端可用但本次调用失败。"""


__all__ = [
    "RetrievalError",
    "RetrievalNotConfigured",
    "RetrievalBackendError",
]
