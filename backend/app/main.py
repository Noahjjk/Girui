"""极睿知识库 业务后端 入口。"""
from __future__ import annotations

import logging
import logging.config
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.router import api_router
from app.core.config import settings
from app.db.seed import check_runtime, seed_initial_data, warn_insecure_config
from app.db.session import init_models
from app.services.index_store import get_index_store
from app.services.ingest import get_ingest_manager

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("jirui")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 68)
    logger.info("%s 后端启动，版本 %s", settings.APP_NAME, settings.APP_VERSION)
    logger.info("=" * 68)
    warn_insecure_config()
    check_runtime()
    await init_models()
    try:
        await seed_initial_data()
    except Exception:  # noqa: BLE001
        logger.exception("初始化数据失败（不阻断启动）")

    # 轻量内核（RETRIEVAL_BACKEND=local）才需要进程内的解析工作者：
    # 启动时会自动续跑上次中断的文档，避免卡在「解析中」。
    if settings.use_local_retrieval:
        try:
            await get_ingest_manager().start()
        except Exception:  # noqa: BLE001
            logger.exception("检索索引初始化失败，文档上传解析将不可用")

    logger.info("就绪，监听 %s", settings.API_PREFIX)
    yield

    if settings.use_local_retrieval:
        try:
            await get_ingest_manager().stop()
            await get_index_store().close()
        except Exception:  # noqa: BLE001
            logger.exception("关闭检索索引时出错")
    logger.info("后端退出")


app = FastAPI(
    title=f"{settings.APP_NAME} API",
    description=(
        "企业级私有知识库系统。检索内核可插拔（内置轻量内核或外部 RAGFlow），"
        "本服务负责认证、权限隔离、知识管理、问答编排与审计。"
    ),
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


@app.middleware("http")
async def access_log(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("请求处理异常 %s %s", request.method, request.url.path)
        raise
    elapsed = int((time.perf_counter() - started) * 1000)
    if request.url.path != f"{settings.API_PREFIX}/health":
        logger.info(
            "%s %s -> %s (%dms)",
            request.method, request.url.path, response.status_code, elapsed,
        )
    response.headers["X-Process-Time-Ms"] = str(elapsed)
    return response


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(x) for x in first.get("loc", [])[1:]) or "请求参数"
    return JSONResponse(
        status_code=422,
        content={"detail": f"{field}：{first.get('msg', '格式不正确')}", "errors": exc.errors()},
    )


@app.exception_handler(SQLAlchemyError)
async def db_error_handler(request: Request, exc: SQLAlchemyError):
    logger.exception("数据库错误")
    return JSONResponse(status_code=500, content={"detail": "数据库操作失败，请稍后重试"})


app.include_router(api_router, prefix=settings.API_PREFIX)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "api": settings.API_PREFIX,
        "docs": "/docs" if settings.DEBUG else None,
    }
