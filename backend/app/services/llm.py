"""多供应商大模型路由。

所有供应商统一按 OpenAI 兼容协议调用，因此 DeepSeek / 通义千问 / vLLM / Ollama /
任意自建端点只是 base_url + model_name 的差异。前端「模型选择」下拉的每一项
对应 model_providers 表的一条记录。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt
from app.models.enums import ProviderKind
from app.models.llm import ModelProvider

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, provider: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.provider = provider


@dataclass
class ChatResult:
    content: str
    usage: Dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""


class LLMClient:
    """单个模型供应商的调用封装。"""

    def __init__(self, provider: ModelProvider, api_key: str):
        self.provider = provider
        self.api_key = api_key or ""
        self.base_url = provider.base_url.rstrip("/")

    # -------------------------------------------------- 内部

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _payload(
        self,
        messages: List[Dict[str, Any]],
        stream: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
        top_p: Optional[float],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.provider.model_name,
            "messages": messages,
            "stream": stream,
            "temperature": temperature if temperature is not None else self.provider.temperature,
            "max_tokens": max_tokens or self.provider.max_tokens,
            "top_p": top_p if top_p is not None else self.provider.top_p,
        }
        extra = self.provider.extra or {}
        if isinstance(extra, dict):
            payload.update({k: v for k, v in extra.items() if k not in payload})
        if stream and self.provider.provider != ProviderKind.OLLAMA:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _url(self) -> str:
        return f"{self.base_url}/chat/completions"

    # -------------------------------------------------- 非流式

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
    ) -> ChatResult:
        started = time.perf_counter()
        payload = self._payload(messages, False, temperature, max_tokens, top_p)
        try:
            async with httpx.AsyncClient(timeout=self.provider.timeout_seconds) as client:
                resp = await client.post(self._url(), headers=self._headers(), json=payload)
        except httpx.HTTPError as exc:
            raise LLMError(f"无法连接模型服务 {self.base_url}：{exc}") from exc

        if resp.status_code >= 400:
            raise LLMError(self._extract_error(resp), status=resp.status_code)

        data = resp.json()
        choices = data.get("choices") or []
        content = ""
        if choices:
            content = (choices[0].get("message") or {}).get("content") or ""
        return ChatResult(
            content=content,
            usage=data.get("usage") or {},
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=data.get("model") or self.provider.model_name,
        )

    # -------------------------------------------------- 流式

    async def stream(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """逐块产出 {"type": "delta"|"usage"|"done", ...}"""
        payload = self._payload(messages, True, temperature, max_tokens, top_p)
        timeout = httpx.Timeout(self.provider.timeout_seconds, connect=15.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST", self._url(), headers=self._headers(), json=payload
                ) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "ignore")
                        # 少数端点不认 stream_options，去掉后重试一次
                        if resp.status_code == 400 and "stream_options" in body:
                            payload.pop("stream_options", None)
                        else:
                            raise LLMError(
                                self._extract_error_body(body, resp.status_code),
                                status=resp.status_code,
                            )
                    else:
                        async for chunk in self._iter_sse(resp):
                            yield chunk
                        return

                # 重试路径
                async with client.stream(
                    "POST", self._url(), headers=self._headers(), json=payload
                ) as resp2:
                    if resp2.status_code >= 400:
                        body = (await resp2.aread()).decode("utf-8", "ignore")
                        raise LLMError(
                            self._extract_error_body(body, resp2.status_code),
                            status=resp2.status_code,
                        )
                    async for chunk in self._iter_sse(resp2):
                        yield chunk
        except httpx.HTTPError as exc:
            raise LLMError(f"模型服务连接中断：{exc}") from exc

    async def _iter_sse(self, resp: httpx.Response) -> AsyncIterator[Dict[str, Any]]:
        buffer = ""
        async for raw in resp.aiter_text():
            buffer += raw
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    yield {"type": "done"}
                    return
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue

                if isinstance(obj, dict) and obj.get("usage"):
                    yield {"type": "usage", "usage": obj["usage"]}

                for choice in obj.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield {"type": "delta", "text": text}
                    if choice.get("finish_reason"):
                        yield {"type": "finish", "reason": choice["finish_reason"]}
        yield {"type": "done"}

    # -------------------------------------------------- 连通性自检

    async def test(self, prompt: str = "请回复两个字：正常") -> ChatResult:
        return await self.complete(
            [{"role": "user", "content": prompt}], temperature=0.0, max_tokens=32
        )

    # -------------------------------------------------- 错误处理

    @staticmethod
    def _extract_error(resp: httpx.Response) -> str:
        return LLMClient._extract_error_body(resp.text, resp.status_code)

    @staticmethod
    def _extract_error_body(body: str, status: int) -> str:
        try:
            obj = json.loads(body)
            err = obj.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            if isinstance(err, str):
                return err
            if obj.get("message"):
                return str(obj["message"])
        except (json.JSONDecodeError, AttributeError):
            pass
        return f"模型服务返回 HTTP {status}: {body[:300]}"


# ---------------------------------------------------------------- 工厂


def build_client(provider: ModelProvider) -> LLMClient:
    return LLMClient(provider, decrypt(provider.api_key_enc or ""))


async def get_provider(db: AsyncSession, provider_id: Optional[int]) -> ModelProvider:
    """按 ID 取供应商；未指定则取默认，再退化为第一个启用的。"""
    if provider_id:
        provider = (
            await db.execute(select(ModelProvider).where(ModelProvider.id == provider_id))
        ).scalar_one_or_none()
        if provider and provider.enabled:
            return provider

    provider = (
        await db.execute(
            select(ModelProvider)
            .where(ModelProvider.enabled.is_(True), ModelProvider.is_default.is_(True))
            .order_by(ModelProvider.sort_order)
            .limit(1)
        )
    ).scalar_one_or_none()
    if provider:
        return provider

    provider = (
        await db.execute(
            select(ModelProvider)
            .where(ModelProvider.enabled.is_(True))
            .order_by(ModelProvider.sort_order, ModelProvider.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if provider is None:
        raise LLMError("系统中没有可用的大模型，请先在「模型管理」中添加并启用一个模型")
    return provider


async def get_default_provider_id(db: AsyncSession) -> Optional[int]:
    provider = (
        await db.execute(
            select(ModelProvider)
            .where(ModelProvider.enabled.is_(True), ModelProvider.is_default.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()
    return provider.id if provider else None


async def list_enabled_providers(db: AsyncSession) -> List[ModelProvider]:
    return list(
        (
            await db.execute(
                select(ModelProvider)
                .where(ModelProvider.enabled.is_(True))
                .order_by(ModelProvider.sort_order, ModelProvider.id)
            )
        ).scalars().all()
    )
