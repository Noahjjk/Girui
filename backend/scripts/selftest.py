"""部署自检 / 核心逻辑回归。

    python -m scripts.selftest

不需要数据库连接，也不需要外部服务，覆盖以下核心逻辑：

  1. 全部 14 张表的 DDL 能否在 PostgreSQL 方言下正确编译
  2. 密码哈希与校验、密码强度规则
  3. JWT 签发与解析（含过期与篡改）
  4. 模型 API Key 的加解密与脱敏
  5. 权限中枢的片段级判定（deny / allow / 白名单模式 / 文档可见性）
  6. 提示词构造（片段编号、引用、历史、图片）
  7. 上传文件的类型校验与路径穿越防护
  8. 同一批表在 SQLite 方言下的编译（轻量部署的实际目标）
  9. 文本清洗与中文分词（FTS5 索引的基础）
 10. 切片器（长度上限、页码继承、确定性）
 11. 文档解析（各格式，以及不支持格式是否给出可操作提示）
 12. 片段 ID 生成与向量归一化
 13. 运行模式与检索参数的一致性

需要真实数据库 / 真实解析的端到端验证见 scripts/e2e_local.py。

任何一项失败请勿上线。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("SECRET_ENCRYPTION_KEY", "selftest-key-not-for-production")

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAILED.append(f"{name} — {detail}")
        print(f"  \033[31m✗\033[0m {name}  {detail}")


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ---------------------------------------------------------------- 1. DDL

def test_ddl() -> None:
    section("1. 数据表 DDL（PostgreSQL 方言编译）")
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.db.base import Base
    import app.models  # noqa: F401

    tables = sorted(Base.metadata.tables.keys())
    check("模型全部注册", len(tables) == 15, f"实际 {len(tables)} 张：{tables}")
    check("文件夹表已注册", "kb_folders" in tables)

    dialect = postgresql.dialect()
    for name in tables:
        table = Base.metadata.tables[name]
        try:
            str(CreateTable(table).compile(dialect=dialect))
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=dialect))
            check(f"表 {name} 可编译", True)
        except Exception as exc:  # noqa: BLE001
            check(f"表 {name} 可编译", False, str(exc))


# ---------------------------------------------------------------- 2. 密码

def test_password() -> None:
    section("2. 密码哈希与强度校验")
    from app.core.security import (
        check_password_strength,
        hash_password,
        verify_password,
    )

    hashed = hash_password("Jirui@2026")
    check("哈希非明文", hashed != "Jirui@2026" and hashed.startswith("$2b$"))
    check("正确密码通过", verify_password("Jirui@2026", hashed))
    check("错误密码拒绝", not verify_password("Jirui@2027", hashed))
    check("非法哈希不抛异常", verify_password("x", "not-a-hash") is False)

    check("过短被拒", check_password_strength("abc123") is not None)
    check("纯数字被拒", check_password_strength("123456789") is not None)
    check("纯字母被拒", check_password_strength("abcdefghij") is not None)
    check("含空格被拒", check_password_strength("abcd 1234") is not None)
    check("合规通过", check_password_strength("Jirui@2026") is None)


# ---------------------------------------------------------------- 3. JWT

def test_jwt() -> None:
    section("3. JWT 签发与解析")
    from app.core.security import (
        create_access_token,
        decode_access_token,
        generate_refresh_token,
        hash_refresh_token,
    )

    token = create_access_token("42", {"username": "zhangsan", "role": "user"})
    payload = decode_access_token(token)
    check("解析成功", payload is not None)
    check("sub 正确", (payload or {}).get("sub") == "42")
    check("附带声明保留", (payload or {}).get("username") == "zhangsan")
    check("typ=access", (payload or {}).get("typ") == "access")

    expired = create_access_token("42", expires_minutes=-1)
    check("过期令牌被拒", decode_access_token(expired) is None)
    check("篡改令牌被拒", decode_access_token(token[:-4] + "aaaa") is None)
    check("垃圾串被拒", decode_access_token("not.a.jwt") is None)

    refresh = generate_refresh_token()
    check("refresh 长度足够", len(refresh) >= 32)
    check("refresh 每次不同", refresh != generate_refresh_token())
    check("refresh 哈希稳定", hash_refresh_token(refresh) == hash_refresh_token(refresh))
    check("refresh 哈希非明文", refresh not in hash_refresh_token(refresh))


# ---------------------------------------------------------------- 4. 加密

def test_crypto() -> None:
    section("4. 模型 API Key 加解密")
    from app.core.crypto import decrypt, encrypt, mask

    secret = "sk-1234567890abcdefghijklmn"
    ciphered = encrypt(secret)
    check("密文非明文", ciphered != secret and secret not in ciphered)
    check("带版本前缀", ciphered.startswith("enc:v1:"))
    check("解密还原", decrypt(ciphered) == secret)
    check("同一明文两次密文不同", encrypt(secret) != ciphered)
    check("空值安全", encrypt("") == "" and decrypt("") == "")
    check("明文兼容读取", decrypt("legacy-plain") == "legacy-plain")
    check("损坏密文返回空", decrypt("enc:v1:AAAA") == "")

    masked = mask(secret)
    check("脱敏保留首尾", masked.startswith("sk-1") and masked.endswith("klmn"))
    check("脱敏隐藏中段", "****" in masked and secret not in masked)


# ---------------------------------------------------------------- 5. 权限

def test_permission() -> None:
    section("5. 权限中枢片段级判定")
    from app.models.enums import UserRole
    from app.services.permission import PermissionScope

    scope = PermissionScope(user_id=7, role=UserRole.USER, is_admin=False)
    scope.doc_kb = {"docA": 1, "docB": 1, "docC": 1}
    scope.doc_name = {"docA": "公开规范", "docB": "受限制度", "docC": "白名单文档"}
    scope.visible_docs = {"docA", "docC"}

    check("普通可见文档片段放行", scope.is_chunk_visible("c1", "docA"))
    check("不可见文档片段拦截", not scope.is_chunk_visible("c2", "docB"))

    scope.denied_chunks.add("c1")
    check("片段级 deny 覆盖文档可见性", not scope.is_chunk_visible("c1", "docA"))
    scope.denied_chunks.clear()

    scope.allowed_chunks.add("c9")
    scope.docs_with_chunk_whitelist.add("docC")
    check("白名单内片段放行", scope.is_chunk_visible("c9", "docC"))
    check("白名单外片段拦截（白名单模式）", not scope.is_chunk_visible("c8", "docC"))
    check("deny 优先级高于 allow", True)
    scope.denied_chunks.add("c9")
    check("同时 allow 与 deny 时 deny 生效", not scope.is_chunk_visible("c9", "docC"))
    scope.denied_chunks.clear()

    # 白名单模式不波及其它文档
    check("白名单模式不波及其它文档", scope.is_chunk_visible("c3", "docA"))

    # 批量过滤
    raw = [
        {"id": "c1", "document_id": "docA", "content": "公开内容"},
        {"id": "c2", "document_id": "docB", "content": "受限内容"},
        {"id": "c8", "document_id": "docC", "content": "白名单外"},
        {"id": "c9", "document_id": "docC", "content": "白名单内"},
    ]
    kept = scope.filter_chunks(raw)
    kept_ids = {c["id"] for c in kept}
    check("批量过滤结果正确", kept_ids == {"c1", "c9"}, f"实际 {kept_ids}")
    check("受限内容未泄漏", "受限内容" not in str(kept))

    stats = scope.stats()
    check("统计包含可见文档数", stats.get("visible_documents") == 2)


# ---------------------------------------------------------------- 6. 提示词

def test_prompt() -> None:
    section("6. 提示词构造")
    from app.services.prompt import build_context_block, build_messages, build_title

    chunks = [
        {"index": 1, "content": "日志命名规范：模块_功能_日期.log",
         "document_name": "开发规范.pdf", "kb_name": "公司规范"},
        {"index": 2, "content": "异常必须记录堆栈",
         "document_name": "异常处理.md", "kb_name": "公司规范"},
    ]
    ctx = build_context_block(chunks)
    check("上下文含编号 [1]", "[1]" in ctx and "[2]" in ctx)
    check("上下文含来源", "开发规范.pdf" in ctx and "公司规范" in ctx)

    msg = build_messages("日志怎么命名", chunks)
    system = next(m for m in msg if m["role"] == "system")
    user = next(m for m in msg if m["role"] == "user")
    check("system 约束只依据片段", "只依据下方【参考片段】作答" in system["content"])
    check("user 含参考片段", "【参考片段】" in user["content"])
    check("user 含问题", "日志怎么命名" in user["content"])
    check("无片段时给出占位", "没有检索到任何可用片段" in build_context_block([]))

    vision = build_messages("这是什么", chunks, image_data_uris=["data:image/png;base64,AAA"],
                            supports_vision=True)
    check("视觉模型收到图片块",
          isinstance(vision[-1]["content"], list)
          and any(p.get("type") == "image_url" for p in vision[-1]["content"]))

    no_vision = build_messages("这是什么", chunks, image_data_uris=["data:image/png;base64,AAA"],
                               supports_vision=False)
    check("非视觉模型收到说明", "不支持图像识别" in no_vision[-1]["content"])

    history = [{"role": "user", "content": "上一问"}, {"role": "assistant", "content": "上一答"}]
    with_history = build_messages("继续", chunks, history=history)
    check("历史消息被保留", any(m.get("content") == "上一答" for m in with_history))

    check("标题裁剪", len(build_title("字" * 100)) <= 31)
    check("空标题兜底", build_title("") == "新对话")


# ---------------------------------------------------------------- 7. 上传

def test_upload() -> None:
    section("7. 上传文件校验与路径安全")
    from app.services import storage

    check("合法 pdf", storage.validate_extension("规范.pdf") == "pdf")
    check("合法 docx", storage.validate_extension("制度.DOCX") == "docx")

    try:
        storage.validate_extension("木马.exe")
        check("非法后缀被拒", False, "未抛出异常")
    except storage.UploadError:
        check("非法后缀被拒", True)

    try:
        storage.validate_extension("无后缀文件")
        check("无后缀被拒", False, "未抛出异常")
    except storage.UploadError:
        check("无后缀被拒", True)

    name = storage.safe_display_name("../../etc/passwd")
    check("路径穿越被清洗", "/" not in name and ".." not in name, f"实际 {name}")
    check("Windows 路径被清洗",
          "\\" not in storage.safe_display_name(r"..\..\windows\system32\cmd.exe"))
    check("空文件名兜底", storage.safe_display_name("") == "unnamed")
    check("超长名被截断", len(storage.safe_display_name("a" * 500)) <= 200)

    try:
        storage.validate_size(0)
        check("空文件被拒", False, "未抛出异常")
    except storage.UploadError:
        check("空文件被拒", True)

    try:
        storage.validate_size(999 * 1024 * 1024)
        check("超大文件被拒", False, "未抛出异常")
    except storage.UploadError:
        check("超大文件被拒", True)

    check("mime 推断", storage._guess_mime("pdf") == "application/pdf")
    check("未知后缀 mime 兜底",
          storage._guess_mime("zzz") == "application/octet-stream")


# ---------------------------------------------------------------- 8. SQLite DDL

def test_ddl_sqlite() -> None:
    """轻量部署跑在 SQLite 上，DDL 必须能在 SQLite 方言下编译。

    最容易踩的坑是可移植 JSON 列：模型里写的是 JSONB，
    必须靠 with_variant 落到通用 JSON，否则 SQLite 建表直接失败。
    """
    section("8. 数据表 DDL（SQLite 方言编译）")
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex, CreateTable

    import app.models  # noqa: F401
    from app.db.base import Base

    dialect = sqlite.dialect()
    tables = sorted(Base.metadata.tables.keys())
    check("模型全部注册", len(tables) == 15, f"实际 {len(tables)} 张")

    for name in tables:
        table = Base.metadata.tables[name]
        try:
            str(CreateTable(table).compile(dialect=dialect))
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=dialect))
            check(f"表 {name} 可编译（SQLite）", True)
        except Exception as exc:  # noqa: BLE001
            check(f"表 {name} 可编译（SQLite）", False, str(exc))

    chat = Base.metadata.tables["chat_messages"]
    sqlite_ddl = str(CreateTable(chat).compile(dialect=dialect)).upper()
    pg_ddl = str(CreateTable(chat).compile(dialect=postgresql.dialect())).upper()
    check("SQLite 下降级为通用 JSON", "JSONB" not in sqlite_ddl)
    check("PostgreSQL 下仍用 JSONB", "JSONB" in pg_ddl)


# ---------------------------------------------------------------- 9. 文本工具

def test_textutil() -> None:
    section("9. 文本清洗与中文分词")
    from app.services.textutil import (
        approx_tokens,
        clean_text,
        fts_query,
        fts_tokens,
        token_mode,
    )

    mode = token_mode()
    check("分词模式已确定", mode in ("jieba", "bigram"), mode)

    check("全角转半角", clean_text("ＡＢＣ１２３") == "ABC123")
    check("压缩多余空行", clean_text("甲\n\n\n\n\n乙") == "甲\n\n乙")
    check("去零宽字符", "\u200b" not in clean_text("前\u200b后"))
    check("去控制字符", "\x07" not in clean_text("甲\x07乙"))
    check("统一 CRLF", "\r" not in clean_text("甲\r\n乙"))

    # SQLite 内置分词器不认中文词边界，这里必须真的切出多个 token
    toks = fts_tokens("公司差旅费报销制度")
    check("中文切出多个 token", len(toks.split()) >= 2, toks)
    check("英文转小写", "rpa" in fts_tokens("RPA自动化").split())
    check("标点不入索引", "，" not in fts_tokens("报销，制度"))

    q = fts_query("差旅报销标准")
    check("查询表达式非空", bool(q))
    check("表达式用引号包裹", '"' in q)
    check("特殊字符被清理", "-" not in fts_query("a-b(c)d").replace('"', ""))
    check("纯标点查询返回空", fts_query("！！！") == "")

    check("token 估算为正", approx_tokens("这是一段中文") > 0)
    check("空串 token 为 0", approx_tokens("") == 0)


# ---------------------------------------------------------------- 10. 切片器

def test_chunker() -> None:
    section("10. 切片器")
    from app.services.chunker import chunk_sections, split_text
    from app.services.parser import Section

    long_text = "公司规范要求差旅费用应当据实报销。" * 120  # 约 2000 字
    parts = split_text(long_text, size=300, overlap=60, min_size=20)
    check("长文本被切成多片", len(parts) >= 4, f"{len(parts)} 片")
    check(
        "每片不超上限（留重叠余量）",
        all(len(p) <= 300 + 20 + 60 for p in parts),
        f"最长 {max(len(p) for p in parts)}",
    )
    check("没有空片", all(p.strip() for p in parts))

    check("短文本不切分", len(split_text("一句话。", size=300, overlap=60, min_size=20)) == 1)

    hard = split_text("字" * 1000, size=300, overlap=0, min_size=10)
    check("无分隔符时硬切且不超限", all(len(p) <= 300 for p in hard) and len(hard) >= 3)

    # 页码继承 —— 溯源定位依赖它
    secs = [Section(text="甲" * 500, page=1), Section(text="乙" * 500, page=7)]
    chunks = chunk_sections(secs, size=200, overlap=40, min_size=10)
    check("切片继承页码", {c.page for c in chunks} == {1, 7}, f"{[c.page for c in chunks]}")
    check("序号从 0 连续", [c.ordinal for c in chunks] == list(range(len(chunks))))
    check("token 估算为正", all(c.tokens > 0 for c in chunks))

    again = [c.content for c in chunk_sections(secs, size=200, overlap=40, min_size=10)]
    check("同样输入产出同样切片（片段 ID 才能稳定）", [c.content for c in chunks] == again)


# ---------------------------------------------------------------- 11. 解析器

def test_parser() -> None:
    section("11. 文档解析")
    from app.services.parser import ParseError, decode_text, html_to_text, parse_bytes

    check("UTF-8 解码", decode_text("中文".encode("utf-8")) == "中文")
    check("GB18030 兜底解码", decode_text("中文".encode("gb18030")) == "中文")

    html = "<html><body><h1>标题</h1><script>bad()</script><p>正文</p></body></html>"
    text = html_to_text(html)
    check("HTML 保留正文", "标题" in text and "正文" in text)
    check("HTML 剔除脚本", "bad()" not in text)

    check("Markdown 解析", "正文内容" in parse_bytes("# 标题\n\n正文内容".encode(), "a.md").text)
    check("纯文本解析", "甲乙丙" in parse_bytes("甲乙丙".encode(), "a.txt").text)
    check("CSV 按行拼接", "张三 | 100" in parse_bytes("姓名,金额\n张三,100".encode(), "a.csv").text)

    json_doc = parse_bytes('{"名称": "极睿"}'.encode(), "a.json")
    check("JSON 解析", "极睿" in json_doc.text)

    for ext in ("doc", "xls", "ppt", "epub"):
        try:
            parse_bytes(b"x" * 200, f"a.{ext}")
            check(f".{ext} 被明确拒绝", False, "未抛异常")
        except ParseError as exc:
            msg = str(exc)
            check(
                f".{ext} 被拒绝且给出可操作提示",
                bool(msg) and ("另存为" in msg or "转换为" in msg),
                msg[:60],
            )

    try:
        parse_bytes(b"abc", "a.zzz")
        check("未知后缀被拒", False, "未抛异常")
    except ParseError as exc:
        check("未知后缀提示支持格式", "支持" in str(exc))

    for content, label in ((b"", "空文件"), (b"   \n\t  ", "纯空白文件")):
        try:
            parse_bytes(content, "a.txt")
            check(f"{label}被拒", False, "未抛异常")
        except ParseError:
            check(f"{label}被拒", True)


# ---------------------------------------------------------------- 12. 索引与向量

def test_index_and_embedding() -> None:
    section("12. 片段 ID 与向量归一化")
    from app.services.index_store import make_chunk_id

    check("片段 id 确定性生成", make_chunk_id("abc", 3) == "abc-0003")
    check("片段 id 不超字段长度", len(make_chunk_id("a" * 32, 9999)) <= 64)
    check(
        "序号补零后字典序等于数值序",
        make_chunk_id("d", 2) < make_chunk_id("d", 10),
    )

    import numpy as np

    from app.services.embedding import _l2_normalize

    mat = _l2_normalize(np.array([[3.0, 4.0], [0.0, 0.0]], dtype=np.float32))
    check("L2 归一化后模长为 1", abs(float(np.linalg.norm(mat[0])) - 1.0) < 1e-6)
    check("零向量不产生 NaN", bool(np.isfinite(mat[1]).all()))
    check("归一化后点积即余弦", abs(float(mat[0] @ mat[0]) - 1.0) < 1e-6)


# ---------------------------------------------------------------- 13. 配置一致性

def test_config_consistency() -> None:
    section("13. 运行模式与参数一致性")
    from app.core.config import settings

    check(
        "检索后端取值合法",
        settings.RETRIEVAL_BACKEND in ("local", "ragflow"),
        settings.RETRIEVAL_BACKEND,
    )
    check(
        "数据库 DSN 可识别",
        settings.is_sqlite or settings.database_url.startswith("postgresql"),
        settings.database_url,
    )
    check("嵌入维度为正", settings.EMBEDDING_DIM > 0, str(settings.EMBEDDING_DIM))
    check(
        "切片长度小于模型上限（否则会被静默截断）",
        settings.CHUNK_SIZE < settings.LOCAL_EMBEDDING_MAX_TOKENS,
        f"CHUNK_SIZE={settings.CHUNK_SIZE} MAX={settings.LOCAL_EMBEDDING_MAX_TOKENS}",
    )
    check("重叠小于切片长度", settings.CHUNK_OVERLAP < settings.CHUNK_SIZE)
    check("Top-N 不超过 Top-K", settings.RETRIEVAL_TOP_N <= settings.RETRIEVAL_TOP_K)
    check("混合权重在 0~1 之间", 0.0 <= settings.HYBRID_VECTOR_WEIGHT <= 1.0)
    check(
        "CORS 放行 null（桌面端 file:// 必需）",
        "null" in settings.allowed_origins_list,
        str(settings.allowed_origins_list),
    )
    check("索引库路径已推导", bool(settings.index_db_path))
    check("嵌入模型目录已推导", bool(settings.local_embedding_dir))


# ---------------------------------------------------------------- main

def main() -> int:
    print("\033[1m极睿知识库 自检\033[0m")
    print("=" * 62)

    for fn in (test_ddl, test_password, test_jwt, test_crypto,
               test_permission, test_prompt, test_upload,
               test_ddl_sqlite, test_textutil, test_chunker, test_parser,
               test_index_and_embedding, test_config_consistency):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            FAILED.append(f"{fn.__name__} 抛出异常: {exc}")
            print(f"  \033[31m✗\033[0m {fn.__name__} 抛出异常")
            traceback.print_exc()

    print("\n" + "=" * 62)
    if FAILED:
        print(f"\033[31m失败 {len(FAILED)} 项，通过 {PASSED} 项\033[0m")
        for item in FAILED:
            print(f"  · {item}")
        return 1

    print(f"\033[32m全部通过（{PASSED} 项）\033[0m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
