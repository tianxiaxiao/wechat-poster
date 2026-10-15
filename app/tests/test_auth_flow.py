"""
test_auth_flow.py — 登录鉴权 / 权限 / 操作日志 端到端测试
====================================================================
可复现：用**临时数据库**在 8799 端口起一个独立实例，走真实 HTTP 请求验证，
不碰线上 8765 服务和真实 data/poster.db。

运行：python tests/test_auth_flow.py
输出：逐项 通过/失败 表 + 退出码（0 全通过）
"""
import http.cookiejar
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_PW = "TestAdmin123"
RESULTS = []


def check(name: str, cond: bool, detail: str = ""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


class Client:
    """带 cookie 的极简 HTTP 客户端（标准库，无第三方依赖）"""

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
    def delete(self, p, **kw): return self.req("DELETE", p, **kw)


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
    tmp = Path(tempfile.mkdtemp(prefix="poster_test_"))
    db = tmp / "test.db"
    # 预置一个「定时关闭」的设置行，避免测试实例触发真实发布
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

        anon = Client()

        print("\n[1] 未登录访问拦截")
        st, _ = anon.get("/", allow_redirect=False)
        check("未登录访问首页 → 302 跳转 /login", st in (301, 302, 307), f"HTTP {st}")
        st, d = anon.get("/api/jobs")
        check("未登录访问 /api/jobs → 401", st == 401, f"HTTP {st}")
        st, _ = anon.get("/static/index.html", allow_redirect=False)
        check("未登录直接取 /static/index.html 被拦（防绕过）", st in (302, 401, 403, 404), f"HTTP {st}")
        st, d = anon.get("/login")
        check("登录页可匿名访问", st == 200 and "登录" in str(d), f"HTTP {st}")

        print("\n[2] 登录")
        bad = Client()
        st, d = bad.post("/api/auth/login", {"username": "admin", "password": "wrong-password"})
        check("错误密码 → 401", st == 401, f"HTTP {st} {d}")
        st, d = bad.get("/api/jobs")
        check("错误密码后仍为未登录（401）", st == 401, f"HTTP {st}")

        adm = Client()
        st, d = adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录成功 → 200", st == 200 and d.get("user", {}).get("role") == "admin", f"HTTP {st}")
        st, d = adm.get("/api/jobs")
        check("登录后可访问 /api/jobs", st == 200, f"HTTP {st}")

        print("\n[3] 用户管理（仅管理员）")
        st, d = adm.post("/api/users", {"username": "ed1", "password": "Editor12345",
                                        "name": "编辑小张", "role": "editor", "is_active": 1})
        check("管理员新增编辑用户 → 200", st == 200, f"HTTP {st} {str(d)[:80]}")
        st, d = adm.post("/api/users", {"username": "ro1", "password": "Viewer12345",
                                        "name": "只读小李", "role": "viewer", "is_active": 1})
        check("管理员新增只读用户 → 200", st == 200, f"HTTP {st} {str(d)[:80]}")
        st, d = adm.post("/api/users", {"username": "ed1", "password": "Editor12345", "role": "editor"})
        check("重复用户名 → 400", st == 400, f"HTTP {st} {str(d)[:60]}")
        st, d = adm.post("/api/users", {"username": "short", "password": "123", "role": "editor"})
        check("密码过短 → 400", st == 400, f"HTTP {st} {str(d)[:60]}")
        st, d = adm.get("/api/users")
        check("管理员可读用户列表（含 3 个账号）", st == 200 and len(d) == 3, f"HTTP {st} n={len(d) if isinstance(d, list) else '-'}")

        print("\n[4] 角色权限")
        ed = Client()
        st, d = ed.post("/api/auth/login", {"username": "ed1", "password": "Editor12345"})
        check("编辑账号登录成功", st == 200, f"HTTP {st}")
        st, d = ed.get("/api/users")
        check("编辑访问用户管理 → 403", st == 403, f"HTTP {st}")
        st, d = ed.get("/api/audit")
        check("编辑访问操作日志 → 403", st == 403, f"HTTP {st}")
        st, d = ed.post("/api/topics", {"name": "测试主题A", "desc": ""})
        check("编辑可新增主题（业务写操作）→ 200", st == 200, f"HTTP {st} {str(d)[:60]}")

        ro = Client()
        st, d = ro.post("/api/auth/login", {"username": "ro1", "password": "Viewer12345"})
        check("只读账号登录成功", st == 200, f"HTTP {st}")
        st, d = ro.get("/api/jobs")
        check("只读可读 /api/jobs", st == 200, f"HTTP {st}")
        st, d = ro.post("/api/topics", {"name": "只读不该建的主题", "desc": ""})
        check("只读写操作被拒 → 403", st == 403, f"HTTP {st} {str(d)[:60]}")
        st, d = ro.post("/api/users", {"username": "x1", "password": "Xxxxxxxx1", "role": "editor"})
        check("只读写用户管理被拒 → 403", st == 403, f"HTTP {st}")

        print("\n[5] 密钥脱敏")
        st, d = adm.get("/api/accounts")
        adm_sec = (d[0].get("secret") or "") if isinstance(d, list) and d else ""
        st2, d2 = ed.get("/api/accounts")
        ed_sec = (d2[0].get("secret") or "") if isinstance(d2, list) and d2 else ""
        check("编辑看到的 AppSecret 被脱敏（含 ****）", "****" in ed_sec and "****" not in adm_sec,
              f"admin={adm_sec[:14]}… editor={ed_sec[:14]}…")

        print("\n[6] 操作日志")
        st, d = adm.get("/api/audit?limit=200")
        items = d.get("items", []) if isinstance(d, dict) else []
        acts = {it["action"] for it in items}
        check("日志可查（管理员）", st == 200 and len(items) > 0, f"HTTP {st} 共 {len(items)} 条")
        for a, label in [("auth.login", "登录成功"), ("auth.login_fail", "登录失败"),
                         ("user.create", "新增用户"), ("topic.create", "新增主题")]:
            check(f"日志含「{label}」记录", a in acts, a)
        failed = [it for it in items if it["action"] == "auth.login_fail"]
        check("登录失败记录带 IP", bool(failed) and bool(failed[0].get("ip")), failed[0].get("ip") if failed else "")
        st, d = adm.get("/api/audit/export.csv")
        check("日志可导出 CSV（带 BOM，Excel 不乱码）", st == 200 and "时间,用户" in str(d), f"HTTP {st}")
        st, d = ed.get("/api/audit/export.csv")
        check("编辑导出日志被拒 → 403", st == 403, f"HTTP {st}")

        print("\n[7] 修改密码 / 登出")
        st, d = ed.post("/api/auth/password", {"old_password": "Editor12345", "new_password": "Editor99999"})
        check("编辑改自己密码 → 200", st == 200, f"HTTP {st} {str(d)[:60]}")
        ed2 = Client()
        st, _ = ed2.post("/api/auth/login", {"username": "ed1", "password": "Editor12345"})
        check("旧密码已失效 → 401", st == 401, f"HTTP {st}")
        st, _ = ed2.post("/api/auth/login", {"username": "ed1", "password": "Editor99999"})
        check("新密码可登录 → 200", st == 200, f"HTTP {st}")
        st, d = ed2.post("/api/auth/logout")
        check("登出 → 200", st == 200, f"HTTP {st}")
        st, _ = ed2.get("/api/jobs")
        check("登出后访问受保护接口 → 401", st == 401, f"HTTP {st}")

        print("\n[8] 防爆破限流")
        bf = Client()
        codes = []
        for i in range(10):
            st, _ = bf.post("/api/auth/login", {"username": "nobody", "password": f"bad{i}"})
            codes.append(st)
        check("同一 IP 连续失败后触发 429 锁定", 429 in codes, f"状态码序列 {codes}")

        print("\n[9] 账号安全底线")
        st, d = adm.delete(f"/api/users/{1}")     # 1 = 当前登录的管理员自己
        check("不能删除当前登录账号 → 400", st == 400, f"HTTP {st} {str(d)[:60]}")
        st, d = adm.put(f"/api/users/{1}", {"role": "editor"})
        check("不能把最后一名管理员降级 → 400", st == 400, f"HTTP {st} {str(d)[:60]}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in RESULTS if c)
    print("\n" + "=" * 56)
    print(f"=== 通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    if ok != len(RESULTS):
        for n, c, d in RESULTS:
            if not c:
                print("  ❌", n, d)
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
