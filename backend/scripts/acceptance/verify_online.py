"""线上端到端验收：登录 -> 鉴权接口 -> 知识库/用户/权限/审计。

用法：python backend/scripts/acceptance/verify_online.py [BASE_URL]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

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


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'[PASS]' if ok else '[FAIL]'} {name}" + (f"  -> {detail}" if detail else ""))


print(f"目标：{API}\n")

# ---------- 1. 健康 ----------
code, body = call("GET", "/system/health")
check("健康检查 200", code == 200, f"{code} {body}")

# ---------- 2. 未认证必须被拦 ----------
code, _ = call("GET", "/users")
check("未认证访问 /users 被拒(401)", code == 401, f"HTTP {code}")
code, _ = call("GET", "/system/info")
check("未认证访问 /system/info 被拒(401)", code == 401, f"HTTP {code}")

# ---------- 3. 错误密码必须被拦 ----------
code, _ = call("POST", "/auth/login", body={**ADMIN, "password": "wrong-password-xxx"})
check("错误密码登录被拒(401)", code == 401, f"HTTP {code}")

# ---------- 4. 正确登录 ----------
code, body = call("POST", "/auth/login", body=ADMIN)
ok = code == 200 and isinstance(body, dict) and "access_token" in body
check("管理员登录成功", ok, f"HTTP {code}")
if not ok:
    print("\n登录失败，后续用例无法执行。")
    sys.exit(1)

token = body["access_token"]
user = body.get("user") or {}
check("返回用户信息含 role", bool(user.get("role")), json.dumps(user, ensure_ascii=False))

# ---------- 5. 系统信息（探测检索内核） ----------
code, body = call("GET", "/system/info", token)
check("鉴权后 /system/info 可访问", code == 200, f"HTTP {code}")
if code == 200 and isinstance(body, dict):
    print("      system/info =", json.dumps(body, ensure_ascii=False)[:400])

# ---------- 6. 关键列表接口 ----------
for path in ("/users", "/kb", "/models", "/logs"):
    code, body = call("GET", path, token)
    n = len(body) if isinstance(body, list) else (len(body.get("items", [])) if isinstance(body, dict) else "?")
    check(f"GET {path} 200", code == 200, f"HTTP {code} n={n}")
    if code != 200:
        print("      ", str(body)[:200])

print(f"\n{'=' * 56}\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("线上验收全部通过。")
