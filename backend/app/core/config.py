"""全局配置。所有可调项集中于此，来源为环境变量 / .env。"""
from functools import lru_cache
from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（app/core/config.py → 上溯三层）
BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------------- 应用 ----------------
    APP_NAME: str = "极睿知识库"
    APP_VERSION: str = "0.1.0"
    API_PREFIX: str = "/api/v1"
    DEBUG: bool = False
    TZ: str = "Asia/Shanghai"

    # ---------------- 数据库 ----------------
    # 轻量部署默认走 SQLite（单文件、零进程，适配小内存服务器）。
    # 留空则按 DATA_DIR 推导；要切回 PostgreSQL，填完整 DSN 即可：
    #   postgresql+asyncpg://jirui:jirui@postgres:5432/jirui_kb
    DATABASE_URL: str = ""
    DB_ECHO: bool = False

    # ---------------- 认证 ----------------
    JWT_SECRET: str = "dev-only-secret-please-change-must-be-32-chars-minimum"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # 「记住登录」勾选时的 Refresh Token 有效期
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    # 未勾选时的有效期
    REFRESH_TOKEN_EXPIRE_HOURS_SHORT: int = 12
    LOGIN_MAX_FAIL: int = 5
    LOGIN_LOCK_MINUTES: int = 15

    # 用于加密存储模型 API Key，Fernet 密钥（base64 32 字节）
    SECRET_ENCRYPTION_KEY: str = ""

    # ---------------- 检索内核 ----------------
    # local   = 内置轻量内核（SQLite 全文 + 本地 ONNX 嵌入），适配小内存服务器
    # ragflow = 外部 RAGFlow 服务（需要 4 核 / 16 GB 级别的机器）
    RETRIEVAL_BACKEND: str = "local"
    # 轻量内核的索引库位置，留空则 DATA_DIR/index.db
    INDEX_DB_PATH: str = ""

    # ---------------- RAGFlow（RETRIEVAL_BACKEND=ragflow 时生效） ----------------
    RAGFLOW_BASE_URL: str = "http://ragflow:9380"
    RAGFLOW_API_KEY: str = ""
    RAGFLOW_TIMEOUT: int = 120

    # ---------------- 嵌入模型 ----------------
    EMBEDDING_MODEL: str = "bge-small-zh-v1.5"
    EMBEDDING_DIM: int = 512
    # 本地 ONNX 模型目录，留空则 <backend>/models/bge-small-zh-v1.5
    LOCAL_EMBEDDING_DIR: str = ""
    LOCAL_EMBEDDING_MAX_TOKENS: int = 512
    EMBED_BATCH_SIZE: int = 16
    # onnxruntime 的 CPU 内存 arena。
    # 打开时 ORT 会缓存一块大内存反复复用，推理略快，但**终身不归还**：
    # 实测小内存机器上常驻能到 200 MB 以上，且 malloc_trim 也救不回来
    # （因为那不是 glibc 堆，是 ORT 自己的分配器）。
    # 本机只有 1.6 GB，因此默认关闭：多几次 malloc，换回两百多兆常驻内存。
    EMBEDDING_MEM_ARENA: bool = False
    # bge-*-zh-v1.5 已不需要查询指令前缀，留空即可
    EMBED_QUERY_INSTRUCTION: str = ""

    # ---------------- 切片 ----------------
    CHUNK_SIZE: int = 400        # 目标字符数
    CHUNK_OVERLAP: int = 80      # 相邻片段重叠字符数
    CHUNK_MIN_SIZE: int = 24     # 短于该长度的碎片直接并入上一片

    # ---------------- 检索参数 ----------------
    RETRIEVAL_TOP_K: int = 30            # 检索候选条数
    RETRIEVAL_TOP_N: int = 8             # 经权限过滤+排序后进入提示词的条数
    SIMILARITY_THRESHOLD: float = 0.4    # 向量相似度下限（低于 0.40 不作为原文溯源参考）
    VECTOR_SIMILARITY_WEIGHT: float = 0.3  # RAGFlow 语义相似度权重
    HYBRID_VECTOR_WEIGHT: float = 0.6      # 轻量内核：向量分在混合排序中的权重
    CHAT_HISTORY_ROUNDS: int = 6           # 携带的历史轮数

    # ---------------- 上传 ----------------
    DATA_DIR: str = "/data"
    MAX_UPLOAD_SIZE_MB: int = 200
    ALLOWED_UPLOAD_EXT: str = (
        "pdf,doc,docx,txt,md,markdown,xls,xlsx,csv,pptx,ppt,html,htm,json,epub"
    )
    ALLOWED_IMAGE_EXT: str = "png,jpg,jpeg,webp,gif,bmp"

    # ---------------- 初始管理员 ----------------
    ADMIN_USERNAME: str = "admin"
    ADMIN_INIT_PASSWORD: str = "admin@123456"
    ADMIN_DISPLAY_NAME: str = "系统管理员"
    ADMIN_EMAIL: str = "admin@jirui.local"

    # ---------------- 默认模型 ----------------
    DEFAULT_LLM_PROVIDER: str = "deepseek"
    DEFAULT_LLM_MODEL: str = "deepseek-chat"
    DEFAULT_LLM_BASE_URL: str = "https://api.deepseek.com/v1"
    DEFAULT_LLM_API_KEY: str = ""

    # ---------------- CORS ----------------
    # `null` 不能删：Electron 桌面端从 file:// 加载，浏览器发出的 Origin 就是字面量
    # "null"。不显式放行的话，桌面端所有请求都会被 CORS 拦掉（Web 版走同源反代，不受影响）。
    ALLOWED_ORIGINS: str = "null,http://localhost,http://127.0.0.1"

    # ---------------- 版本更新 ----------------
    UPDATE_FEED_URL: str = ""
    DESKTOP_LATEST_VERSION: str = "0.1.0"

    @property
    def allowed_origins_list(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_upload_ext_set(self) -> set:
        return {e.strip().lower() for e in self.ALLOWED_UPLOAD_EXT.split(",") if e.strip()}

    @property
    def allowed_image_ext_set(self) -> set:
        return {e.strip().lower() for e in self.ALLOWED_IMAGE_EXT.split(",") if e.strip()}

    @property
    def upload_dir(self) -> str:
        return f"{self.DATA_DIR}/uploads"

    @property
    def log_dir(self) -> str:
        return f"{self.DATA_DIR}/logs"

    @property
    def index_dir(self) -> str:
        return f"{self.DATA_DIR}/index"

    @property
    def database_url(self) -> str:
        """留空则按 DATA_DIR 推导 SQLite 路径。"""
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return f"sqlite+aiosqlite:///{self.DATA_DIR}/jirui.db"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def index_db_path(self) -> str:
        return self.INDEX_DB_PATH or f"{self.index_dir}/chunks.db"

    @property
    def local_embedding_dir(self) -> str:
        return self.LOCAL_EMBEDDING_DIR or str(BACKEND_DIR / "models" / "bge-small-zh-v1.5")

    @property
    def use_local_retrieval(self) -> bool:
        return (self.RETRIEVAL_BACKEND or "local").strip().lower() != "ragflow"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
