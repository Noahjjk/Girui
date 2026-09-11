"""线上规模压测：大文件入库的吞吐与检索延迟。

1.6 GB 内存的机器上，「上传上限 200 MB」是个潜在雷区：
上传体要读进内存，解析后可能切出上万片段，向量全部驻留。
本脚本只测**吞吐与延迟**，不接受也不上报内存——
内存请用下面的命令在服务器上看（本脚本不做远程采集）：

    ssh root@<host> "watch -n2 'systemctl show jirui-backend -p MemoryCurrent -p MemoryPeak'"

用法：
    export JIRUI_ADMIN_PASSWORD='<.env 里的 ADMIN_INIT_PASSWORD>'
    python backend/scripts/acceptance/verify_scale.py http://47.104.154.66 [文件MB数]
"""
from __future__ import annotations

import os
import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://47.104.154.66").rstrip("/")
TARGET_MB = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5
API = f"{BASE}/api/v1"

ADMIN = {
    "username": os.environ.get("JIRUI_ADMIN_USER", "admin"),
    "password": os.environ.get("JIRUI_ADMIN_PASSWORD", ""),
    "remember_me": False,
}
if not ADMIN["password"]:
    print("[错误] 未提供管理员密码。请先设置环境变量后重跑：")
    print("       export JIRUI_ADMIN_PASSWORD='<.env 里的 ADMIN_INIT_PASSWORD>'")
    raise SystemExit(2)

PARA = (
    "第{n}条 为保证公司各项业务规范有序开展，相关部门应严格按照本制度规定执行。"
    "各业务单元在实施过程中如遇特殊情形，应及时向归口管理部门书面报告，"
    "由归口管理部门会同法务、财务共同研究后给出处理意见。"
)

# ---------------------------------------------------------------- 造文件
body = ["# 公司综合管理制度汇编（压测用，可删除）\n"]
n = 0
while len("\n".join(body).encode("utf-8")) < TARGET_MB * 1024 * 1024:
    n += 1
    body.append(f"\n## 第 {n} 章 业务规范\n")
    for k in range(20):
        m = n * 100 + k
        body.append(PARA.format(n=m))
        body.append(
            f"具体操作上，第 {m} 条所指的审批权限如下：金额十万元以下的由部门负责人审批；"
            f"十万元至五十万元的由分管副总审批；五十万元以上的须提交总经理办公会审议。"
            f"相关票据、合同与审批单应在业务完成后十个工作日内归档。"
        )
DOC = "\n".join(body).encode("utf-8")
print(f"测试文件：{len(DOC) / 1024 / 1024:.2f} MB\n")


def main() -> int:
    with httpx.Client(base_url=API, timeout=900.0) as c:
        r = c.post("/auth/login", json=ADMIN)
        if r.status_code != 200:
            print(f"[错误] 登录失败 HTTP {r.status_code}: {r.text[:200]}")
            return 1
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

        r = c.get("/kb", params={"page": 1, "page_size": 200})
        for kb in r.json().get("items", []):
            if kb["name"].startswith("压测-"):
                c.delete(f"/kb/{kb['id']}")

        kb = c.post("/kb", json={
            "name": f"压测-{int(time.time()) % 100000}",
            "description": "规模压测，可删除",
            "embedding_model": "bge-small-zh-v1.5",
            "chunk_method": "naive",
        }).json()
        kb_id = kb["id"]
        print(f"知识库 id={kb_id}")

        t0 = time.time()
        r = c.post(
            f"/kb/{kb_id}/documents/upload",
            files=[("files", ("综合管理制度汇编-压测.txt", DOC, "text/plain"))],
            data={"visibility": "public", "auto_parse": "true"},
            timeout=900.0,
        )
        print(f"上传接口 HTTP {r.status_code}，耗时 {time.time() - t0:.1f}s")
        if r.status_code != 200:
            print(r.text[:400])
            return 1

        t0 = time.time()
        deadline = time.time() + 3600
        doc = None
        while time.time() < deadline:
            docs = c.get(f"/kb/{kb_id}/documents", params={"page": 1, "page_size": 50}).json()["items"]
            if docs:
                doc = docs[0]
                el = time.time() - t0
                print(f"  [{el:7.1f}s] {doc['status']:8s} 片段={doc['chunk_count']}")
                if doc["status"] in ("ready", "failed"):
                    break
            time.sleep(10)

        if not doc:
            print("[错误] 超时未拿到文档状态")
            return 1

        total = time.time() - t0
        chunks = doc["chunk_count"] or 0
        print(f"\n解析总耗时 {total:.1f}s")
        if doc["status"] != "ready":
            print(f"[错误] 未进入 ready：{doc.get('error_message')}")
            return 1
        if chunks and total:
            print(f"吞吐 {chunks / total:.1f} 片段/秒   （{chunks} 片段）")

        # 大库检索延迟
        print("\n检索延迟（重复 5 次）：")
        lat = []
        for _ in range(5):
            t0 = time.time()
            pv = c.post("/chat/retrieval-preview", json={
                "question": "五十万元以上的支出需要谁审批", "kb_ids": [kb_id], "top_n": 5,
            })
            lat.append(time.time() - t0)
            print(f"  {lat[-1]:.2f}s  原始召回 {pv.json().get('raw_candidates')} 条，"
                  f"保留 {pv.json().get('kept')} 条")
        print(f"平均 {sum(lat) / len(lat):.2f}s")

        c.delete(f"/kb/{kb_id}")
        print("\n已清理压测知识库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
