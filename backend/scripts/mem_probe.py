"""诊断：嵌入与切片过程的真实内存行为，以及 glibc 堆是否会归还给操作系统。

在服务器上直接跑，避免隔着网络猜。
用法：
    python scripts/mem_probe.py               # 默认 2000 条，关闭 ONNX arena
    python scripts/mem_probe.py 2000 off      # 条数 + 是否开 arena
"""
from __future__ import annotations

import ctypes
import gc
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
ARENA = (sys.argv[2] if len(sys.argv) > 2 else "off").strip().lower() in ("on", "1", "true", "yes")

# 必须在导入应用配置之前设好：Settings 是 import 期实例化的
os.environ["EMBEDDING_MEM_ARENA"] = "true" if ARENA else "false"


def rss_mb() -> float:
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * 4096 / 1024 / 1024


def trim() -> None:
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception as exc:  # noqa: BLE001
        print("  malloc_trim 不可用：", exc)


def step(label: str) -> None:
    print(f"  {label:38s} RSS = {rss_mb():7.1f} MB")


print(f"Python {sys.version.split()[0]}   PID {os.getpid()}")
print(f"片段数 {N}   ONNX CPU arena = {'开' if ARENA else '关'}")
step("启动基线")

print("\n[1] 导入依赖（tokenizers / onnxruntime / jieba 各自吃多少）")
from app.services.embedding import get_embedder  # noqa: E402

step("导入 embedding 模块后")

import jieba  # noqa: E402

jieba.initialize()
step("jieba.initialize() 后")

embedder = get_embedder()
embedder.load()
step("onnxruntime 模型加载后")

print(f"\n[2] 造 {N} 条典型片段（约 400 字/条）")
para = (
    "第{n}条 为保证公司各项业务规范有序开展，相关部门应严格按照本制度规定执行。"
    "各业务单元在实施过程中如遇特殊情形，应及时向归口管理部门书面报告。"
)
texts = [para.format(n=i) * 3 for i in range(N)]
total_bytes = sum(len(t.encode()) for t in texts)
print(f"  共 {len(texts)} 条，{total_bytes / 1024 / 1024:.2f} MB 文本")
step("文本就绪")

print("\n[3] 逐批编码（与生产路径一致）")
import numpy as np  # noqa: E402

t0 = time.perf_counter()
views = []
for i in range(0, len(texts), 16):
    views.append(embedder.encode(texts[i : i + 16]))
dt = time.perf_counter() - t0
step(f"编码完成（{dt:.1f}s，{len(texts) / dt:.0f} 条/秒）")

mat = np.vstack(views)
print(f"  向量矩阵 {mat.shape} = {mat.nbytes / 1024 / 1024:.1f} MB")
step("向量矩阵驻留")

print("\n[4] 释放对象 + GC")
del views, mat, texts
gc.collect()
step("del + gc.collect() 之后")

print("\n[5] 把 glibc 堆归还给操作系统")
trim()
step("malloc_trim(0) 之后")

print("\n[6] 再来一轮，看是否继续增长（有无累积）")
texts = [para.format(n=i) * 3 for i in range(N)]
views = [embedder.encode(texts[i : i + 16]) for i in range(0, len(texts), 16)]
mat = np.vstack(views)
step("第二轮编码后")
del views, mat, texts
gc.collect()
trim()
step("第二轮释放后")

print("\n结论判读：")
print("  · 若第 4 步与第 1 步差距很大、而第 5 步明显回落 → glibc 堆滞留，定时 malloc_trim 即可")
print("  · 若第 6 步比第 3 步还高 → 存在真正的累积增长，需要按文档粒度回收")
