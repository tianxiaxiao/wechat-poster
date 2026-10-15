"""
test_schedule_admin.py — 管理员「定时总览 / 代管各用户定时」验证
================================================================
复用临时实例模式：临时库 + 独立端口（8797），走真实 HTTP，不碰线上数据、不触发真实发布。

覆盖：
  · /api/schedule 的 multi_user 标识（1 人=False，2 人=True）→ 前端据此隐藏老「全局定时」
  · GET  /api/admin/user-schedules：管理员可见全部用户的定时；编辑/匿名 → 403
  · POST /api/admin/user-schedules/{uid}：管理员代管生效（定时开关/时间/公众号/产品）
  · 越权防护：管理员也不能把「A 用户私有的号」分配给 B 用户 → 400
  · 共享号（owner_id=NULL）可分配给任何人 → 200
  · 参数校验：时间格式错 → 400；用户不存在 → 404
  · 代管结果对用户本人可见（/api/user/settings 能读到）
  · 操作日志留痕：schedule.admin
运行：python tests/test_schedule_admin.py
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
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PORT = 8797
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

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=20) as resp:
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

    def get(self, p): return self.req("GET", p)
    def post(self, p, b=None): return self.req("POST", p, b)


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
    tmp = Path(tempfile.mkdtemp(prefix="poster_sa_"))
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
        st, _ = adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录", st == 200, f"HTTP {st}")

        print("\n[1] multi_user 标识（前端据此隐藏老「全局定时」）")
        st, d = adm.get("/api/schedule")
        check("仅 1 个用户时 multi_user=False", st == 200 and d.get("multi_user") is False,
              f"HTTP {st} multi_user={d.get('multi_user') if st == 200 else d}")
        st, d = adm.post("/api/users", {"username": "edA", "password": "EdA1234567", "role": "editor"})
        check("新建编辑 edA", st == 200, f"HTTP {st}")
        st, d = adm.get("/api/schedule")
        check("2 个用户时 multi_user=True", st == 200 and d.get("multi_user") is True,
              f"HTTP {st} multi_user={d.get('multi_user') if st == 200 else d}")

        print("\n[2] 公众号归属准备")
        # admin 建两个号：一个转为「系统共享(NULL)」，一个保持 admin 私有（用于验证越权）
        st, d = adm.post("/api/accounts", {"name": "共享号", "appid": "wx_shared_sa", "secret": "sharedsec1111", "author": "A"})
        check("admin 建「共享号」→ 200", st == 200, f"HTTP {st}")
        shared_id = d.get("id")
        st, d = adm.post("/api/accounts", {"name": "admin私有号", "appid": "wx_admin_sa", "secret": "adminsec11111", "author": "A"})
        check("admin 建「admin私有号」→ 200", st == 200, f"HTTP {st}")
        admin_private_id = d.get("id")
        edA = Client(); edA.post("/api/auth/login", {"username": "edA", "password": "EdA1234567"})
        st, d = edA.post("/api/accounts", {"name": "edA号", "appid": "wx_eda_sa", "secret": "edasec11111", "author": "A"})
        check("edA 建自己的号 → 200", st == 200, f"HTTP {st}")
        eda_id = d.get("id")
        # 把「共享号」改成系统共享（owner_id=NULL）
        con = sqlite3.connect(db); con.execute("update accounts set owner_id = NULL where id = ?", (shared_id,))
        con.commit(); con.close()

        print("\n[3] 权限：只有管理员能进定时总览")
        anon = Client()
        st, d = anon.get("/api/admin/user-schedules")
        check("未登录访问定时总览 → 401", st == 401, f"HTTP {st}")
        st, d = edA.get("/api/admin/user-schedules")
        check("编辑访问定时总览 → 403", st == 403, f"HTTP {st}")
        st, d = edA.post("/api/admin/user-schedules/1", {"schedule_enabled": True, "schedule_time": "08:00"})
        check("编辑代管他人定时 → 403", st == 403, f"HTTP {st}")
        st, d = adm.get("/api/admin/user-schedules")
        check("管理员访问定时总览 → 200", st == 200, f"HTTP {st}")

        print("\n[4] 总览内容")
        usernames = [u["username"] for u in d.get("users", [])]
        check("总览含全部用户(admin/edA)", set(usernames) >= {"admin", "edA"}, f"users={usernames}")
        eda_row = next((u for u in d["users"] if u["username"] == "edA"), None)
        check("edA 初始未开启定时", eda_row and eda_row["schedule_enabled"] is False,
              f"enabled={eda_row and eda_row['schedule_enabled']}")
        check("edA 可用号 = 自己的 + 共享（不含 admin私有号）",
              eda_row and set(eda_row["allowed_account_ids"]) == {eda_id, shared_id},
              f"allowed={eda_row and eda_row['allowed_account_ids']} 期望=({eda_id},{shared_id})")
        check("返回公众号列表供勾选", any(a["id"] == shared_id for a in d.get("accounts", [])),
              f"accounts={[a['name'] for a in d.get('accounts', [])]}")
        check("含全局兜底定时信息", "global_fallback" in d and "enabled" in d["global_fallback"],
              f"gb={d.get('global_fallback')}")
        check("multi_user=True", d.get("multi_user") is True, f"multi_user={d.get('multi_user')}")

        print("\n[5] 越权防护：管理员也不能把他人私有号分配给 edA")
        st, d = adm.post(f"/api/admin/user-schedules/{eda_row['id']}", {
            "schedule_enabled": True, "schedule_time": "09:15",
            "schedule_account_ids": [admin_private_id], "schedule_product_id": 0})
        check("分配 admin私有号给 edA → 400", st == 400, f"HTTP {st} {d}")

        print("\n[6] 代管生效：共享号 + edA 自己的号")
        st, d = adm.post(f"/api/admin/user-schedules/{eda_row['id']}", {
            "schedule_enabled": True, "schedule_time": "09:15",
            "schedule_account_ids": [eda_id, shared_id], "schedule_product_id": 0})
        check("代管保存 → 200", st == 200, f"HTTP {st} {d}")
        st, d = adm.get("/api/admin/user-schedules")
        eda_row2 = next((u for u in d["users"] if u["username"] == "edA"), None)
        check("总览显示 edA 已开启定时", eda_row2 and eda_row2["schedule_enabled"] is True,
              f"enabled={eda_row2 and eda_row2['schedule_enabled']}")
        check("总览显示时间 09:15", eda_row2 and eda_row2["schedule_time"] == "09:15",
              f"time={eda_row2 and eda_row2['schedule_time']}")
        check("总览显示勾选了 2 个号", eda_row2 and set(eda_row2["schedule_account_ids"]) == {eda_id, shared_id},
              f"ids={eda_row2 and eda_row2['schedule_account_ids']}")
        check("总览带出号名便于核对", eda_row2 and set(eda_row2["account_names"]) == {"共享号", "edA号"},
              f"names={eda_row2 and eda_row2['account_names']}")
        st, d = edA.get("/api/user/settings")
        check("代管结果对 edA 本人可见（其 /api/user/settings 能读到）",
              st == 200 and d.get("schedule_enabled") is True and set(d.get("schedule_account_ids", [])) == {eda_id, shared_id},
              f"HTTP {st} enabled={d.get('schedule_enabled')} ids={d.get('schedule_account_ids')}")

        print("\n[7] 参数校验")
        st, d = adm.post(f"/api/admin/user-schedules/{eda_row['id']}", {
            "schedule_enabled": True, "schedule_time": "25:99", "schedule_account_ids": []})
        check("时间格式非法 → 400", st == 400, f"HTTP {st} {d}")
        st, d = adm.post("/api/admin/user-schedules/99999", {"schedule_enabled": True, "schedule_time": "10:00"})
        check("用户不存在 → 404", st == 404, f"HTTP {st} {d}")

        print("\n[8] 操作日志留痕")
        st, d = adm.get("/api/audit?limit=50&q=" + urllib.parse.quote("代管"))
        hit = st == 200 and any("代管" in (it.get("detail") or "") for it in d.get("items", []))
        check("代管动作写入操作日志", hit,
              f"HTTP {st} 命中={sum(1 for it in d.get('items', []) if '代管' in (it.get('detail') or '')) if st == 200 else d}")

        print("\n[9] 全局兜底仍可单独开关（单人部署兼容）")
        st, d = adm.post("/api/schedule", {"enabled": True, "time": "07:45", "account_id": 0, "product_id": 0})
        check("管理员保存全局兜底 → 200", st == 200, f"HTTP {st} {d}")
        st, d = adm.get("/api/admin/user-schedules")
        gb = d.get("global_fallback", {})
        check("总览读到全局兜底已开启 07:45", gb.get("enabled") is True and gb.get("time") == "07:45",
              f"gb={gb}")

        print("\n" + "=" * 56)
        ok = sum(1 for _, c, _ in RESULTS if c)
        print(f"=== 通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
        return 0 if ok == len(RESULTS) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
