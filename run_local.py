"""极睿知识库 本地启动入口（带前端静态托管）。"""
import os
import sys
from pathlib import Path

# 下载全部模型命令参考：python scripts/download_models.py --model all

# 配置工程路径
PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# 设置本地 Windows 环境变量覆盖
os.environ["DATA_DIR"] = str(PROJECT_ROOT / "data")
os.environ["LOCAL_EMBEDDING_DIR"] = str(PROJECT_ROOT / "models" / "bge-small-zh-v1.5")
os.environ["INDEX_DB_PATH"] = str(PROJECT_ROOT / "data" / "index" / "chunks.db")
os.environ["ALLOWED_ORIGINS"] = "null,http://localhost:8000,http://127.0.0.1:8000,http://localhost,http://127.0.0.1"

import uvicorn
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from backend.app.main import app

# 替换 app 原有的 root 路由
app.router.routes = [r for r in app.router.routes if getattr(r, 'path', None) != "/"]

web_dir = PROJECT_ROOT / "web"
assets_dir = web_dir / "assets"

if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    fav = web_dir / "favicon.ico"
    if fav.exists():
        return FileResponse(str(fav), media_type="image/x-icon")
    return FileResponse(str(web_dir / "index.html"))

@app.get("/favicon.png", include_in_schema=False)
async def serve_favicon_png():
    fav = web_dir / "favicon.png"
    if fav.exists():
        return FileResponse(str(fav), media_type="image/png")
    return FileResponse(str(web_dir / "index.html"))

@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = web_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "Web UI not found"}

@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    # API 接口直接由已注册的 router 处理，只有未匹配的静态/前端路由才 fallback 到 index.html
    index_file = web_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "Web UI not found"}

if __name__ == "__main__":
    print(f"Starting server on http://127.0.0.1:8000 ...")
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
