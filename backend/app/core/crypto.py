"""对称加密：用于在数据库中加密存储模型供应商的 API Key。

密钥来自环境变量 SECRET_ENCRYPTION_KEY，既接受标准 Fernet 密钥，
也接受任意字符串（自动经 SHA-256 派生）。
"""
from __future__ import annotations

import base64
import hashlib
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_PREFIX = "enc:v1:"


def _derive_key(raw: str) -> bytes:
    if not raw:
        # 未配置时退化为固定开发密钥，并在启动日志中告警（见 main.py）
        raw = "jirui-insecure-development-key"
    try:
        if len(raw) == 44 and raw.endswith("="):
            base64.urlsafe_b64decode(raw.encode())
            return raw.encode()
    except Exception:  # noqa: BLE001
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


_fernet = Fernet(_derive_key(settings.SECRET_ENCRYPTION_KEY))


def encrypt(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _PREFIX + _fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    if not ciphertext.startswith(_PREFIX):
        # 兼容早期明文入库的数据
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""


def mask(secret: str, keep: int = 4) -> str:
    """展示用脱敏。"""
    if not secret:
        return ""
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}{'*' * 8}{secret[-keep:]}"


def is_dev_key() -> bool:
    return not settings.SECRET_ENCRYPTION_KEY
