"""RAGFlow OpenAPI 客户端。

职责：把 RAGFlow 当作纯检索内核调用 —— 建库、上传、解析、列片段、检索。
不含任何业务权限逻辑（权限在 permission.py）。

RAGFlow OpenAPI 统一响应格式为 {"code": 0, "message": "", "data": ...}，
code 非 0 即业务失败，因此不能只看 HTTP 状态码。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

from app.core.config import settings
from app.services.errors import RetrievalError, RetrievalNotConfigured

logger = logging.getLogger(__name__)


class RAGFlowError(RetrievalError):
    """RAGFlow 返回业务错误或网络异常。是 RetrievalError 的子类，
    所以调用方只需捕获 RetrievalError 就能同时覆盖两个后端。"""


class RAGFlowNotConfigured(RetrievalNotConfigured):
    pass


class RAGFlowClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[int] = None,
    ):
        self.base_url = (base_url or settings.RAGFLOW_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.RAGFLOW_API_KEY
        self.timeout = timeout or settings.RAGFLOW_TIMEOUT

    # ------------------------------------------------------------ 基础设施

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise RAGFlowNotConfigured(
                "RAGFlow API Key 未配置。请登录 RAGFlow 界面生成 API Key，"
                "填入 deploy/.env 的 RAGFLOW_API_KEY 后重启业务后端。"
            )
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Sequence[Tuple[str, Tuple[str, bytes, str]]]] = None,
        raw: bool = False,
    ) -> Any:
        url = f"{self.base_url}/api/v1{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(
                    method, url, headers=self._headers(),
                    params=params, json=json, data=data, files=files,
                )
        except httpx.HTTPError as exc:
            raise RAGFlowError(f"无法连接 RAGFlow({self.base_url})：{exc}") from exc

        if resp.status_code >= 500:
            raise RAGFlowError(f"RAGFlow 服务异常 HTTP {resp.status_code}", status=resp.status_code)

        try:
            payload = resp.json()
        except ValueError:
            raise RAGFlowError(
                f"RAGFlow 返回非 JSON 内容 HTTP {resp.status_code}: {resp.text[:200]}",
                status=resp.status_code,
            ) from None

        if isinstance(payload, dict) and "code" in payload:
            if payload.get("code") not in (0, None):
                raise RAGFlowError(
                    str(payload.get("message") or payload.get("msg") or "未知错误"),
                    code=payload.get("code"),
                    status=resp.status_code,
                )
            return payload.get("data")

        if resp.status_code >= 400:
            raise RAGFlowError(f"RAGFlow 请求失败 HTTP {resp.status_code}", status=resp.status_code)

        return payload

    # ------------------------------------------------------------ 连通性

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/datasets", params={"page": 1, "page_size": 1})
            return True
        except RAGFlowError:
            return False

    async def version(self) -> Optional[str]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/v1/system/version")
                if resp.status_code == 200:
                    return resp.json().get("data") or resp.json().get("version")
        except Exception:  # noqa: BLE001
            return None
        return None

    # ------------------------------------------------------------ 知识库 (dataset)

    async def create_dataset(
        self,
        name: str,
        description: str = "",
        embedding_model: str = "bge-m3",
        chunk_method: str = "naive",
        permission: str = "me",
    ) -> Dict[str, Any]:
        payload = {
            "name": name,
            "description": description or f"极睿知识库 - {name}",
            "embedding_model": embedding_model,
            "chunk_method": chunk_method,
            "permission": permission,
        }
        return await self._request("POST", "/datasets", json=payload)

    async def list_datasets(self, page: int = 1, page_size: int = 100) -> List[Dict[str, Any]]:
        data = await self._request(
            "GET", "/datasets", params={"page": page, "page_size": page_size, "orderby": "create_time", "desc": True}
        )
        return data or []

    async def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        data = await self._request("GET", "/datasets", params={"id": dataset_id, "page": 1, "page_size": 1})
        if isinstance(data, list) and data:
            return data[0]
        return None

    async def update_dataset(self, dataset_id: str, **fields: Any) -> Any:
        return await self._request("PUT", f"/datasets/{dataset_id}", json=fields)

    async def delete_datasets(self, dataset_ids: List[str]) -> Any:
        return await self._request("DELETE", "/datasets", json={"ids": dataset_ids})

    # ------------------------------------------------------------ 文档

    async def upload_documents(
        self,
        dataset_id: str,
        files: Sequence[Tuple[str, bytes, str]],
        metas: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        """files: [(filename, content_bytes, mime)]

        metas 是给本地内核用的落盘路径提示（见 retriever.LocalRetriever），
        RAGFlow 自己管存储，这里接收后忽略，保证两个后端签名一致。
        """
        multipart = [("file", (name, content, mime or "application/octet-stream")) for name, content, mime in files]
        data = await self._request("POST", f"/datasets/{dataset_id}/documents", files=multipart)
        return data or []

    async def list_documents(
        self, dataset_id: str, page: int = 1, page_size: int = 100, keywords: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size, "orderby": "create_time", "desc": True}
        if keywords:
            params["keywords"] = keywords
        data = await self._request("GET", f"/datasets/{dataset_id}/documents", params=params)
        return data or {}

    async def update_document(self, dataset_id: str, document_id: str, **fields: Any) -> Any:
        return await self._request("PUT", f"/datasets/{dataset_id}/documents/{document_id}", json=fields)

    async def delete_documents(self, dataset_id: str, document_ids: List[str]) -> Any:
        return await self._request(
            "DELETE", f"/datasets/{dataset_id}/documents", json={"ids": document_ids}
        )

    async def parse_documents(self, dataset_id: str, document_ids: List[str]) -> Any:
        """触发解析与切片。RAGFlow 侧异步执行，需轮询文档状态。"""
        return await self._request(
            "POST", f"/datasets/{dataset_id}/chunks", json={"document_ids": document_ids}
        )

    async def stop_parse(self, dataset_id: str, document_ids: List[str]) -> Any:
        return await self._request(
            "DELETE", f"/datasets/{dataset_id}/chunks", json={"document_ids": document_ids}
        )

    # ------------------------------------------------------------ 片段

    async def list_chunks(
        self,
        dataset_id: str,
        document_id: str,
        page: int = 1,
        page_size: int = 100,
        keywords: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"page": page, "page_size": page_size}
        if keywords:
            params["keywords"] = keywords
        data = await self._request(
            "GET", f"/datasets/{dataset_id}/documents/{document_id}/chunks", params=params
        )
        return data or {}

    async def list_all_chunks(
        self, dataset_id: str, document_id: str, max_chunks: int = 5000
    ) -> List[Dict[str, Any]]:
        """翻页拉取一篇文档的全部片段（用于管理员挑选授权片段）。"""
        out: List[Dict[str, Any]] = []
        page = 1
        page_size = 100
        while len(out) < max_chunks:
            data = await self.list_chunks(dataset_id, document_id, page=page, page_size=page_size)
            chunks = (data or {}).get("chunks") or []
            if not chunks:
                break
            out.extend(chunks)
            total = (data or {}).get("total") or 0
            if page * page_size >= total:
                break
            page += 1
        return out[:max_chunks]

    # ------------------------------------------------------------ 检索

    async def retrieval(
        self,
        question: str,
        dataset_ids: List[str],
        document_ids: Optional[List[str]] = None,
        top_k: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
        vector_similarity_weight: Optional[float] = None,
        rerank_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """向量 + 全文混合检索。

        document_ids 是权限收敛的关键入口：把用户看不到的文档直接排除在召回之外，
        但仍然要在调用方做一次 chunk 级复核（见 permission.py）。
        """
        if not dataset_ids:
            return {"chunks": [], "doc_aggs": [], "total": 0}

        k = top_k or settings.RETRIEVAL_TOP_K
        payload: Dict[str, Any] = {
            "question": question,
            "dataset_ids": dataset_ids,
            "document_ids": document_ids or [],
            "page": 1,
            "page_size": k,
            "top_k": max(1024, k),
            "similarity_threshold": (
                similarity_threshold
                if similarity_threshold is not None
                else settings.SIMILARITY_THRESHOLD
            ),
            "vector_similarity_weight": (
                vector_similarity_weight
                if vector_similarity_weight is not None
                else settings.VECTOR_SIMILARITY_WEIGHT
            ),
            "keyword": False,
            "highlight": False,
        }
        if rerank_id:
            payload["rerank_id"] = rerank_id
        data = await self._request("POST", "/retrieval", json=payload)
        return data or {"chunks": [], "doc_aggs": [], "total": 0}


ragflow_client = RAGFlowClient()
