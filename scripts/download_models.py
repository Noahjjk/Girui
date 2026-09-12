"""下载/同步极睿知识库所需模型到目标目录。

支持模型：
1. bge-small-zh-v1.5（ONNX量化版，默认轻量嵌入模型，约 25MB）
   存放位置：models/bge-small-zh-v1.5/
2. bge-m3（多语言长文本嵌入模型，可选）
   存放位置：models/bge-m3/

用法：
    python scripts/download_models.py                # 默认下载/准备 bge-small-zh-v1.5
    python scripts/download_models.py --model all    # 下载全部支持的模型
    python scripts/download_models.py --model bge-m3 # 下载 bge-m3 模型
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT_DIR / "models"
RAG_DB_MODELS = Path("E:/rag_database/backend/models")

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
UA = "jirui-kb-model-downloader/1.0"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(url: str, timeout: int = 180, max_redirects: int = 8):
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


def _download_file(url: str, dest: Path) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  [已存在] {dest.name} ({dest.stat().st_size / 1048576:.2f} MB)")
        return True

    print(f"  [下载中] {dest.name} <- {url}")
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with _open(url) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if total > 0:
                        pct = done / total * 100
                        sys.stdout.write(f"\r         进度: {pct:5.1f}% ({done/1048576:.1f}/{total/1048576:.1f} MB)")
                        sys.stdout.flush()
            if total > 0:
                sys.stdout.write("\n")
            if total and done != total:
                tmp.unlink(missing_ok=True)
                print(f"         [失败] 文件大小不匹配: {done}/{total}")
                return False
            tmp.replace(dest)
        print(f"         [完成] {dest.name} ({dest.stat().st_size / 1048576:.2f} MB)")
        return True
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"\n         [异常] {e}")
        return False


def setup_bge_small(dest_dir: Path) -> bool:
    print(f"\n=== [1/1] 配置 bge-small-zh-v1.5 嵌入模型 ===")
    print(f"目标路径: {dest_dir}")

    # 1. 优先从本地已有目录复制（避免重复下载）
    local_source = RAG_DB_MODELS / "bge-small-zh-v1.5"
    if local_source.exists() and (local_source / "onnx" / "model_quantized.onnx").exists():
        print(f"  -> 检测到本地已存在完整模型文件: {local_source}")
        print(f"  -> 正在直接复制到 {dest_dir}...")
        dest_dir.mkdir(parents=True, exist_ok=True)
        for item in local_source.rglob("*"):
            if item.is_file():
                rel = item.relative_to(local_source)
                target = dest_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.stat().st_size != item.stat().st_size:
                    shutil.copy2(item, target)
        print("  [完成] 本地同步成功！")
        return True

    # 2. 本地不存在时从 HuggingFace / hf-mirror 镜像站下载
    repo = "onnx-community/bge-small-zh-v1.5-ONNX"
    files = [
        "config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "vocab.txt",
        "onnx/model_quantized.onnx",
        "onnx/model_quantized.onnx_data",
    ]

    success = True
    for f in files:
        url = f"{HF_ENDPOINT}/{repo}/resolve/main/{f}"
        target = dest_dir / f
        if not _download_file(url, target):
            # 尝试 ModelScope 兜底
            ms_url = (
                f"https://www.modelscope.cn/api/v1/models/AI-ModelScope/bge-small-zh-v1.5/repo"
                f"?Revision=master&FilePath={urllib.parse.quote(f)}"
            )
            print(f"  -> 尝试 ModelScope 镜像源: {f}")
            if not _download_file(ms_url, target):
                success = False

    return success


def setup_bge_m3(dest_dir: Path) -> bool:
    print(f"\n=== 配置 bge-m3 嵌入模型 ===")
    print(f"目标路径: {dest_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 1. 优先尝试阿里 ModelScope（国内直连高速，不依赖外网代理）
    try:
        from modelscope.hub.snapshot_download import snapshot_download as ms_download
        print("  -> 正在使用阿里 ModelScope 镜像源下载 bge-m3（国内高速通道）...")
        model_path = ms_download(
            model_id="BAAI/bge-m3",
            local_dir=str(dest_dir),
        )
        print(f"  [完成] ModelScope 下载成功！存储路径: {model_path}")
        return True
    except Exception as e:
        print(f"  -> ModelScope 下载异常 ({e})，尝试 Hugging Face 镜像源...")

    # 2. 备用：Hugging Face 镜像站 / 官方源
    try:
        from huggingface_hub import snapshot_download
        print(f"  -> 使用 huggingface_hub (节点: {HF_ENDPOINT}) 下载 bge-m3...")
        snapshot_download(
            repo_id="BAAI/bge-m3",
            local_dir=str(dest_dir),
            endpoint=HF_ENDPOINT,
            max_workers=4,
            resume_download=True,
        )
        print("  [完成] bge-m3 下载成功！")
        return True
    except Exception as e:
        print(f"  [失败] HuggingFace 源下载失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="极睿知识库模型下载脚本")
    parser.add_argument(
        "--model",
        choices=["bge-small", "bge-m3", "all"],
        default="bge-small",
        help="选择要下载的模型：bge-small（系统默认）、bge-m3 或 all",
    )
    args = parser.parse_args()

    bge_small_dir = MODELS_DIR / "bge-small-zh-v1.5"
    bge_m3_dir = MODELS_DIR / "bge-m3"

    ok = True
    if args.model in ("bge-small", "all"):
        if not setup_bge_small(bge_small_dir):
            ok = False

    if args.model in ("bge-m3", "all"):
        if not setup_bge_m3(bge_m3_dir):
            ok = False

    if ok:
        print("\n==========================================")
        print(" 模型配置完成！")
        print(f" 默认嵌入模型路径: {bge_small_dir}")
        print("==========================================")
    else:
        print("\n部分模型下载未完成，请检查网络或代理设置。")


if __name__ == "__main__":
    main()
