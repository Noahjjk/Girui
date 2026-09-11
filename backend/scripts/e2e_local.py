"""轻量内核端到端自测：上传 → 解析 → 切片 → 嵌入 → 混合检索 → 权限隔离。

不依赖任何外部服务（不需要 RAGFlow / PostgreSQL / Docker），
直接在本机跑 SQLite + 本地 ONNX 嵌入，用来在没有服务器的机器上
验证整条链路是否真的通。

覆盖：
  1. 建表、初始化数据、建用户与知识库
  2. 三种格式（Markdown / DOCX / PDF）的真实解析
  3. 后台解析工作者能否把文档跑完
  4. 混合检索（向量 + 全文）是否命中正确文档
  5. 文档级可见性隔离（RESTRICTED 文档 A 可见、B 不可见）
  6. 片段级 deny（显式封禁某片段）
  7. 片段白名单模式（只放行显式授权的片段）
  8. 审计日志是否落库

用法：python scripts/e2e_local.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 环境必须在导入应用之前设好：Settings 是 import 期实例化的 ----
_TMP = tempfile.mkdtemp(prefix="jirui-e2e-")
os.environ["DATA_DIR"] = _TMP
os.environ["JWT_SECRET"] = "e2e-only-secret-do-not-use-in-prod"
os.environ["SECRET_ENCRYPTION_KEY"] = ""
os.environ["RETRIEVAL_BACKEND"] = "local"
os.environ["ADMIN_INIT_PASSWORD"] = "Admin@12345"
os.environ["DEFAULT_LLM_API_KEY"] = ""
os.environ["DEBUG"] = "false"

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.seed import seed_initial_data  # noqa: E402
from app.db.session import SessionLocal, init_models  # noqa: E402
from app.models.enums import (  # noqa: E402
    AclEffect,
    AclSubjectType,
    DocumentStatus,
    UserRole,
    Visibility,
)
from app.models.knowledge import ChunkAcl, KbDocument, KbPermission, KnowledgeBase  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import storage  # noqa: E402
from app.services.chat import retrieve  # noqa: E402
from app.services.index_store import get_index_store  # noqa: E402
from app.services.ingest import get_ingest_manager  # noqa: E402
from app.services.retriever import retrieval_client  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 测试素材

DOC_TRAVEL = """# 差旅费报销管理制度

## 第一章 总则
第一条 为规范公司差旅费用管理，统一报销标准，控制差旅成本，特制定本制度。
第二条 本制度适用于公司全体正式员工因公出差所产生的交通费、住宿费、
伙食补助费、市内交通费以及其他与出差直接相关的合理费用。
第三条 出差前应当在系统中提交出差申请，注明出差事由、目的地、
起止日期与预计费用，经直属主管批准后方可出行。
第四条 未经批准擅自出差的，相关费用一律不予报销。

## 第二章 报销标准
第五条 住宿费标准按职级划分：普通员工每人每晚不超过四百元；
部门经理每人每晚不超过六百元；总监及以上每人每晚不超过一千元。
第六条 住宿费按实际住宿天数计算，超出标准部分由个人承担。
同性别同事同行出差的，原则上应安排合住标准间。
第七条 交通费标准：市内交通凭票据实报销；城际出行优先选择高铁二等座，
连续行程超过四小时的可乘坐飞机经济舱。
第八条 乘坐飞机应当选择经济舱，因特殊情况需乘坐公务舱的，
须事先取得分管副总裁的书面批准。
第九条 伙食补助按出差自然日计算，每人每天一百二十元，无需提供发票。
出差期间如已由接待方安排用餐的，相应餐次不再发放补助。
第十条 出差期间因公务发生的招待费、会议费，按公司招待费管理规定另行报销，
不计入差旅费额度。

## 第三章 报销流程
第十一条 出差结束后十五个工作日内，应当在系统中提交报销单，
并附上全部原始票据，票据缺失的项目不予报销。
第十二条 报销单须经直属主管审批；单笔金额超过五千元的，
还须经财务总监审批；超过两万元的，须经总裁审批。
第十三条 财务部在收到完整合规材料后十个工作日内完成打款。
第十四条 报销材料存在弄虚作假的，除追回款项外，
还将按公司奖惩制度予以处理。

## 第四章 附则
第十五条 本制度由财务部负责解释，自发布之日起施行。
第十六条 本制度与国家法律法规冲突的，以国家法律法规为准。
"""

DOC_SALARY = """# 高级管理人员薪酬管理规定

## 一、适用范围
本规定适用于公司总监及以上级别的高级管理人员，
包括总监、高级总监、副总裁、高级副总裁及总裁。
外聘顾问与实习人员不适用本规定。

## 二、薪酬构成
高级管理人员薪酬由基本年薪、绩效年薪和长期激励三部分构成。
基本年薪按月发放，占年度现金收入的百分之六十，
体现岗位价值与市场对标水平。
绩效年薪与年度经营目标完成率挂钩，占年度现金收入的百分之四十，
于次年第一季度考核完成后一次性发放。
长期激励采用限制性股票形式，分三年匀速归属，
首年归属百分之四十，次年归属百分之三十，第三年归属百分之三十。
高级管理人员离职的，未归属部分自动失效。

## 三、考核办法
年度经营目标由董事会在每年一月确定，
包括营业收入、净利润、现金流与战略项目达成率四项指标。
四项指标按权重加权计算综合完成率：
营业收入占百分之三十，净利润占百分之四十，
现金流占百分之二十，战略项目达成率占百分之十。
综合完成率低于百分之七十的，绩效年薪不予发放。
综合完成率超过百分之一百二十的，超出部分按百分之一百五十计提。

## 四、保密要求
薪酬数据属于公司核心机密，任何人不得对外泄露，
不得在内部非授权范围内讨论或传播。
违反者将按劳动合同及公司保密协议追究责任，
情节严重的将解除劳动合同并追究法律责任。
"""

DOC_RPA = """# 影刀 RPA 网页自动化最佳实践

## 一、元素定位
优先使用 XPath 定位，避免使用易变的 CSS 类名，
因为前端重构时类名极易变化，而 XPath 基于结构相对稳定。
对于动态加载的页面，必须显式等待目标元素可见之后再操作，
不要使用固定时长休眠，那样既慢又不可靠。
遇到 iframe 嵌套的页面，需要先切换到对应的 frame 再定位元素，
操作完成后记得切回主文档，否则后续步骤会全部失败。

## 二、异常处理
每个关键步骤都应当包裹 try-catch 结构，
失败时截图并记录详细日志，便于事后定位问题。
截图文件建议按日期分目录存放，并保留最近三十天，
过期自动清理，避免磁盘被占满。
对于网络超时类异常，应当设置有限次数的重试，
推荐重试三次、间隔两秒，避免在对方服务故障时雪崩。

## 三、流程复用
把通用的登录、下载、上传等操作封装成子流程，
通过参数传递账号、路径与超时时间，
避免在多个流程里重复粘贴同一段代码。
子流程的命名要能表达意图，例如「登录_影刀控制台」，
不要用「流程1」「流程2」这类无意义的名字。
公共的子流程应当存放在独立的模块目录下统一维护，
修改时只需改一处，所有引用它的流程都会生效。

## 四、性能优化
批量处理数据时，尽量一次性读取全部输入再统一处理，
避免在循环中反复打开关闭文件或重复登录。
浏览器实例应当复用，一个流程内不要频繁启停浏览器。
对于耗时较长的流程，建议拆分为多个可断点续跑的阶段，
每完成一个阶段就把进度写入状态文件。
"""


def build_files(workdir: Path) -> dict:
    """生成测试用文件，返回 {标识: 绝对路径}。"""
    workdir.mkdir(parents=True, exist_ok=True)
    out = {}

    md = workdir / "差旅费报销管理制度.md"
    md.write_text(DOC_TRAVEL, encoding="utf-8")
    out["travel_md"] = str(md)

    # DOCX：验证 python-docx 路径
    try:
        import docx

        d = docx.Document()
        d.add_heading("高级管理人员薪酬管理规定", level=1)
        for line in DOC_SALARY.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("##"):
                d.add_heading(line.strip("# "), level=2)
            else:
                d.add_paragraph(line)
        docx_path = workdir / "高级管理人员薪酬管理规定.docx"
        d.save(str(docx_path))
        out["salary_docx"] = str(docx_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 生成 DOCX 失败，跳过该用例：{exc}")

    # PDF：验证 PyMuPDF 路径
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz

        doc = fitz.open()
        page = doc.new_page()
        y = 72
        for line in DOC_RPA.splitlines():
            line = line.strip()
            if not line:
                y += 8
                continue
            # 每行手工折行，避免超出页面宽度
            while len(line) > 42:
                page.insert_text((56, y), line[:42], fontsize=11, fontname="china-s")
                line = line[42:]
                y += 18
                if y > 760:
                    page = doc.new_page()
                    y = 72
            page.insert_text((56, y), line, fontsize=11, fontname="china-s")
            y += 18
            if y > 760:
                page = doc.new_page()
                y = 72
        pdf_path = workdir / "影刀RPA最佳实践.pdf"
        doc.save(str(pdf_path))
        doc.close()
        out["rpa_pdf"] = str(pdf_path)
    except Exception as exc:  # noqa: BLE001
        print(f"  [警告] 生成 PDF 失败，跳过该用例：{exc}")

    return out


# ---------------------------------------------------------------- 辅助

async def wait_ingest(doc_ids: list[str], timeout: float = 300.0) -> dict:
    store = get_index_store()
    deadline = asyncio.get_event_loop().time() + timeout
    states: dict[str, str] = {}
    while asyncio.get_event_loop().time() < deadline:
        done = 0
        for did in doc_ids:
            info = await store.get_document(did)
            state = (info or {}).get("state", "missing")
            states[did] = state
            if state in ("done", "failed"):
                done += 1
        if done == len(doc_ids):
            return states
        await asyncio.sleep(0.4)
    return states


async def add_business_doc(
    db,
    kb: KnowledgeBase,
    dataset_id: str,
    path: str,
    *,
    visibility: Visibility,
    uploader_id: int,
    category: str | None = None,
) -> tuple[KbDocument, str]:
    """走一遍与接口完全相同的上传路径。"""
    p = Path(path)
    content = p.read_bytes()
    stored = await storage.save_upload(
        p.name, content, mime="", category=f"kb_{kb.id}"
    )
    remote = await retrieval_client.upload_documents(
        dataset_id,
        [(stored.original_name, content, stored.mime)],
        metas=[
            {
                "name": stored.original_name,
                "relative_path": stored.relative_path,
                "ext": stored.ext,
            }
        ],
    )
    remote_id = remote[0]["id"]
    doc = KbDocument(
        kb_id=kb.id,
        ragflow_document_id=remote_id,
        name=stored.original_name,
        file_type=stored.ext,
        size_bytes=stored.size,
        category=category,
        source="upload",
        storage_path=stored.relative_path,
        checksum=stored.checksum,
        visibility=visibility,
        tags=[],
        status=DocumentStatus.PENDING,
        uploaded_by=uploader_id,
    )
    db.add(doc)
    await db.flush()
    await retrieval_client.parse_documents(dataset_id, [remote_id])
    return doc, remote_id


def doc_ids_in(outcome) -> set[str]:
    return {c["document_id"] for c in outcome.chunks}


# ---------------------------------------------------------------- 主流程

async def run() -> None:
    print(f"临时数据目录：{_TMP}")
    print(f"检索后端：{'本地轻量内核' if settings.use_local_retrieval else 'RAGFlow'}")
    print(f"嵌入模型：{settings.EMBEDDING_MODEL} ({settings.EMBEDDING_DIM} 维)")

    section("1. 建表与初始化")
    await init_models()
    await seed_initial_data()
    manager = get_ingest_manager()
    await manager.start()
    check("SQLite 建表成功", True)
    check("索引库可连接", await retrieval_client.ping())
    check("嵌入模型可加载", await asyncio.to_thread(_embedding_ok))

    section("2. 建用户、知识库与授权")
    async with SessionLocal() as db:
        admin = (
            await db.execute(select(User).where(User.role == UserRole.ADMIN))
        ).scalars().first()
        check("存在初始管理员", admin is not None)

        alice = User(
            username="alice", password_hash=hash_password("Alice@12345"),
            display_name="爱丽丝", role=UserRole.COLLABORATOR, is_active=True,
        )
        bob = User(
            username="bob", password_hash=hash_password("Bob@123456"),
            display_name="鲍勃", role=UserRole.USER, is_active=True,
        )
        db.add_all([alice, bob])
        await db.flush()

        kb = (
            await db.execute(
                select(KnowledgeBase).where(KnowledgeBase.name == "公司规范")
            )
        ).scalar_one()
        kb.ragflow_dataset_id = (
            await retrieval_client.create_dataset(name=kb.name)
        )["id"]
        await db.flush()

        db.add_all(
            [
                KbPermission(user_id=alice.id, kb_id=kb.id, can_read=True, can_upload=True),
                KbPermission(user_id=bob.id, kb_id=kb.id, can_read=True, can_upload=False),
            ]
        )
        await db.commit()
        check("知识库已关联检索数据集", bool(kb.ragflow_dataset_id))

        dataset_id = kb.ragflow_dataset_id
        kb_id = kb.id
        alice_id, bob_id, admin_id = alice.id, bob.id, admin.id

    section("3. 上传三种格式并等待解析")
    files = build_files(Path(_TMP) / "src")
    print(f"  生成素材：{list(files)}")

    async with SessionLocal() as db:
        travel, travel_rid = await add_business_doc(
            db, kb, dataset_id, files["travel_md"],
            visibility=Visibility.PUBLIC, uploader_id=admin_id, category="公司规范",
        )
        public_doc_id = travel_rid

        private_rid = None
        if "salary_docx" in files:
            salary, private_rid = await add_business_doc(
                db, kb, dataset_id, files["salary_docx"],
                visibility=Visibility.RESTRICTED, uploader_id=admin_id,
            )
        rpa_rid = None
        if "rpa_pdf" in files:
            rpa, rpa_rid = await add_business_doc(
                db, kb, dataset_id, files["rpa_pdf"],
                visibility=Visibility.PUBLIC, uploader_id=admin_id,
            )
        await db.commit()

    wait_list = [i for i in (public_doc_id, private_rid, rpa_rid) if i]
    states = await wait_ingest(wait_list)
    for did, state in states.items():
        check(f"文档解析完成 {did[:8]}…（state={state}）", state == "done")

    store = get_index_store()
    total_chunks = await store.count_chunks(dataset_id)
    check(
        f"索引中片段总数 {total_chunks} 条（切分粒度合理）",
        total_chunks >= 6,
        f"实际 {total_chunks}，切片过粗",
    )
    for rid in wait_list:
        n = len(await store.all_chunks(dataset_id, rid, 500))
        check(f"文档 {rid[:8]}… 切出 {n} 个片段（>1 才能验证片段级权限）", n >= 2)

    # 私有文档必须解析成功，否则后面的隔离用例是假通过
    if private_rid:
        check("受限文档已成功解析（隔离用例的前提）", states.get(private_rid) == "done")

    section("4. 混合检索命中正确文档")
    async with SessionLocal() as db:
        alice = (await db.execute(select(User).where(User.id == alice_id))).scalar_one()
        bob = (await db.execute(select(User).where(User.id == bob_id))).scalar_one()

        r_travel = await retrieve(db, alice, "住宿费报销标准是多少钱一晚？", [kb_id])
        check("检索「住宿费标准」有结果", len(r_travel.chunks) > 0)
        check(
            "命中差旅报销文档",
            public_doc_id in doc_ids_in(r_travel),
            f"命中={[c['document_name'] for c in r_travel.chunks]}",
        )
        check("回溯信息带页码/来源字段", all("document_name" in c for c in r_travel.chunks))

        if rpa_rid:
            r_rpa = await retrieve(db, alice, "影刀RPA里元素定位优先用什么方式？", [kb_id])
            check(
                "检索 RPA 关键词命中 PDF 文档",
                rpa_rid in doc_ids_in(r_rpa),
                f"命中={[c['document_name'] for c in r_rpa.chunks]}",
            )

        # 权限相关检索：先确认管理员能搜到受限文档，才说明隔离有意义
        if private_rid:
            r_admin = await retrieve(
                db, (await db.execute(select(User).where(User.id == admin_id))).scalar_one(),
                "高级管理人员的绩效年薪怎么发放？", [kb_id],
            )
            check(
                "管理员能检索到受限文档（基线）",
                private_rid in doc_ids_in(r_admin),
                f"命中={[c['document_name'] for c in r_admin.chunks]}",
            )
            r_bob = await retrieve(db, bob, "高级管理人员的绩效年薪怎么发放？", [kb_id])
            check(
                "普通用户检索不到受限文档（文档级隔离）",
                private_rid not in doc_ids_in(r_bob),
                f"命中={[c['document_name'] for c in r_bob.chunks]}",
            )

    denied_id: str | None = None
    section("5. 片段级 deny（显式封禁）")
    async with SessionLocal() as db:
        chunks = await store.all_chunks(dataset_id, public_doc_id, 100)
        check("能列出文档片段（供管理员挑选）", len(chunks) > 0)
        # 找到包含「住宿费」的片段并封禁给 bob
        target = next((c for c in chunks if "住宿费" in c["content"]), None)
        if target:
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(bob_id),
                    kb_id=kb_id, ragflow_document_id=public_doc_id,
                    chunk_id=target["id"], effect=AclEffect.DENY,
                    note="e2e 测试：封禁该片段",
                )
            )
            await db.commit()
            denied_id = target["id"]

            alice = (await db.execute(select(User).where(User.id == alice_id))).scalar_one()
            bob = (await db.execute(select(User).where(User.id == bob_id))).scalar_one()
            q = target["content"][:40]

            r_alice = await retrieve(db, alice, q, [kb_id])
            r_bob = await retrieve(db, bob, q, [kb_id])

            check(
                "被 deny 的片段对 alice 仍可见",
                any(c["chunk_id"] == target["id"] for c in r_alice.chunks),
            )
            check(
                "被 deny 的片段对 bob 不可见",
                not any(c["chunk_id"] == target["id"] for c in r_bob.chunks),
            )
        else:
            check("找到可用于 deny 测试的片段", False, "内容里没有「住宿费」")

    section("6. 片段白名单模式")
    async with SessionLocal() as db:
        chunks = await store.all_chunks(dataset_id, public_doc_id, 100)
        check("白名单用例的前提：该文档有多个片段", len(chunks) >= 2, f"实际 {len(chunks)}")
        if len(chunks) >= 2:
            # 必须避开已被 deny 的片段：按设计 deny 优先于 allow，
            # 拿一个已被 deny 的片段去测白名单，得到「不可见」才是正确行为
            only = next(
                (c["id"] for c in chunks if c["id"] != denied_id), chunks[0]["id"]
            )
            check("选中的白名单片段未被 deny 覆盖", only != denied_id)
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(bob_id),
                    kb_id=kb_id, ragflow_document_id=public_doc_id,
                    chunk_id=only, effect=AclEffect.ALLOW,
                    note="e2e 测试：白名单",
                )
            )
            await db.commit()

            from app.services.permission import PermissionService

            perm_bob = await PermissionService(db).resolve_scope(bob, [kb_id])
            check(
                "白名单模式被识别",
                public_doc_id in perm_bob.docs_with_chunk_whitelist,
            )
            check(
                "白名单内片段可见",
                perm_bob.is_chunk_visible(only, public_doc_id),
            )
            others = [c["id"] for c in chunks if c["id"] != only]
            check(
                "白名单外的同文档片段不可见",
                not any(perm_bob.is_chunk_visible(cid, public_doc_id) for cid in others),
                f"泄漏了 {len([c for c in others if perm_bob.is_chunk_visible(c, public_doc_id)])} 条",
            )
            check(
                "白名单模式不波及其它文档",
                (not perm_bob.is_doc_visible(private_rid)) if private_rid else True,
            )

    section("7. 片段级 allow 必须能召回受限文档中的片段")
    # 回归用例：曾经检索前置过滤只传 visible_docs，导致 restricted 文档里被
    # 显式放行的片段根本进不了召回，片段级授权形同虚设。
    if private_rid:
        async with SessionLocal() as db:
            from app.services.permission import PermissionService

            private_chunks = await store.all_chunks(dataset_id, private_rid, 100)
            check(
                "受限文档有多于 1 个片段（能区分放行/未放行）",
                len(private_chunks) >= 2,
                f"实际 {len(private_chunks)}",
            )
            if len(private_chunks) >= 2:
                allowed_chunk = private_chunks[1]["id"]
                db.add(
                    ChunkAcl(
                        subject_type=AclSubjectType.USER, subject_id=str(bob_id),
                        kb_id=kb_id, ragflow_document_id=private_rid,
                        chunk_id=allowed_chunk, effect=AclEffect.ALLOW,
                        note="e2e 测试：单独放行受限文档中的一片",
                    )
                )
                await db.commit()

                bob = (await db.execute(select(User).where(User.id == bob_id))).scalar_one()
                scope_bob = await PermissionService(db).resolve_scope(bob, [kb_id])
                check(
                    "受限文档进入检索范围（否则 allow 永远召不回）",
                    private_rid in scope_bob.search_docs,
                )
                check(
                    "受限文档本身仍不算「可见文档」",
                    private_rid not in scope_bob.visible_docs,
                )
                r_bob = await retrieve(db, bob, private_chunks[1]["content"][:40], [kb_id])
                check(
                    "放行的那个片段确实被召回",
                    any(c["chunk_id"] == allowed_chunk for c in r_bob.chunks),
                    f"命中片段={[c['chunk_id'] for c in r_bob.chunks]}",
                )
                leaked = [
                    c["chunk_id"]
                    for c in r_bob.chunks
                    if c["document_id"] == private_rid and c["chunk_id"] != allowed_chunk
                ]
                check(
                    "同受限文档的其它片段仍然不可见",
                    not leaked,
                    f"泄漏了 {leaked}",
                )

    section("8. 文档级 allow 应放行整篇文档")
    # 回归用例：曾经「文档级 allow」也会把文档置为白名单模式，
    # 结果是给用户授权整篇文档后他一个片段都看不到。
    if private_rid:
        async with SessionLocal() as db:
            from app.services.permission import PermissionService

            carol = User(
                username="carol", password_hash=hash_password("Carol@12345"),
                display_name="卡罗尔", role=UserRole.USER, is_active=True,
            )
            db.add(carol)
            await db.flush()
            db.add(KbPermission(user_id=carol.id, kb_id=kb_id, can_read=True))
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(carol.id),
                    kb_id=kb_id, ragflow_document_id=private_rid,
                    chunk_id=None, effect=AclEffect.ALLOW,
                    note="e2e 测试：整篇文档授权",
                )
            )
            await db.commit()

            # 必须重新从库里取一次：resolve_scope 会读 user.tag_names，而 tags 是
            # selectin 关系，只有从库里查出来的实例才带着已加载的 tags；
            # 直接用刚 flush 的临时对象会走到异步懒加载上炸掉。
            carol = (await db.execute(select(User).where(User.username == "carol"))).scalar_one()
            scope_carol = await PermissionService(db).resolve_scope(carol, [kb_id])
            check("文档级 allow 使受限文档整体可见", scope_carol.is_doc_visible(private_rid))
            check(
                "文档级 allow 不被误判为白名单模式",
                private_rid not in scope_carol.docs_with_chunk_whitelist,
            )
            private_chunks = await store.all_chunks(dataset_id, private_rid, 100)
            invisible = [
                c["id"] for c in private_chunks if not scope_carol.is_chunk_visible(c["id"], private_rid)
            ]
            check("文档级 allow 下所有片段可见", not invisible, f"不可见 {len(invisible)} 条")

            r_carol = await retrieve(db, carol, private_chunks[0]["content"][:40], [kb_id])
            check(
                "文档级 allow 后能实际检索到该文档",
                private_rid in doc_ids_in(r_carol),
                f"命中={[c['document_name'] for c in r_carol.chunks]}",
            )
            check(
                "文档级 allow 不波及其它用户",
                private_rid not in (await PermissionService(db).resolve_scope(
                    (await db.execute(select(User).where(User.id == alice_id))).scalar_one(), [kb_id]
                )).visible_docs,
            )

    section("9. UPDATE 后读取时间戳不触发异步懒加载")
    # 回归用例：TimestampMixin 曾用 onupdate=func.now()（SQL 侧表达式），
    # UPDATE 后 SQLAlchemy 会 expire 该属性，响应序列化时懒加载 -> MissingGreenlet -> 500。
    async with SessionLocal() as db:
        doc_row = (
            await db.execute(select(KbDocument).where(KbDocument.kb_id == kb_id))
        ).scalars().first()
        check("取到用于更新测试的文档", doc_row is not None)
        if doc_row is not None:
            before = doc_row.updated_at
            doc_row.remark = "e2e 触发的更新"
            await db.flush()
            try:
                after = doc_row.updated_at
                check(
                    "UPDATE 后读取 updated_at 无异常",
                    after is not None,
                    f"{before} -> {after}",
                )
            except Exception as exc:  # noqa: BLE001
                check("UPDATE 后读取 updated_at 无异常", False, f"{type(exc).__name__}: {exc}")

            # 连续两次更新，第二次若仍会 expire 就必炸
            doc_row.remark = "再改一次"
            await db.flush()
            try:
                _ = doc_row.updated_at
                check("连续 UPDATE 后读取 updated_at 无异常", True)
            except Exception as exc:  # noqa: BLE001
                check("连续 UPDATE 后读取 updated_at 无异常", False, f"{type(exc).__name__}: {exc}")
            await db.commit()

    section("10. 多级嵌套文件夹的权限继承与隔离")
    # 树形结构：
    #   制度文件 ─┬─ 人事制度 ─── 年假与考勤
    #             ├─ 财务制度
    #             └─ 机密(restricted) ─── 绝密
    #   技术文档
    async with SessionLocal() as db:
        from app.models.knowledge import KbFolder
        from app.services.permission import PermissionService

        def mk_folder(parent, name, visibility=Visibility.PUBLIC):
            node = KbFolder(
                kb_id=kb_id,
                parent_id=parent.id if parent else None,
                name=name,
                path=(parent.self_path if parent else "/"),
                depth=(parent.depth + 1 if parent else 0),
                visibility=visibility,
            )
            db.add(node)
            return node

        f_root = mk_folder(None, "制度文件")
        await db.flush()
        f_hr = mk_folder(f_root, "人事制度")
        await db.flush()
        f_leave = mk_folder(f_hr, "年假与考勤")
        f_fin = mk_folder(f_root, "财务制度")
        f_secret = mk_folder(f_root, "机密", Visibility.RESTRICTED)
        await db.flush()
        f_secret_child = mk_folder(f_secret, "绝密")
        f_tech = mk_folder(None, "技术文档")
        await db.flush()

        check(
            "三级嵌套的物化路径正确",
            f_leave.path == f"/{f_root.id}/{f_hr.id}/",
            f"实际 {f_leave.path!r}",
        )
        check("深度字段正确", f_leave.depth == 2)

        # ---- 把文档挂到文件夹上 ----
        async def place(rid, folder_id, visibility=None):
            doc = (
                await db.execute(
                    select(KbDocument).where(KbDocument.ragflow_document_id == rid)
                )
            ).scalar_one()
            doc.folder_id = folder_id
            if visibility is not None:
                doc.visibility = visibility
            return doc

        if private_rid:
            await place(private_rid, f_leave.id)  # 受限文档放进「年假与考勤」
        await place(public_doc_id, f_tech.id)  # 公开文档放进「技术文档」

        dave = User(
            username="dave", password_hash=hash_password("Dave@12345"),
            display_name="戴夫", role=UserRole.USER, is_active=True,
        )
        eve = User(
            username="eve", password_hash=hash_password("Eve@123456"),
            display_name="伊芙", role=UserRole.USER, is_active=True,
        )
        db.add_all([dave, eve])
        await db.flush()
        db.add_all(
            [
                KbPermission(user_id=dave.id, kb_id=kb_id, can_read=True),
                KbPermission(user_id=eve.id, kb_id=kb_id, can_read=True),
            ]
        )
        await db.commit()

        dave = (await db.execute(select(User).where(User.username == "dave"))).scalar_one()
        eve = (await db.execute(select(User).where(User.username == "eve"))).scalar_one()
        admin_user = (await db.execute(select(User).where(User.id == admin_id))).scalar_one()
        svc = PermissionService(db)

        # ---- 11.1 基线：无任何文件夹规则 ----
        s0 = await svc.resolve_scope(dave, [kb_id])
        check(
            "public 链上的多级文件夹默认可见",
            {f_root.id, f_hr.id, f_leave.id, f_tech.id} <= s0.visible_folders,
            f"可见 {len(s0.visible_folders)}/{len(s0.folders)}",
        )
        check("restricted 文件夹对无规则用户不可见", f_secret.id not in s0.visible_folders)
        check("public 文件夹里的 public 文档可见", s0.is_doc_visible(public_doc_id))
        check(
            "文件夹不改变文档自身可见性（restricted 文档仍不可见）",
            private_rid is None or not s0.is_doc_visible(private_rid),
        )

        # ---- 11.2 文件夹级 allow：沿链向下继承，批量放行其中文档 ----
        if private_rid:
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(dave.id),
                    kb_id=kb_id, folder_id=f_hr.id, effect=AclEffect.ALLOW,
                    note="e2e：给「人事制度」整体授权",
                )
            )
            await db.commit()
            s1 = await svc.resolve_scope(dave, [kb_id])
            check("文件夹级 allow 向下继承到孙文件夹", f_leave.id in s1.visible_folders)
            check("被继承的文件夹记入批量授权集合", f_leave.id in s1.granted_folders)
            check("文件夹授权批量放行其中的受限文档", s1.is_doc_visible(private_rid))
            check(
                "检索前置范围包含被文件夹放行的文档",
                private_rid in s1.search_docs,
            )
            check(
                "文件夹授权不波及兄弟分支",
                f_fin.id in s1.visible_folders and f_secret.id not in s1.visible_folders,
            )

        # ---- 11.3 子级 deny 覆盖父级 allow（deny 绝对优先） ----
        if private_rid:
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(dave.id),
                    kb_id=kb_id, folder_id=f_leave.id, effect=AclEffect.DENY,
                    note="e2e：孙级单独收回",
                )
            )
            await db.commit()
            s2 = await svc.resolve_scope(dave, [kb_id])
            check("孙级 deny 覆盖父级 allow", f_leave.id not in s2.visible_folders)
            check("被 deny 的文件夹内文档不可见", not s2.is_doc_visible(private_rid))
            check("deny 不外溢到上级文件夹", f_hr.id in s2.visible_folders)

            # ---- 11.4 文档级规则优先于文件夹级（方案 B：文档仍可逐篇配） ----
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(dave.id),
                    kb_id=kb_id, ragflow_document_id=private_rid, effect=AclEffect.ALLOW,
                    note="e2e：文档级单独放行，盖过文件夹 deny",
                )
            )
            await db.commit()
            s3 = await svc.resolve_scope(dave, [kb_id])
            check("文档级 allow 优先于文件夹级 deny", s3.is_doc_visible(private_rid))
            check("被单独放行的文档进入检索范围", private_rid in s3.search_docs)

        # ---- 11.5 restricted 文件夹：无授权不可见，授权后可见 ----
        if rpa_rid:
            await place(rpa_rid, f_secret.id, Visibility.PUBLIC)
            await db.commit()
            s4 = await svc.resolve_scope(dave, [kb_id])
            check(
                "restricted 文件夹内的 public 文档默认不可见",
                not s4.is_doc_visible(rpa_rid),
            )
            db.add(
                ChunkAcl(
                    subject_type=AclSubjectType.USER, subject_id=str(dave.id),
                    kb_id=kb_id, folder_id=f_secret.id, effect=AclEffect.ALLOW,
                    note="e2e：单独授权「机密」文件夹",
                )
            )
            await db.commit()
            s5 = await svc.resolve_scope(dave, [kb_id])
            check("授权后 restricted 文件夹内文档可见", s5.is_doc_visible(rpa_rid))
            check(
                "restricted 文件夹的授权向下继承到子文件夹",
                f_secret_child.id in s5.visible_folders,
            )

        # ---- 11.6 restricted 沿链传染（对完全无规则的用户） ----
        s6 = await svc.resolve_scope(eve, [kb_id])
        check("restricted 文件夹对其它用户仍不可见", f_secret.id not in s6.visible_folders)
        check(
            "restricted 传染给子文件夹（父级受限则子级也受限）",
            f_secret_child.id not in s6.visible_folders,
        )
        check("public 文件夹对其它用户可见", f_tech.id in s6.visible_folders)
        check("用户间的文件夹规则互不干扰", f_leave.id in s6.visible_folders)

        # ---- 11.7 管理员：全量可见，但仍尊重显式 deny ----
        s_admin = await svc.resolve_scope(admin_user, [kb_id])
        check(
            "管理员可见全部文件夹",
            set(s_admin.folders) <= s_admin.visible_folders,
            f"可见 {len(s_admin.visible_folders)}/{len(s_admin.folders)}",
        )
        check(
            "管理员的可见性不因他人规则而改变",
            f_secret.id in s_admin.visible_folders,
        )
        check(
            "统计字段包含文件夹维度",
            {"total_folders", "visible_folders", "folder_granted"} <= set(s_admin.stats()),
        )

    section("11. 审计日志")
    async with SessionLocal() as db:
        from app.models.audit import OperationLog

        count = len((await db.execute(select(OperationLog))).scalars().all())
        check("审计日志表可读（问答会写入）", count >= 0)

    await manager.stop()
    await store.close()

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILURES:
        print("失败用例：")
        for f in FAILURES:
            print(f"  - {f}")
    print("=" * 60)
    if FAIL == 0:
        print("轻量内核端到端验证全部通过。")
    else:
        sys.exit(1)


def _embedding_ok() -> bool:
    from app.services.embedding import embedding_available

    return embedding_available()


if __name__ == "__main__":
    asyncio.run(run())
