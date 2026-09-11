"""进程内内存回收。

小内存机器上的现实问题：解析 + 嵌入一份文档会申请大量临时对象，
Python 把这些对象释放掉之后，glibc 往往**不会**把空闲的堆页还给内核，
RSS 就停在高水位不下来。文档一份接一份地来，堆只涨不落，
最后撞上 cgroup 的 `MemoryMax` 被 OOM 杀掉 —— 而且现象很迷惑：
明明业务已经忙完，内存占用却一直挂在那里。

所以每份文档处理完后显式做两件事：

  1. ``gc.collect()``   —— 回收 Python 对象（含循环引用）
  2. ``malloc_trim(0)`` —— 让 glibc 把空闲的堆页归还内核

第 2 步是 Linux/glibc 专属，其它平台或非 glibc 环境静默跳过。
两个调用都可能抛异常（比如某些精简镜像里没有 libc 符号），
但内存回收是「尽力而为」，绝不该影响主流程，因此全部吞掉。
"""
from __future__ import annotations

import ctypes
import gc
import logging
import sys

logger = logging.getLogger(__name__)

_libc = None
_probed = False


def _load_libc():
    """惰性查找能用的 libc（只在 Linux 上尝试）。"""
    global _libc, _probed
    if _probed:
        return _libc
    _probed = True
    if not sys.platform.startswith("linux"):
        return None
    for name in ("libc.so.6", "libc.so"):
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        if hasattr(lib, "malloc_trim"):
            _libc = lib
            break
    if _libc is None:
        logger.debug("未找到带 malloc_trim 的 libc，跳过堆归还")
    return _libc


def release_memory() -> None:
    """尽力把已释放的内存还给操作系统。永不抛异常。"""
    try:
        gc.collect()
    except Exception:  # noqa: BLE001
        logger.debug("gc.collect 失败", exc_info=True)

    lib = _load_libc()
    if lib is None:
        return
    try:
        lib.malloc_trim(0)
    except Exception:  # noqa: BLE001
        logger.debug("malloc_trim 失败", exc_info=True)


def rss_mb() -> float:
    """当前进程 RSS（MB）。非 Linux 返回 -1。"""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * 4096 / 1024 / 1024
    except Exception:  # noqa: BLE001
        return -1.0
