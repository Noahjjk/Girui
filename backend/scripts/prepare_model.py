"""下载本地嵌入模型权重（bge-small-zh-v1.5，ONNX int8）。

为什么不用 transformers / PyTorch：
  PyTorch 一导入就吃 300~400 MB 常驻内存，而目标机器只有 1.6 GB 总内存。
  我们只需要「tokenizer.json + ONNX 计算图」，用 tokenizers + onnxruntime 即可推理，
  整个嵌入模块常驻内存可以压到 200 MB 以内。

模型来源：onnx-community/bge-small-zh-v1.5-ONNX
  取 onnx/model_quantized.onnx（int8，约 25 MB）。该导出使用外部权重格式，
  会同时产出 model_quantized.onnx_data，两者必须放在同一目录。

坑：hf-mirror 对非 LFS 的小文件（config/tokenizer 等）返回 307 跳转，
    urlopen 的默认重定向处理器在这个 URL 上会失败，必须手动跟 Location。
    LFS 大文件则直接 200。本脚本两类都处理，并带 /raw/ 与 ModelScope 兜底。

用法：
    python scripts/prepare_model.py                # 下载到 backend/models/bge-small-zh-v1.5
    python scripts/prepare_model.py --dest /data/models/bge-small-zh-v1.5
    python scripts/prepare_model.py --check        # 只校验，不下载
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DEST = BACKEND_DIR / "models" / "bge-small-zh-v1.5"

REPO = "onnx-community/bge-small-zh-v1.5-ONNX"
FALLBACK_REPO = "BAAI/bge-small-zh-v1.5"
UA = "jirui-kb-model-fetcher/1.0"

# (远端路径, 本地相对路径, 是否必需)
FILES = [
    ("config.json", "config.json", True),
    ("tokenizer.json", "tokenizer.json", True),
    ("tokenizer_config.json", "tokenizer_config.json", True),
    ("onnx/model_quantized.onnx", "onnx/model_quantized.onnx", True),
    ("onnx/model_quantized.onnx_data", "onnx/model_quantized.onnx_data", False),
    ("special_tokens_map.json", "special_tokens_map.json", False),
    ("vocab.txt", "vocab.txt", False),
]

# ModelScope 兜底（仅用于几个纯文本配置）
MODELSCOPE = {
    p: "https://www.modelscope.cn/api/v1/models/AI-ModelScope/bge-small-zh-v1.5/repo"
       "?Revision=master&FilePath=" + urllib.parse.quote(p)
    for p in ("config.json", "tokenizer.json", "tokenizer_config.json",
              "vocab.txt", "special_tokens_map.json")
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用自动重定向，交给 _open 手动处理。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")


def _candidates(repo_path: str) -> list[str]:
    ep = _endpoint()
    urls = [
        f"{ep}/{REPO}/resolve/main/{repo_path}",
        f"{ep}/{REPO}/raw/main/{repo_path}",
    ]
    base = repo_path.rsplit("/", 1)[-1]
    if not repo_path.startswith("onnx/"):
        urls.append(f"{ep}/{FALLBACK_REPO}/raw/main/{base}")
        urls.append(f"{ep}/{FALLBACK_REPO}/resolve/main/{base}")
    if repo_path in MODELSCOPE:
        urls.append(MODELSCOPE[repo_path])
    return urls


def _open(url: str, timeout: int = 120, max_redirects: int = 6):
    """打开 URL，手动跟随重定向。"""
    for _ in range(max_redirects):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            return _OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location")
                if loc:
                    url = urllib.parse.urljoin(url, loc)
                    continue
            raise
    raise RuntimeError("重定向次数过多")


def _download(repo_path: str, dest: Path, required: bool) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [跳过] {dest.name} 已存在 ({dest.stat().st_size / 1048576:.2f} MB)")
        return True

    print(f"  [下载] {repo_path}")
    last_err = "未知错误"
    for url in _candidates(repo_path):
        try:
            with _open(url) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                tmp = dest.with_suffix(dest.suffix + ".part")
                done = 0
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                if total and done != total:
                    last_err = f"长度不符 {done}/{total}"
                    tmp.unlink(missing_ok=True)
                    continue
                tmp.replace(dest)
            print(f"         完成 {dest.stat().st_size / 1048576:.2f} MB")
            return True
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code} ({urllib.parse.urlsplit(url).netloc})"
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue

    if required:
        print(f"         失败：{last_err}")
        return False
    print(f"         [可选，跳过] {last_err}")
    return True


def _verify(dest: Path) -> bool:
    missing = [rel for _, rel, req in FILES if req and not (dest / rel).exists()]
    print(f"\n模型目录：{dest}")
    total = 0
    for f in sorted(dest.rglob("*")):
        if f.is_file() and not f.name.endswith(".part"):
            size = f.stat().st_size
            total += size
            print(f"  {str(f.relative_to(dest)):<44} {size / 1048576:>8.2f} MB")
    print(f"  {'合计':<44} {total / 1048576:>8.2f} MB")

    if missing:
        print("\n缺失必需文件：")
        for m in missing:
            print(f"  - {m}")
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="下载本地嵌入模型（bge-small-zh-v1.5 ONNX int8）")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="模型存放目录")
    ap.add_argument("--check", action="store_true", help="只校验已有文件，不下载")
    args = ap.parse_args()

    dest = Path(args.dest)
    print(f"模型来源：{_endpoint()}/{REPO}")
    print(f"目标目录：{dest}\n")

    if args.check:
        return 0 if _verify(dest) else 1

    ok = True
    for repo_path, rel, required in FILES:
        ok = _download(repo_path, dest / rel, required=required) and ok

    if not _verify(dest) or not ok:
        print("\n模型准备失败。")
        return 1

    print("\n模型准备完成。后端启动时会从该目录加载。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
