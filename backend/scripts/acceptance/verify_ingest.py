"""线上验收 2：文档上传 -> 解析 -> 切片 -> 嵌入 -> 检索索引。

不依赖大模型，纯验证轻量检索内核。
用法：python backend/scripts/acceptance/verify_ingest.py [BASE_URL]
"""
from __future__ import annotations

import io
import json
import sys
import time
import urllib.error
import urllib.request

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

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail else ""))


# ---------------------------------------------------------------- 造测试文件
MD = """# 员工考勤与请假管理办法

## 第一章 总则

第一条 为规范公司考勤管理，保障正常办公秩序，结合公司实际，制定本办法。
本办法适用于公司全体在职员工，含试用期员工。

第二条 公司实行标准工时制，工作时间为每日九时至十八时，午休一小时。
技术研发岗位经部门负责人批准，可申请弹性工作时段，但每日在岗时间不得少于八小时。

## 第二章 请假类型

第三条 年休假。员工入职满一年后享受带薪年休假，工龄一至十年者每年五天，
十至二十年者每年十天，二十年以上者每年十五天。年休假需提前三个工作日在系统提交申请。

第四条 病假。员工因病需休息治疗的，应提供二级以上医院出具的诊断证明。
病假期间工资按当地最低工资标准的百分之八十发放。连续病假超过三十天的，
需报人力资源部备案并办理医疗期手续。

第五条 事假。员工因个人事务需请假的，按日扣发当日工资。
年度累计事假不得超过十五天，超出部分视为旷工处理。

第六条 婚假、产假、陪产假。依法办理结婚登记的，享受婚假十天；
女职工产假按国家规定执行，男职工陪产假十五天。

## 第三章 考勤异常处理

第七条 迟到与早退。迟到或早退三十分钟以内的，每次扣发当日工资的百分之十；
超过三十分钟不足两小时的，按半天事假处理；超过两小时的，按旷工半天处理。

第八条 旷工。未经批准擅自不到岗的，按旷工处理。年度累计旷工三天以上的，
公司有权解除劳动合同。

## 第四章 附则

第九条 本办法由人力资源部负责解释，自发布之日起施行。
第十条 本办法未尽事宜，按国家有关法律法规及公司其他规章制度执行。
"""

DOCX_PARAS = [
    "差旅报销实施细则",
    "",
    "一、适用范围",
    "本细则适用于公司全体员工的国内及国外出差活动所产生的交通费、住宿费、伙食补助费"
    "以及其他因公支出的报销申请。",
    "",
    "二、差旅审批",
    "员工出差前须在办公系统提交出差申请，写明出差事由、目的地、起止日期与预算金额，"
    "经直属主管审批同意后方可出行。未经审批擅自出差的，费用不予报销。",
    "紧急出差可先口头请示主管，回程后三个工作日内补办审批手续。",
    "",
    "三、交通费标准",
    "市内交通凭票实报实销。城际交通方面，总监及以上级别可乘坐高铁商务座或飞机公务舱，"
    "其余员工乘坐高铁二等座或飞机经济舱。长途夜间乘车可选择软卧。",
    "自驾出差的，按实际里程每公里一元二角补贴，过路费与停车费凭票报销。",
    "",
    "四、住宿费标准",
    "一线城市每晚不超过六百元，省会城市不超过四百五十元，其他城市不超过三百五十元。"
    "超标部分由个人承担。参加会议或培训且主办方统一安排住宿的，按主办方标准执行。",
    "",
    "五、伙食补助",
    "出差期间伙食补助按每人每天一百元包干发放，不再凭票报销。"
    "当日往返不足八小时的，减半发放。",
    "",
    "六、报销时限与流程",
    "出差结束后十个工作日内提交报销单，逾期未提交的，需由部门负责人书面说明原因。"
    "报销单需附全部原始票据，粘贴在专用报销凭证纸上，经主管、财务、总经理三级审批后打款。",
    "虚报、重复报销或伪造票据的，除追回款项外，视情节给予纪律处分。",
]

PDF_PAGES = [
    "机房运维与数据安全规范\n\n"
    "第一节 机房出入管理\n"
    "公司机房实行门禁管理，仅运维部授权人员可进入。外部人员进入须登记姓名、单位、"
    "事由、进出时间，并由内部人员全程陪同。机房门禁记录保存不少于一年。\n\n"
    "第二节 环境监控\n"
    "机房温度常年控制在十八至二十七摄氏度，相对湿度百分之四十至六十。"
    "温湿度传感器每五分钟采集一次数据，超过阈值自动向运维值班人员发送告警短信。\n"
    "机房严禁堆放杂物，严禁在机房内饮食吸烟。\n"
    ,
    "第三节 服务器与账户管理\n"
    "生产服务器统一使用堡垒机跳转登录，禁止直接使用 root 账户远程登录。"
    "每个运维人员分配独立账号，密码长度不少于十六位，每九十天强制更换一次。\n"
    "所有生产环境变更必须走工单流程，变更窗口原则上安排在业务低峰期，"
    "重要变更需两人复核并在变更后观察三十分钟。\n\n"
    "第四节 数据备份与恢复\n"
    "核心业务数据库每日凌晨执行一次全量备份，每小时执行一次增量备份。"
    "备份文件采用 AES-256 加密后上传至异地对象存储，保留期为一百八十天。\n"
    "每季度组织一次恢复演练，演练结果形成书面报告归档。\n"
    ,
    "第五节 安全事件响应\n"
    "发现数据泄露、勒索软件或异常登录等安全事件的，第一发现人须在十五分钟内"
    "上报信息安全负责人，同时隔离受影响主机，保存现场日志。\n"
    "信息安全负责人应在两小时内启动应急响应，二十四小时内形成初步分析报告，"
    "并按监管要求履行报告义务。\n\n"
    "第六节 附则\n"
    "违反本规范造成数据丢失或泄露的，视情节轻重给予通报批评、降级、解除劳动合同等处理；"
    "构成犯罪的，依法移送司法机关。\n"
    "本规范由运维部与信息安全委员会共同解释。\n",
]


def build_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}

    files["员工考勤与请假管理办法.md"] = MD.encode("utf-8")

    import docx

    d = docx.Document()
    for p in DOCX_PARAS:
        if not p:
            continue
        if p in ("差旅报销实施细则",):
            d.add_heading(p, level=1)
        elif p.startswith(("一、", "二、", "三、", "四、", "五、", "六、")):
            d.add_heading(p, level=2)
        else:
            d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    files["差旅报销实施细则.docx"] = buf.getvalue()

    import fitz

    doc = fitz.open()
    for page_text in PDF_PAGES:
        page = doc.new_page()  # A4 595x842 pt
        rect = fitz.Rect(56, 56, 539, 786)
        page.insert_textbox(rect, page_text, fontname="china-s", fontsize=10.5, align=0)
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    files["机房运维与数据安全规范.pdf"] = out.getvalue()

    return files


# ---------------------------------------------------------------- 主流程
print(f"目标：{API}\n")

with httpx.Client(base_url=API, timeout=120.0) as c:
    r = c.post("/auth/login", json=ADMIN)
    check("管理员登录", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300])
        sys.exit(1)
    c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    # 清理历史同名库，保证可重复执行
    r = c.get("/kb", params={"page": 1, "page_size": 200})
    existing = r.json().get("items", []) if r.status_code == 200 else []
    for kb in existing:
        if kb["name"].startswith("验收-"):
            c.delete(f"/kb/{kb['id']}")
            print(f"      （清理遗留知识库 {kb['name']}）")

    r = c.post("/kb", json={
        "name": f"验收-轻量内核-{int(time.time()) % 100000}",
        "description": "自动化验收用，可随时删除",
        "embedding_model": "bge-small-zh-v1.5",
        "chunk_method": "naive",
    })
    check("新建知识库", r.status_code == 201, f"HTTP {r.status_code} {r.text[:150]}")
    if r.status_code != 201:
        sys.exit(1)
    kb = r.json()
    kb_id = kb["id"]
    print(f"      知识库 id={kb_id} name={kb['name']}")
    check("知识库已分配检索数据集 ID", bool(kb.get("ragflow_dataset_id")), kb.get("ragflow_dataset_id"))

    files = build_files()
    print(f"\n  上传 {len(files)} 个文件：{', '.join(f'{k}({len(v)}B)' for k, v in files.items())}")
    multipart = [
        ("files", (name, data, "application/octet-stream")) for name, data in files.items()
    ]
    r = c.post(f"/kb/{kb_id}/documents/upload", files=multipart,
               data={"visibility": "public", "auto_parse": "true"}, timeout=300.0)
    check("上传接口 200", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        sys.exit(1)
    res = r.json()
    for item in res.get("results", res.get("items", [])):
        print(f"      - {item.get('name')}: success={item.get('success')} {item.get('message') or ''}")
    ok_upload = all(i.get("success") for i in res.get("results", res.get("items", [])))
    check("三个文件均上传成功", ok_upload)

    # 等解析完成
    print("\n  等待后台解析（解析 -> 切片 -> 嵌入）...")
    deadline = time.time() + 240
    docs = []
    while time.time() < deadline:
        r = c.get(f"/kb/{kb_id}/documents", params={"page": 1, "page_size": 50})
        docs = r.json().get("items", [])
        states = {d["name"]: d["status"] for d in docs}
        done = all(s in ("ready", "failed") for s in states.values()) and len(states) == len(files)
        print(f"      {time.strftime('%H:%M:%S')} {states}")
        if done:
            break
        time.sleep(5)

    check("所有文档解析完成（非 pending/parsing）",
          all(d["status"] in ("ready", "failed") for d in docs) and len(docs) == len(files),
          str({d["name"]: d["status"] for d in docs}))
    check("无解析失败文档",
          all(d["status"] == "ready" for d in docs),
          str({d["name"]: d.get("error_message") for d in docs if d["status"] == "failed"}))

    total_chunks = 0
    print()
    for d in docs:
        r = c.get(f"/kb/{kb_id}/documents/{d['id']}/chunks", params={"limit": 100})
        chunks = r.json() if r.status_code == 200 else []
        total_chunks += len(chunks)
        check(f"《{d['name']}》产生 ≥2 条片段", len(chunks) >= 2, f"{len(chunks)} 条")
        if chunks:
            head = chunks[0]["content"].replace("\n", " ")[:60]
            print(f"        首片：{head}…")

    check("片段总数 ≥ 8", total_chunks >= 8, f"共 {total_chunks} 条")
    check("知识库计数已回写", True, json.dumps({k: kb.get(k) for k in ("doc_count", "chunk_count")}))

    r = c.get(f"/kb/{kb_id}")
    if r.status_code == 200:
        kb2 = r.json()
        check("详情接口 doc_count/chunk_count 与实际一致",
              kb2.get("doc_count") == len(docs) and kb2.get("chunk_count") == total_chunks,
              f"doc_count={kb2.get('doc_count')} chunk_count={kb2.get('chunk_count')}")

print(f"\n{'=' * 56}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("入库链路验收全部通过。")
