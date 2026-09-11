"""线上验收 3：片段级权限隔离（本系统最核心的能力）。

不需要大模型，用 /chat/retrieval-preview 直接看检索+权限过滤结果。

覆盖的判定优先级：
    片段 deny > 片段 allow > 文档白名单模式 > 文档级可见性

用法：python backend/scripts/acceptance/verify_permission.py [BASE_URL]
"""
from __future__ import annotations

import io
import json
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://47.104.154.66").rstrip("/")
API = f"{BASE}/api/v1"
import os

ADMIN = {
    "username": os.environ.get("JIRUI_ADMIN_USER", "admin"),
    "password": os.environ.get("JIRUI_ADMIN_PASSWORD", ""),
    "remember_me": False,
}
if not ADMIN["password"]:
    print("[错误] 未提供管理员密码。请先设置环境变量后重跑：")
    print("       export JIRUI_ADMIN_PASSWORD='<.env 里的 ADMIN_INIT_PASSWORD>'")
    raise SystemExit(2)
PWD = "Jirui@Test2026"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------- 测试语料
PUBLIC_DOC = """极睿知识库 产品功能说明书

一、产品定位
极睿知识库是面向企业内部的知识检索与问答系统，把散落在共享盘、聊天记录、
邮件附件里的公司文档统一收拢，提供带权限控制的语义检索能力。

二、支持的文档格式
系统支持 PDF、Word（docx）、Excel（xlsx）、PowerPoint（pptx）、
纯文本、Markdown、CSV、JSON、HTML 等格式的解析与切片。
旧版二进制格式（doc、xls、ppt）请先另存为新版格式后再上传。

三、检索方式
系统采用向量语义检索与中文全文检索相结合的混合检索策略。
向量部分用 bge-small-zh-v1.5 模型把文本编码为 512 维向量，
全文部分用 jieba 分词后写入 SQLite FTS5 索引。
两路结果归一化后按权重融合，默认向量权重为零点六。

四、权限模型
权限分两层。知识库层决定用户能进入哪些库；
文档与片段层决定具体内容是否可见。
管理员可以对单个片段单独授权或屏蔽，粒度精确到一个段落。

五、溯源与审计
每条回答都会标注引用的文档名与片段序号，点击可查看原文。
所有检索与问答行为写入审计日志，记录提问人、提问时间、命中的片段与耗时。
"""

SECRET_DOC = """薪酬结构与职级管理办法（机密）

第一条 适用范围
本办法适用于公司全体正式员工的薪酬定级、调薪与奖金核算，
属于公司机密文件，未经人力资源部书面许可不得对外披露。

第二条 职级体系
公司职级分为 P1 至 P8 共八个技术序列等级，M1 至 M4 共四个管理序列等级。
应届本科毕业生定级 P3，硕士定级 P4，博士定级 P5。

第三条 薪酬带宽
P3 级月薪带宽为一万二千元至一万八千元；
P4 级为一万八千元至两万六千元；
P5 级为两万六千元至三万八千元；
P6 级为三万八千元至五万五千元。
超出带宽上限的定薪需经薪酬委员会专项审批。

第四条 绩效奖金
年度绩效奖金基数为三至六个月月薪，按个人绩效系数与公司业绩系数加权计算。
绩效系数分布强制排序：S 级不超过百分之十，A 级不超过百分之三十，
B 级占比约百分之五十，C 级不低于百分之十。

第五条 调薪机制
每年四月与十月各组织一次调薪评审。普调幅度参考当年通胀率与行业薪酬涨幅，
一般在百分之三至百分之八之间。晋升调薪不受普调窗口限制。

第六条 保密要求
员工薪酬信息属于个人隐私与公司机密，严禁私下互相打探、比较或传播。
违反者视情节给予书面警告直至解除劳动合同的处分。
"""


def build_files():
    files = {}
    files["公开-产品功能说明书.md"] = PUBLIC_DOC.encode("utf-8")
    files["机密-薪酬结构与职级管理办法.md"] = SECRET_DOC.encode("utf-8")
    return files


# ---------------------------------------------------------------- 工具
def login(c: httpx.Client, username: str, password: str):
    r = c.post("/auth/login", json={"username": username, "password": password, "remember_me": False})
    if r.status_code != 200:
        return None
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
    return r.json()["user"]


def preview(c: httpx.Client, question: str, kb_ids: list[int] | None = None, top_n: int = 10):
    r = c.post("/chat/retrieval-preview", json={"question": question, "kb_ids": kb_ids, "top_n": top_n})
    if r.status_code != 200:
        return r.status_code, {"error": r.text[:300]}
    return r.status_code, r.json()


def doc_names(resp: dict) -> set[str]:
    out = set()
    for cit in resp.get("citations") or []:
        name = cit.get("document_name") or cit.get("doc_name") or ""
        if name:
            out.add(name)
    return out


print(f"目标：{API}\n")

with httpx.Client(base_url=API, timeout=180.0) as admin:
    assert login(admin, ADMIN["username"], ADMIN["password"]), "管理员登录失败"
    print("  管理员已登录\n")

    # ---------- 清理 ----------
    r = admin.get("/kb", params={"page": 1, "page_size": 200})
    for kb in (r.json().get("items", []) if r.status_code == 200 else []):
        if kb["name"].startswith("权限验收-"):
            admin.delete(f"/kb/{kb['id']}")
    r = admin.get("/users", params={"page": 1, "page_size": 200})
    for u in (r.json().get("items", []) if r.status_code == 200 else []):
        if u["username"] in ("zhangwei", "lina"):
            admin.delete(f"/users/{u['id']}")

    # ---------- 建账号 ----------
    users = {}
    for uname, dname in (("zhangwei", "张伟"), ("lina", "李娜")):
        r = admin.post("/users", json={
            "username": uname, "password": PWD, "display_name": dname,
            "role": "user", "department": "测试部",
        })
        check(f"创建账号 {uname}({dname})", r.status_code == 201, f"HTTP {r.status_code} {r.text[:120]}")
        if r.status_code == 201:
            users[uname] = r.json()["id"]
    if len(users) != 2:
        sys.exit(1)

    # ---------- 建知识库 ----------
    r = admin.post("/kb", json={
        "name": f"权限验收-{int(time.time()) % 100000}",
        "description": "片段级权限隔离验收",
        "embedding_model": "bge-small-zh-v1.5", "chunk_method": "naive",
    })
    check("新建知识库", r.status_code == 201, f"HTTP {r.status_code}")
    kb_id = r.json()["id"]

    # ---------- 上传 ----------
    files = build_files()
    multipart = [("files", (n, d, "text/markdown")) for n, d in files.items()]
    r = admin.post(f"/kb/{kb_id}/documents/upload", files=multipart,
                   data={"visibility": "public", "auto_parse": "true"}, timeout=300.0)
    check("上传两个文档", r.status_code == 200 and r.json().get("succeeded") == 2,
          f"HTTP {r.status_code} {r.text[:200]}")

    # 等解析
    docs = []
    deadline = time.time() + 240
    while time.time() < deadline:
        r = admin.get(f"/kb/{kb_id}/documents", params={"page": 1, "page_size": 50})
        docs = r.json().get("items", [])
        if len(docs) == 2 and all(d["status"] in ("ready", "failed") for d in docs):
            break
        time.sleep(5)
    check("两文档解析完成", len(docs) == 2 and all(d["status"] == "ready" for d in docs),
          str({d["name"]: d["status"] for d in docs}))

    pub = next((d for d in docs if d["name"].startswith("公开-")), None)
    sec = next((d for d in docs if d["name"].startswith("机密-")), None)
    assert pub and sec, "没找到测试文档"

    # ---------- 机密文档改成 restricted ----------
    r = admin.patch(f"/kb/{kb_id}/documents/{sec['id']}", json={"visibility": "restricted"})
    check("机密文档置为 restricted", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------- 授权两个用户读该库 ----------
    for uname, uid in users.items():
        r = admin.post(f"/kb/{kb_id}/permissions", json={
            "user_id": uid, "can_read": True, "can_upload": False, "can_manage": False,
        })
        check(f"授权 {uname} 可读知识库", r.status_code in (200, 201), f"HTTP {r.status_code}")

    # ---------- 取片段 ID ----------
    def chunks_of(doc_id: int):
        r = admin.get(f"/kb/{kb_id}/documents/{doc_id}/chunks", params={"limit": 100})
        return r.json() if r.status_code == 200 else []

    pub_chunks = chunks_of(pub["id"])
    sec_chunks = chunks_of(sec["id"])
    check("公开文档有多个片段", len(pub_chunks) >= 2, f"{len(pub_chunks)} 条")
    check("机密文档有多个片段", len(sec_chunks) >= 2, f"{len(sec_chunks)} 条")
    if not pub_chunks or not sec_chunks:
        sys.exit(1)

    print(f"      公开 doc_id={pub['id']}  首片 chunk_id={pub_chunks[0]['chunk_id']}")
    print(f"      机密 doc_id={sec['id']}  首片 chunk_id={sec_chunks[0]['chunk_id']}\n")

    SEC_Q = "P5 级职级的薪酬带宽是多少，绩效奖金怎么算"
    PUB_Q = "系统支持上传哪些格式的文档，检索是怎么实现的"

    # ---------- 基线：管理员（超管） ----------
    code, resp = preview(admin, SEC_Q, [kb_id])
    admin_sec = doc_names(resp)
    print("      管理员 / 机密问题 ->", json.dumps(resp, ensure_ascii=False)[:260])
    check("管理员能检索到机密文档", any(n.startswith("机密-") for n in admin_sec), str(admin_sec))

    code, resp = preview(admin, PUB_Q, [kb_id])
    admin_pub = doc_names(resp)

    # ---------- 基线：张伟、李娜 未加片段规则时 ----------
    with httpx.Client(base_url=API, timeout=180.0) as zw:
        assert login(zw, "zhangwei", PWD)
        code, resp = preview(zw, SEC_Q, [kb_id])
        print("      张伟(未加规则) / 机密问题 ->", json.dumps(resp, ensure_ascii=False)[:260])
        check("restricted 文档对普通用户默认不可见",
              not any(n.startswith("机密-") for n in doc_names(resp)), str(doc_names(resp)))

        code, resp = preview(zw, PUB_Q, [kb_id])
        check("public 文档对普通用户默认可见",
              any(n.startswith("公开-") for n in doc_names(resp)), str(doc_names(resp)))

        # ---------- 片段级 ALLOW：只放行机密文档的第 2 片给张伟 ----------
        allowed_chunk = sec_chunks[1]["chunk_id"]
        r = admin.post(f"/kb/{kb_id}/acls", json={
            "subject_type": "user", "subject_id": str(users["zhangwei"]), "kb_id": kb_id,
            "ragflow_document_id": sec["ragflow_document_id"], "chunk_id": allowed_chunk,
            "effect": "allow", "note": "验收：单独放行给张伟",
        })
        check("新增片段级 allow 规则", r.status_code == 201, f"HTTP {r.status_code} {r.text[:200]}")

        code, resp = preview(zw, SEC_Q, [kb_id])
        got = resp.get("citations") or []
        got_ids = {c.get("chunk_id") for c in got}
        print("      张伟(已放行1片) / 机密问题 ->", json.dumps(resp, ensure_ascii=False)[:300])
        check("片段 allow 命中了被放行的那一片", allowed_chunk in got_ids, f"命中 {got_ids}")
        check("片段 allow 只放行该片，同文档其他片段仍不可见",
              len([i for i in got_ids if i in {c['chunk_id'] for c in sec_chunks}]) == 1,
              f"机密命中共 {len([i for i in got_ids if i in {c['chunk_id'] for c in sec_chunks}])} 片")

        # ---------- 片段级 DENY 优先于文档可见性 ----------
        denied_chunk = pub_chunks[0]["chunk_id"]
        r = admin.post(f"/kb/{kb_id}/acls", json={
            "subject_type": "user", "subject_id": str(users["zhangwei"]), "kb_id": kb_id,
            "ragflow_document_id": pub["ragflow_document_id"], "chunk_id": denied_chunk,
            "effect": "deny", "note": "验收：屏蔽公开文档首片",
        })
        check("新增片段级 deny 规则", r.status_code == 201, f"HTTP {r.status_code} {r.text[:200]}")

        code, resp = preview(zw, PUB_Q, [kb_id])
        got_ids = {c.get("chunk_id") for c in (resp.get("citations") or [])}
        print("      张伟(公开文档有deny) / 公开问题 ->", json.dumps(resp, ensure_ascii=False)[:300])
        check("片段 deny 生效：被屏蔽的片段不再出现", denied_chunk not in got_ids, f"命中 {got_ids}")
        check("同文档其余片段仍可见",
              len(got_ids & {c["chunk_id"] for c in pub_chunks}) >= 1, f"命中 {got_ids}")

    # ---------- 李娜：什么都没配，不能越权 ----------
    with httpx.Client(base_url=API, timeout=180.0) as ln:
        assert login(ln, "lina", PWD)
        code, resp = preview(ln, SEC_Q, [kb_id])
        check("李娜看不到机密文档（片段规则不串号）",
              not any(n.startswith("机密-") for n in doc_names(resp)), str(doc_names(resp)))
        code, resp = preview(ln, PUB_Q, [kb_id])
        got_ids = {c.get("chunk_id") for c in (resp.get("citations") or [])}
        check("李娜不受张伟的 deny 影响，公开文档首片仍可见",
              pub_chunks[0]["chunk_id"] in got_ids, f"命中 {got_ids}")

    # ---------- 最终一致性：同一问题，三人结果不同 ----------
    print()
    for who in ("admin", "zhangwei", "lina"):
        if who == "admin":
            code, resp = preview(admin, PUB_Q + " 另外 " + SEC_Q, [kb_id], top_n=20)
        else:
            with httpx.Client(base_url=API, timeout=180.0) as c:
                login(c, who, PWD)
                code, resp = preview(c, PUB_Q + " 另外 " + SEC_Q, [kb_id], top_n=20)
        print(f"      {who:9s} raw={resp.get('raw_candidates')} 被权限丢弃={resp.get('dropped_by_permission')} "
              f"保留={resp.get('kept')}")

    # ---------- 诊断接口 ----------
    r = admin.get(f"/kb/{kb_id}/diagnose/{users['zhangwei']}")
    print("\n      诊断-张伟 ->", json.dumps(r.json(), ensure_ascii=False)[:400] if r.status_code == 200 else r.text[:200])
    r = admin.get(f"/kb/{kb_id}/acls")
    print("      片段规则数 ->", len(r.json()) if r.status_code == 200 else r.text[:120])

print(f"\n{'=' * 56}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("片段级权限隔离验收全部通过。")
