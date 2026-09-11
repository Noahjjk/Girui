"""本地嵌入模型（ONNX Runtime 跑 bge-small-zh-v1.5）。

刻意不依赖 transformers / PyTorch：
  PyTorch 一 import 就吃 300~400 MB 常驻内存，目标服务器总共只有 1.6 GB。
  这里只用 tokenizers（读 tokenizer.json）+ onnxruntime（跑 int8 计算图），
  常驻内存约 150~200 MB。

池化方式：BGE 系列用 CLS 池化（取 last_hidden_state 的第 0 个 token），
再做 L2 归一化。归一化后余弦相似度等价于点积，检索侧直接用矩阵乘法即可。

模型目录结构（由 scripts/prepare_model.py 准备）：
    models/bge-small-zh-v1.5/
      config.json
      tokenizer.json
      onnx/model_quantized.onnx
      onnx/model_quantized.onnx_data
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingError(RuntimeError):
    pass


class LocalEmbedder:
    """线程安全的本地嵌入器。首次使用时才加载模型。"""

    def __init__(
        self,
        model_dir: Optional[str] = None,
        max_tokens: Optional[int] = None,
        batch_size: Optional[int] = None,
        instruction: Optional[str] = None,
    ):
        self.model_dir = Path(model_dir or settings.local_embedding_dir)
        self.max_tokens = max_tokens or settings.LOCAL_EMBEDDING_MAX_TOKENS
        self.batch_size = batch_size or settings.EMBED_BATCH_SIZE
        self.instruction = (instruction if instruction is not None
                            else settings.EMBED_QUERY_INSTRUCTION)

        self._lock = threading.Lock()
        self._tokenizer = None
        self._session = None
        self._input_names: List[str] = []
        self._output_name: str = ""
        self._output_is_embedding = False
        self._dim: int = 0
        self._loaded = False

    # ------------------------------------------------------------ 加载

    def _onnx_path(self) -> Path:
        for name in ("model_quantized.onnx", "model.onnx", "model_optimized.onnx"):
            p = self.model_dir / "onnx" / name
            if p.exists():
                return p
        raise EmbeddingError(
            f"未找到 ONNX 模型文件（{self.model_dir / 'onnx'}）。"
            "请先执行 python scripts/prepare_model.py 下载模型。"
        )

    def load(self) -> None:
        """加载模型（幂等，线程安全）。"""
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return

            tok_path = self.model_dir / "tokenizer.json"
            if not tok_path.exists():
                raise EmbeddingError(
                    f"未找到 {tok_path}。请先执行 python scripts/prepare_model.py 下载模型。"
                )

            try:
                from tokenizers import Tokenizer
            except ImportError as exc:  # pragma: no cover
                raise EmbeddingError("缺少 tokenizers 依赖，请 pip install tokenizers") from exc

            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover
                raise EmbeddingError("缺少 onnxruntime 依赖，请 pip install onnxruntime") from exc

            tokenizer = Tokenizer.from_file(str(tok_path))
            tokenizer.enable_truncation(max_length=self.max_tokens)
            pad_id = tokenizer.token_to_id("[PAD]")
            if pad_id is None:
                pad_id = 0
            tokenizer.enable_padding(pad_id=pad_id, pad_token="[PAD]")

            onnx_path = self._onnx_path()
            opts = ort.SessionOptions()
            # 这台机器 CPU 核数很少，线程开太多反而互相抢占
            opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2)))
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.log_severity_level = 3  # 只报 error，避免刷屏
            # 关掉 CPU arena：它会在首次推理时吃掉一大块内存并终身持有，
            # 实测常驻 200 MB 以上，且 malloc_trim 也回收不了（不是 glibc 堆）。
            # 详见 config.EMBEDDING_MEM_ARENA 的说明。
            opts.enable_cpu_mem_arena = settings.EMBEDDING_MEM_ARENA

            session = ort.InferenceSession(
                str(onnx_path), sess_options=opts, providers=["CPUExecutionProvider"]
            )

            self._tokenizer = tokenizer
            self._session = session
            self._input_names = [i.name for i in session.get_inputs()]
            outputs = session.get_outputs()
            # 优先取句向量输出；没有就取最后一个输出（通常是 last_hidden_state）
            picked = None
            for o in outputs:
                if o.name in ("sentence_embedding", "embeddings"):
                    picked = o
                    break
            if picked is None:
                picked = outputs[0]
            self._output_name = picked.name
            self._output_is_embedding = picked.name in ("sentence_embedding", "embeddings")
            self._dim = int(picked.shape[-1]) if isinstance(picked.shape[-1], int) else 0
            self._loaded = True

            logger.info(
                "本地嵌入模型已加载 path=%s inputs=%s output=%s dim=%s",
                onnx_path.name, self._input_names, self._output_name, self._dim or "?",
            )

    # ------------------------------------------------------------ 属性

    @property
    def dim(self) -> int:
        self.load()
        if not self._dim:
            # 拿一条短文本探一下真实维度
            self._dim = int(self.encode(["测试"])[0].shape[0])
        return self._dim

    @property
    def available(self) -> bool:
        try:
            self.load()
            return True
        except EmbeddingError:
            return False

    # ------------------------------------------------------------ 推理

    def _forward(self, texts: Sequence[str]) -> np.ndarray:
        assert self._tokenizer is not None and self._session is not None
        encs = self._tokenizer.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encs], dtype=np.int64)
        attention = np.array([e.attention_mask for e in encs], dtype=np.int64)

        feed = {}
        for name in self._input_names:
            if name == "input_ids":
                feed[name] = input_ids
            elif name == "attention_mask":
                feed[name] = attention
            elif name == "token_type_ids":
                feed[name] = np.zeros_like(input_ids)

        out = self._session.run([self._output_name], feed)[0]

        if self._output_is_embedding:
            vectors = out
        else:
            # (batch, seq, hidden) → CLS 池化
            vectors = out[:, 0, :]

        vectors = np.asarray(vectors, dtype=np.float32)
        if vectors.ndim == 1:
            vectors = vectors.reshape(1, -1)
        return _l2_normalize(vectors)

    def encode(
        self,
        texts: Sequence[str],
        batch_size: Optional[int] = None,
        is_query: bool = False,
    ) -> np.ndarray:
        """把文本编码为 L2 归一化的 float32 向量矩阵 (n, dim)。"""
        self.load()
        items = [t if isinstance(t, str) else str(t) for t in texts]
        if is_query and self.instruction:
            items = [f"{self.instruction}{t}" for t in items]
        if not items:
            return np.zeros((0, self.dim), dtype=np.float32)

        bs = batch_size or self.batch_size
        chunks: List[np.ndarray] = []
        # tokenizers 的 Rust 绑定与 onnxruntime 会话都不是绝对可重入的，
        # 这台机器核数少，串行化反而更稳定
        with self._lock:
            for i in range(0, len(items), bs):
                chunks.append(self._forward(items[i : i + bs]))
        return np.vstack(chunks) if chunks else np.zeros((0, self.dim), dtype=np.float32)

    def encode_one(self, text: str, is_query: bool = False) -> np.ndarray:
        return self.encode([text], is_query=is_query)[0]


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


# ---------------------------------------------------------------- 多模型支持与单例缓存

_embedders: Dict[str, LocalEmbedder] = {}
_embedder_lock = threading.Lock()


def get_embedder(model_name: Optional[str] = None) -> LocalEmbedder:
    """根据模型名称获取对应的嵌入器实例。

    支持：
      - 'bge-m3' 或包含 'm3'：加载 models/bge-m3 (1024维，多语言高精度)
      - 'bge-small-zh-v1.5' 或默认：加载 models/bge-small-zh-v1.5 (512维，轻量级)
    """
    key = (model_name or settings.EMBEDDING_MODEL).strip()
    # 归一化 key
    if "m3" in key.lower():
        norm_key = "bge-m3"
    else:
        norm_key = "bge-small-zh-v1.5"

    if norm_key not in _embedders:
        with _embedder_lock:
            if norm_key not in _embedders:
                if norm_key == "bge-m3":
                    m_dir = Path(settings.DATA_DIR).parent / "models" / "bge-m3"
                    if not m_dir.exists():
                        m_dir = Path("E:/jirui/models/bge-m3")
                    _embedders[norm_key] = LocalEmbedder(model_dir=str(m_dir))
                else:
                    small_dir = Path(settings.local_embedding_dir)
                    if not (small_dir / "tokenizer.json").exists():
                        small_dir = Path("E:/jirui/models/bge-small-zh-v1.5")
                    _embedders[norm_key] = LocalEmbedder(model_dir=str(small_dir))
    return _embedders[norm_key]


def embedding_available(model_name: Optional[str] = None) -> bool:
    try:
        return get_embedder(model_name).available
    except Exception:  # noqa: BLE001
        return False
