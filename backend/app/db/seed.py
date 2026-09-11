"""首次部署时写入初始数据：管理员账号、默认模型、系统配置。"""
from __future__ import annotations

import logging

from sqlalchemy import func, select

from app.core.config import settings
from app.core.crypto import encrypt, is_dev_key
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.models.enums import ProviderKind, UserRole
from app.models.knowledge import KnowledgeBase
from app.models.llm import ModelProvider
from app.models.system import SystemConfig
from app.models.user import User

logger = logging.getLogger(__name__)


async def seed_initial_data() -> None:
    async with SessionLocal() as db:
        await _seed_admin(db)
        await _seed_provider(db)
        await _seed_configs(db)
        await db.commit()


async def _seed_admin(db) -> None:
    count = int((await db.execute(select(func.count(User.id)))).scalar() or 0)
    if count > 0:
        return

    admin = User(
        username=settings.ADMIN_USERNAME,
        password_hash=hash_password(settings.ADMIN_INIT_PASSWORD),
        display_name=settings.ADMIN_DISPLAY_NAME,
        email=settings.ADMIN_EMAIL,
        role=UserRole.ADMIN,
        is_active=True,
        must_change_password=False,
    )
    db.add(admin)
    await db.flush()
    logger.warning(
        "已创建初始管理员账号：%s（请立即登录并修改密码）", settings.ADMIN_USERNAME
    )


async def _seed_provider(db) -> None:
    count = int((await db.execute(select(func.count(ModelProvider.id)))).scalar() or 0)
    if count > 0:
        return
    if not settings.DEFAULT_LLM_API_KEY:
        logger.warning(
            "未配置 DEFAULT_LLM_API_KEY，跳过默认模型初始化。"
            "请登录后到「模型管理」手动添加模型。"
        )
        return

    db.add(
        ModelProvider(
            name=f"{settings.DEFAULT_LLM_PROVIDER} / {settings.DEFAULT_LLM_MODEL}",
            provider=ProviderKind(settings.DEFAULT_LLM_PROVIDER)
            if settings.DEFAULT_LLM_PROVIDER in {p.value for p in ProviderKind}
            else ProviderKind.OPENAI_COMPATIBLE,
            base_url=settings.DEFAULT_LLM_BASE_URL.rstrip("/"),
            model_name=settings.DEFAULT_LLM_MODEL,
            api_key_enc=encrypt(settings.DEFAULT_LLM_API_KEY),
            supports_vision=False,
            max_tokens=4096,
            temperature=0.3,
            enabled=True,
            is_default=True,
            sort_order=0,
            remark="由环境变量初始化的默认模型，可在「模型管理」中修改",
        )
    )
    await db.flush()
    logger.info("已初始化默认模型：%s", settings.DEFAULT_LLM_MODEL)


async def _seed_configs(db) -> None:
    defaults = {
        "site_title": {"value": settings.APP_NAME, "description": "系统标题"},
        "retrieval_top_n": {"value": settings.RETRIEVAL_TOP_N, "description": "进入提示词的片段数"},
        "retrieval_top_k": {"value": settings.RETRIEVAL_TOP_K, "description": "召回候选片段数"},
        "similarity_threshold": {
            "value": settings.SIMILARITY_THRESHOLD, "description": "相似度阈值"
        },
        "welcome_message": {
            "value": "你好，我是极睿知识库助手。请提问公司规范、影刀社区技术帖等相关问题。",
            "description": "问答页欢迎语",
        },
        "allow_self_register": {"value": False, "description": "是否允许自助注册（预留）"},
    }
    for key, meta in defaults.items():
        exists = (
            await db.execute(select(SystemConfig).where(SystemConfig.key == key))
        ).scalar_one_or_none()
        if exists is None:
            db.add(
                SystemConfig(key=key, value=meta["value"], description=meta["description"])
            )

    # 首次部署预置一个默认知识库，避免管理员登录后无处可传
    kb_count = int((await db.execute(select(func.count(KnowledgeBase.id)))).scalar() or 0)
    if kb_count == 0:
        db.add(
            KnowledgeBase(
                name="公司规范",
                code="company-spec",
                description="公司内部开发规范、流程与标准文档",
                embedding_model=settings.EMBEDDING_MODEL,
                chunk_method="naive",
            )
        )
        db.add(
            KnowledgeBase(
                name="影刀社区",
                code="yingdao-community",
                description="影刀 RPA 社区技术帖与最佳实践",
                embedding_model=settings.EMBEDDING_MODEL,
                chunk_method="naive",
            )
        )
        await db.flush()


def warn_insecure_config() -> None:
    if is_dev_key():
        logger.warning(
            "SECRET_ENCRYPTION_KEY 未配置，模型 API Key 正在使用开发密钥加密。"
            "生产环境请设置该环境变量。"
        )
    if settings.JWT_SECRET.startswith("dev-"):
        logger.warning("JWT_SECRET 使用默认值，生产环境请务必替换。")
    if not settings.use_local_retrieval and settings.RAGFLOW_API_KEY in (
        "", "CHANGE_ME_ragflow_api_key"
    ):
        logger.warning("RAGFLOW_API_KEY 未配置，RAGFlow 检索功能不可用。")


def check_runtime() -> None:
    """启动自检：把「跑起来才发现」的问题提前成一条明确的日志。

    本地内核缺模型是最容易踩的坑 —— 表现是上传能成功但解析永远失败，
    排查成本很高，所以这里直接点名。
    """
    if not settings.use_local_retrieval:
        return

    from pathlib import Path

    model_dir = Path(settings.local_embedding_dir)
    missing = [f for f in ("tokenizer.json",) if not (model_dir / f).exists()]
    if not (model_dir / "onnx").is_dir():
        missing.append("onnx/")

    if missing:
        logger.error(
            "本地嵌入模型未就绪：%s 下缺少 %s。"
            "请先执行 python scripts/prepare_model.py 下载模型，"
            "否则文档无法解析、问答无法检索。",
            model_dir, "、".join(missing),
        )
        return

    logger.info("本地嵌入模型就绪：%s", model_dir)
    logger.info("检索索引库：%s", settings.index_db_path)
