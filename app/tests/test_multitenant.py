"""
test_multitenant.py — 多租户：多用户 / 多公众号 / 每用户独立模型与定时 的数据层与接口验证
================================================================================
复用 test_auth_flow 的临时实例模式：临时库 + 独立端口，走真实 HTTP，不碰线上数据，
也不触发真实微信发布（定时关闭 + 无账号/主题默认不发布）。

覆盖：
  · 账号归属隔离：编辑只看到自己创建的和系统共享(NULL)的号，看不到别人的号
  · 每用户私有设置：GET/POST /api/user/settings 互不串号（模型/Key/定时各自独立）
  · 越权防护：编辑不能把别人的公众号写进自己的定时账号列表（400）
  · 调度数据源：_enabled_user_schedules 只返回启用定时的用户（纯函数校验）
运行：python tests/test_multitenant.py
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
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_PW = "TestAdmin123"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def req(self, method, path, body=None, allow_redirect=True):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = self.op if allow_redirect else urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)
        try:
            with op.open(r, timeout=20) as resp:
                raw = resp.read().decode("utf-8", "ignore")
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            try:
                return e.code, json.loads(raw)
            except Exception:
                return e.code, raw
        except Exception as e:
            return 0, str(e)

    def get(self, p, **kw): return self.req("GET", p, **kw)
    def post(self, p, b=None, **kw): return self.req("POST", p, b, **kw)
    def put(self, p, b=None, **kw): return self.req("PUT", p, b, **kw)


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


def main():
    tmp = Path(tempfile.mkdtemp(prefix="poster_mt_"))
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
        "POSTER_DB": str(db),
        "ADMIN_USERNAME": "admin",
        "ADMIN_INIT_PASSWORD": ADMIN_PW,
        "APP_SECRET_KEY": "test-secret-key-for-unit-test",
        "COOKIE_SECURE": "never",
        "PYTHONIOENCODING": "utf-8",
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

        adm = Client()
        st, d = adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录", st == 200, f"HTTP {st}")

        # 建两个编辑
        st, d = adm.post("/api/users", {"username": "edA", "password": "EdA1234567", "role": "editor"})
        check("新建编辑 edA", st == 200, f"HTTP {st}")
        st, d = adm.post("/api/users", {"username": "edB", "password": "EdB1234567", "role": "editor"})
        check("新建编辑 edB", st == 200, f"HTTP {st}")

        # 公众号：admin 建一个、edA 建一个
        st, d = adm.post("/api/accounts", {"name": "admin号", "appid": "wx_admin_mt", "secret": "adminsec11111", "author": "A"})
        check("admin 建公众号 → 200", st == 200, f"HTTP {st}")
        edA = Client(); edA.post("/api/auth/login", {"username": "edA", "password": "EdA1234567"})
        st, d = edA.post("/api/accounts", {"name": "edA号", "appid": "wx_eda_mt", "secret": "edasec11111", "author": "A"})
        check("edA 建公众号 → 200", st == 200, f"HTTP {st}")
        edA_aid = d.get("id")

        # 账号隔离：edA 只看到自己的号（看不到 admin号）
        st, d = edA.get("/api/accounts")
        edA_appids = [a["appid"] for a in (d or [])]
        check("edA 看不到 admin号", "wx_admin_mt" not in edA_appids, f"edA可见={edA_appids}")
        check("edA 能看到自己的号", "wx_eda_mt" in edA_appids, f"edA可见={edA_appids}")
        st, d = adm.get("/api/accounts")
        adm_appids = [a["appid"] for a in (d or [])]
        check("admin 能看到全部号", "wx_admin_mt" in adm_appids and "wx_eda_mt" in adm_appids, f"adm可见={adm_appids}")

        # 每用户私有设置：edA / edB 各自设置不同模型，互不串
        st, d = edA.post("/api/user/settings", {
            "llm_provider": "openai_compat", "llm_base_url": "http://a.local/v1",
            "llm_api_key": "key-A", "llm_model": "model-A",
            "default_account_id": edA_aid, "schedule_enabled": True,
            "schedule_time": "09:15", "schedule_account_ids": [edA_aid], "schedule_product_id": 0})
        check("edA 保存个人设置 → 200", st == 200, f"HTTP {st}")
        edB = Client(); edB.post("/api/auth/login", {"username": "edB", "password": "EdB1234567"})
        st, d = edB.post("/api/user/settings", {
            "llm_provider": "ollama", "llm_base_url": "http://b.local:11434/v1",
            "llm_api_key": "", "llm_model": "model-B",
            "default_account_id": 0, "schedule_enabled": True,
            "schedule_time": "21:00", "schedule_account_ids": [], "schedule_product_id": 0})
        check("edB 保存个人设置 → 200", st == 200, f"HTTP {st}")

        st, d = edA.get("/api/user/settings")
        check("edA 读到自己的模型 model-A", d.get("llm_model") == "model-A" and d.get("llm_api_key") == "key-A", f"{d.get('llm_model')}")
        st, d = edB.get("/api/user/settings")
        check("edB 读到自己的模型 model-B（未串号）", d.get("llm_model") == "model-B", f"{d.get('llm_model')}")
        check("edB 的定时账号为空列表", d.get("schedule_account_ids") == [], f"{d.get('schedule_account_ids')}")

        # 越权防护：edB 试图把 edA 的号写进自己的定时列表 → 400
        st, d = edB.post("/api/user/settings", {
            "llm_provider": "ollama", "llm_model": "x", "schedule_account_ids": [edA_aid]})
        check("edB 越权写入 edA 的公众号 → 400", st == 400, f"HTTP {st}")

        # 调度数据源：只有启用定时的用户被选中（通过接口间接验证 —— 两个用户都启用，admin 全局关闭）
        # 直接用服务端进程内的纯函数校验：导入 app 检查 _enabled_user_schedules
        try:
            import importlib.util
            sys.path.insert(0, str(APP_DIR))   # 让 app 内能 `import db`
            os.environ["POSTER_DB"] = str(db)   # 本进程导入 app 时也指向测试库
            spec = importlib.util.spec_from_file_location("app_mt", str(APP_DIR / "app.py"))
            appmod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(appmod)    # 复用已 set 的 POSTER_DB 环境变量
            sess = appmod.SessionLocal()
            rows = appmod._enabled_user_schedules()
            ids = sorted(r.user_id for r in rows)
            check("调度数据源返回启用定时的用户(edA,edB)", len(rows) >= 2, f"启用用户={ids}")
            sess.close()
        except Exception as e:
            check("调度纯函数可调用", False, str(e)[:120])

        # 多账号「done」判定逻辑（纯函数）：某主题需发到 [1,2] 才 done
        it_all = {"topic": "t", "done_accounts": [1, 2]}
        it_one = {"topic": "t", "done_accounts": [1]}
        it_none = {"topic": "t", "done_accounts": []}
        check("_item_done_for_accounts 全发完=true", appmod._item_done_for_accounts(it_all, [1, 2]) is True)
        check("_item_done_for_accounts 只发1个=false", appmod._item_done_for_accounts(it_one, [1, 2]) is False)
        check("_item_done_for_accounts 空=false", appmod._item_done_for_accounts(it_none, [1, 2]) is False)

        print("\n==== 多租户验证完毕 ====")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        # shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n=== 通过 {len(RESULTS)-len(failed)} / 共 {len(RESULTS)}；失败 {len(failed)} ===")
    if failed:
        print("失败项：", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
