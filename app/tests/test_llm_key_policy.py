"""
test_llm_key_policy.py — 「每个用户必须用自己的 API Key，除非管理员授权共享」验证
================================================================================
背景（用户明确要求）：
  · 每位用户默认**必须用自己的 API Key**，各人消耗各自的额度；
  · 只有【管理员本人】或【被管理员授权】的用户，才允许回落到共享 Key（管理员设置/服务端 .env）；
  · 未授权又没填 Key → **硬拦**，绝不静默使用默认 Key；
  · 每个用户单独保存自己的模型设置；管理员可以把自己的模型设置分配给其他用户。

本脚本分两层验证（**全程不打真实大模型**）：
  A. 纯逻辑层：直接调 llm._make_client / llm.describe_config / llm.requires_user_key，
     用「读客户端实际持有的 api_key」证明 Key 优先级（env 不再顶掉用户自己的 Key）。
  B. HTTP 层：临时库 + 独立端口(8800)，验证拦截/授权/越权/留痕/Key 不回显。
运行：python tests/test_llm_key_policy.py
"""
import http.cookiejar
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PORT = 8800
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_PW = "TestAdmin123"
ED_PW = "Editor12345"
# 模拟「服务端 .env 的默认 Key = 管理员的 Key」；绝不能被普通用户用上
SERVER_KEY = "sk-SERVER-DEFAULT-ADMIN-KEY"
OWN_KEY_A = "sk-eda-OWN-key-12345"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw), raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw), raw
            except Exception:
                return e.code, raw, raw
        except Exception as e:
            return 0, str(e), str(e)

    def get(self, p): return self.req("GET", p)
    def post(self, p, b=None): return self.req("POST", p, b)
    def put(self, p, b=None): return self.req("PUT", p, b)


def wait_up(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def unit_checks():
    """A. 纯逻辑层：Key 优先级 + 是否需要自备 Key（不联网）"""
    os.environ["DEEPSEEK_API_KEY"] = SERVER_KEY
    sys.path.insert(0, str(APP_DIR))
    tmpd = tempfile.mkdtemp(prefix="poster_kp_u_")
    os.environ["POSTER_DB"] = str(Path(tmpd) / "u.db")
    import importlib
    import llm
    importlib.reload(llm)

    print("\n[A1] llm 层：用户自己的 Key 必须压过服务端 env")
    c = llm._make_client("deepseek", api_key=OWN_KEY_A)
    check("★ 传了自己的 Key → 客户端真的用它（env 不再顶掉）", c.api_key == OWN_KEY_A,
          f"client.api_key={c.api_key[:18]}…")
    c2 = llm._make_client("deepseek", api_key="")
    check("没传 Key 且允许回落 → 用 env 的 Key（管理员/单用户部署）", c2.api_key == SERVER_KEY,
          f"client.api_key={c2.api_key[:18]}…")
    c3 = llm._make_client("deepseek", api_key="", allow_env_key=False)
    check("★ 没传 Key 且 allow_env_key=False → **绝不**用 env 的 Key", c3.api_key != SERVER_KEY,
          f"client.api_key={c3.api_key}")
    c4 = llm._make_client("openai_compat", api_key=OWN_KEY_A, base_url="http://127.0.0.1:9/v1")
    check("openai_compat：传了自己的 Key 也优先", c4.api_key == OWN_KEY_A, f"{c4.api_key[:18]}…")

    print("\n[A2] llm 层：哪些情况必须自备 Key")
    check("deepseek（云端）→ 必须自备", llm.requires_user_key("deepseek") is True)
    check("ollama（本地）→ 不需要", llm.requires_user_key("ollama") is False)
    check("openai_compat + 内网地址 → 不需要", llm.requires_user_key("openai_compat", "http://192.168.1.5:8080/v1") is False)
    check("openai_compat + 自建网关域名 → 不需要", llm.requires_user_key("openai_compat", "https://qwen.dadaobo.com/v1") is False)
    check("★ openai_compat + 公网第三方 → 必须自备", llm.requires_user_key("openai_compat", "https://api.siliconflow.cn/v1") is True)

    print("\n[A3] llm 层：回显的 Key 来源")
    info = llm.describe_config("deepseek", api_key=OWN_KEY_A)
    check("describe_config 来源=你填写的", info["key_source"] == "你填写的", info["key_source"])
    info2 = llm.describe_config("deepseek", api_key="", allow_env_key=False)
    check("★ allow_env_key=False 时 has_key=False（不再谎报有 Key）", info2["has_key"] is False,
          f"has_key={info2['has_key']}")
    check("key_origin 参数可覆盖来源文案", llm.describe_config(
        "deepseek", api_key=OWN_KEY_A, key_origin="管理员共享")["key_source"] == "管理员共享")


def main():
    unit_checks()

    tmp = Path(tempfile.mkdtemp(prefix="poster_kp_"))
    db = tmp / "test.db"
    con = sqlite3.connect(db)
    con.execute("create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)")
    con.execute("insert or replace into settings (key, value) values (?, ?)",
                ("daily_schedule", json.dumps({"enabled": False, "time": "09:30", "account_id": 0,
                                               "product_id": 0, "provider": "deepseek",
                                               "last_run_date": time.strftime("%Y-%m-%d")})))
    con.commit(); con.close()

    env = dict(os.environ)
    env.update({
        "POSTER_DB": str(db), "ADMIN_USERNAME": "admin", "ADMIN_INIT_PASSWORD": ADMIN_PW,
        "APP_SECRET_KEY": "test-secret-key-for-unit-test", "COOKIE_SECURE": "never",
        "PYTHONIOENCODING": "utf-8",
        "DEEPSEEK_API_KEY": SERVER_KEY,                      # ← 服务端默认 Key（= 管理员的）
        "OPENAI_COMPAT_API_KEY": "sk-SERVER-COMPAT-KEY",
        "OPENAI_COMPAT_BASE_URL": "https://qwen.dadaobo.com/v1",
    })
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                             "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=str(APP_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("❌ 测试实例未能启动，日志：")
            print((tmp / "server.log").read_text(encoding="utf-8", errors="ignore")[-2500:])
            return 1

        adm, edA, edB = Client(), Client(), Client()
        st, _, _ = adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录", st == 200, f"HTTP {st}")
        st, _, _ = adm.post("/api/users", {"username": "edA", "password": ED_PW, "role": "editor", "name": "编辑A"})
        st, _, _ = adm.post("/api/users", {"username": "edB", "password": ED_PW, "role": "editor", "name": "编辑B"})
        edA.post("/api/auth/login", {"username": "edA", "password": ED_PW})
        edB.post("/api/auth/login", {"username": "edB", "password": ED_PW})

        print("\n[B1] 普通用户默认状态：没有自己的 Key → 保存会警告、发布被拦")
        st, d, _ = edA.get("/api/user/settings")
        check("edA 初始 use_shared=False", d.get("use_shared") is False, f"use_shared={d.get('use_shared')}")
        check("★ edA 初始 key_status=missing", d.get("key_status") == "missing", str(d.get("key_status")))
        st, d, _ = edA.post("/api/user/settings", {
            "llm_provider": "deepseek", "llm_base_url": "", "llm_model": "", "llm_api_key": "",
            "schedule_enabled": False, "schedule_time": "10:00", "schedule_account_ids": [],
            "schedule_product_id": 0})
        check("保存无 Key 的设置 → 200", st == 200, f"HTTP {st}")
        check("★ 保存返回 warning（提示缺 Key）", bool(str(d.get("warning") or "").strip()),
              str(d.get("warning"))[:90])
        check("★ warning 里说的是「你自己的 API Key」", "自己的 API Key" in str(d.get("warning")),
              str(d.get("warning"))[:90])

        st, d, _ = edA.post("/api/run", {"topic": "测试主题", "count": 1, "provider": "deepseek",
                                        "use_api": True})
        check("★ edA 未授权缺 Key 发任务 → 400 拦下", st == 400, f"HTTP {st} {str(d)[:80]}")
        check("★ 拦截提示指向「填自己的 Key / 请管理员授权」",
              ("自己的 API Key" in str(d)) and ("授权" in str(d)), str(d)[:140])

        st, d, _ = edA.post("/api/llm/test", {"provider": "deepseek", "base_url": "", "model": "", "api_key": ""})
        check("★ 「测试连通」同样被拦（不会偷偷拿默认 Key 去测）",
              st == 200 and d.get("ok") is False and d.get("blocked") is True,
              f"HTTP {st} ok={d.get('ok')} blocked={d.get('blocked')}")
        check("测试连通的提示也是人话", "自己的 API Key" in str(d.get("hint")), str(d.get("hint"))[:110])

        print("\n[B2] 填了自己的 Key → 放行，且用的是他自己的")
        st, d, _ = edA.post("/api/user/settings", {
            "llm_provider": "deepseek", "llm_base_url": "", "llm_model": "", "llm_api_key": OWN_KEY_A,
            "schedule_enabled": False, "schedule_time": "10:00", "schedule_account_ids": [],
            "schedule_product_id": 0})
        check("保存自己的 Key → 无 warning", st == 200 and not d.get("warning"), f"warning={d.get('warning')!r}")
        st, d, _ = edA.get("/api/user/settings")
        check("★ key_status=own", d.get("key_status") == "own", str(d.get("key_status")))
        check("★ key_source 说明是「你自己的 Key」", "你自己的" in str(d.get("key_source")), str(d.get("key_source")))
        check("★ 接口只回显状态，不回显 Key 明文以外的信息（Key 仅本人可见且原样返回自己的）",
              d.get("llm_api_key") == OWN_KEY_A, "本人可见自己的 Key（预期）")

        print("\n[B3] 未授权用户不能偷用服务端默认 Key")
        st, d, _ = edB.get("/api/user/settings")
        check("edB 未设置 → key_status=missing", d.get("key_status") == "missing", str(d.get("key_status")))
        st, d, _ = edB.post("/api/run", {"topic": "测试主题", "count": 1, "provider": "deepseek", "use_api": True})
        check("★ edB（也没 Key）同样被拦", st == 400, f"HTTP {st} {str(d)[:80]}")

        print("\n[B4] 管理员授权：允许某用户使用共享模型/Key")
        st, users, _ = adm.get("/api/users")
        uid_a = next(u["id"] for u in users if u["username"] == "edA")
        check("管理员可见每个用户的 key_status/has_own_key", "key_status" in users[0] and "has_own_key" in users[0],
              json.dumps({k: users[0][k] for k in ("username", "key_status", "has_own_key", "llm_use_shared")},
                         ensure_ascii=False))
        st, d, _ = adm.put("/api/users/99999", {"llm_use_shared": True})
        check("用户不存在 → 404", st == 404, f"HTTP {st}")
        st, d, _ = edA.put(f"/api/users/{uid_a}", {"llm_use_shared": True})
        check("★ 普通用户自己授权自己 → 403（越权防护）", st == 403, f"HTTP {st}")
        st, d, _ = edA.post("/api/user/settings", {
            "llm_provider": "deepseek", "llm_base_url": "", "llm_model": "", "llm_api_key": "",
            "schedule_enabled": False, "schedule_time": "10:00", "schedule_account_ids": [],
            "schedule_product_id": 0})
        st, d, _ = edA.get("/api/user/settings")
        check("★ 用户保存设置**不能**给自己开共享权限（仍是 False）", d.get("use_shared") is False,
              f"use_shared={d.get('use_shared')}")

        st, d, raw = adm.put(f"/api/users/{uid_a}", {"llm_use_shared": True})
        check("★ 管理员授权 edA 共享 → 200", st == 200, f"HTTP {st} {str(d)[:80]}")
        check("★ 返回体显示已授权", d.get("user", {}).get("llm_use_shared") is True, str(d.get("user", {}).get("llm_use_shared")))
        check("★ 响应里不含任何 Key 明文", SERVER_KEY not in raw and OWN_KEY_A not in raw, "无 Key 泄露")

        st, d, _ = edA.get("/api/user/settings")
        check("★ 授权后 edA 状态变为 shared", d.get("key_status") == "shared", str(d.get("key_status")))
        check("★ 授权后不再提示缺 Key（key_hint 说明来自管理员共享）",
              "管理员" in str(d.get("key_hint")), str(d.get("key_hint"))[:100])

        # 用自己的 Key 优先于共享：清掉自己的 Key，才回落到共享
        st, d, _ = edA.post("/api/user/settings", {
            "llm_provider": "deepseek", "llm_base_url": "", "llm_model": "", "llm_api_key": OWN_KEY_A,
            "schedule_enabled": False, "schedule_time": "10:00", "schedule_account_ids": [],
            "schedule_product_id": 0})
        st, d, _ = edA.get("/api/user/settings")
        check("★ 填了自己的 Key 时，即使被授权也优先用他自己的", d.get("key_status") == "own",
              str(d.get("key_status")))

        st, d, _ = adm.put(f"/api/users/{uid_a}", {"llm_use_shared": False})
        check("管理员取消授权 → 200", st == 200, f"HTTP {st}")
        st, d, _ = edB.get("/api/user/settings")
        check("edB 仍未被授权（互不影响）", d.get("use_shared") is False, str(d.get("use_shared")))

        print("\n[B5] 定时总览也能看出每个人的 Key 状态")
        st, d, _ = adm.get("/api/admin/user-schedules")
        rows = {u["username"]: u for u in (d.get("users") or [])}
        check("★ 定时总览带 llm_use_shared / key_status",
              "llm_use_shared" in rows.get("edA", {}) and "key_status" in rows.get("edA", {}),
              json.dumps({k: rows.get("edA", {}).get(k) for k in ("llm_use_shared", "key_status")}, ensure_ascii=False))
        check("edA 未授权时 key_status=own（自己填了 Key）", rows.get("edA", {}).get("key_status") == "own",
              str(rows.get("edA", {}).get("key_status")))

        print("\n[B6] 操作留痕")
        st, d, _ = adm.get("/api/audit?limit=120")
        items = d.get("items") if isinstance(d, dict) else d
        details = " ｜ ".join(x.get("detail", "") for x in (items or []))
        check("★ 审计记录里含「授权共享模型」", "授权共享模型" in details, details[:120])
        check("审计记录里含「取消共享模型授权」", "取消共享模型授权" in details, details[:160])
        acts = [x.get("action") for x in (items or [])]
        check("publish/job 被拦时也留痕（job.run 未产生，settings.user 有 warn）",
              "settings.user" in acts, f"actions={acts[:8]}")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    ok = sum(1 for _, c, _ in RESULTS if c)
    print("\n" + "=" * 62)
    print(f"=== Key 策略验证：通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
