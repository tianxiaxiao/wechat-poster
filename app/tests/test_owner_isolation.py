"""
test_owner_isolation.py — 产品库 / 主题库 归属隔离验证
========================================================
需求：非管理员用户不能「显示、查看、修改、删除」非自己创建的产品与主题。

规则（本次实现）：
  · owner_id = NULL → 公共素材：所有人可见；**只有管理员能改删**
  · owner_id = 某人 → 私有：本人 + 管理员可见可改删
  · 管理员新建 → 自动落 NULL（作为团队公共素材）；普通用户新建 → 归自己

覆盖：
  · 列表可见性：editor 只见【自己的 + 公共】；admin 见全部
  · 改 / 删权限：越权 403（含「公共素材编辑也改不了」）
  · 产品与主题规则一致
  · 批量归类只作用于有权限的主题（其余记入 skipped）
  · 发布时选用他人私有产品 → 403（不启动任务）
运行：python tests/test_owner_isolation.py
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
PORT = 8790
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
    def put(self, p, b=None): return self.req("PUT", p, b)
    def delete(self, p): return self.req("DELETE", p)


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
    tmp = Path(tempfile.mkdtemp(prefix="poster_iso_"))
    db = tmp / "test.db"
    con = sqlite3.connect(db)
    con.execute("create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)")
    con.execute("insert or replace into settings (key, value) values (?, ?)",
                ("daily_schedule", json.dumps({"enabled": False, "time": "09:30", "account_id": 0,
                                               "product_id": 0, "provider": "deepseek",
                                               "last_run_date": time.strftime("%Y-%m-%d")})))
    con.commit(); con.close()

    env = dict(os.environ)
    env.update({"POSTER_DB": str(db), "ADMIN_USERNAME": "admin", "ADMIN_INIT_PASSWORD": ADMIN_PW,
                "APP_SECRET_KEY": "test-secret-key-for-unit-test", "COOKIE_SECURE": "never",
                "PYTHONIOENCODING": "utf-8"})
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=str(APP_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("❌ 测试实例未能启动，日志：")
            print((tmp / "server.log").read_text(encoding="utf-8", errors="ignore")[-2500:])
            return 1

        adm = Client()
        st, _ = adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录", st == 200, f"HTTP {st}")
        for un in ("edA", "edB"):
            st, _ = adm.post("/api/users", {"username": un, "password": "Passw0rd123", "role": "editor"})
            check(f"新建编辑 {un}", st == 200, f"HTTP {st}")
        edA = Client(); edA.post("/api/auth/login", {"username": "edA", "password": "Passw0rd123"})
        edB = Client(); edB.post("/api/auth/login", {"username": "edB", "password": "Passw0rd123"})

        print("\n[1] 未登录不可见")
        anon = Client()
        st, _ = anon.get("/api/products")
        check("未登录访问产品库 → 401", st == 401, f"HTTP {st}")
        st, _ = anon.get("/api/topics")
        check("未登录访问主题库 → 401", st == 401, f"HTTP {st}")

        print("\n[2] 建档：各自建私有 + 管理员建公共")
        st, d = edA.post("/api/products", {"name": "A的产品", "intro": "a", "style": ""})
        check("edA 建产品 → 200", st == 200, f"HTTP {st}")
        st, d = edB.post("/api/products", {"name": "B的产品", "intro": "b", "style": ""})
        check("edB 建产品 → 200", st == 200, f"HTTP {st}")
        st, d = adm.post("/api/products", {"name": "公共产品", "intro": "pub", "style": ""})
        check("admin 建产品 → 200", st == 200, f"HTTP {st}")
        st, d = adm.get("/api/products")
        P = {p["name"]: p for p in d}
        pa, pb, ppub = P["A的产品"], P["B的产品"], P["公共产品"]
        check("admin 建的产品落为「公共」(owner_id=NULL)", ppub["owner_id"] is None and ppub["shared"] is True,
              f"owner_id={ppub['owner_id']}")
        check("edA 建的产品有归属(owner_id 非空)", pa["owner_id"] is not None, f"owner_id={pa['owner_id']}")
        check("admin 视角看 edA 的产品：mine=False 但 can_edit=True（管理员可代管）",
              pa["mine"] is False and pa["can_edit"] is True,
              f"mine={pa['mine']} can_edit={pa['can_edit']}")
        check("admin 建的产品对 admin 可改 (can_edit)", ppub["can_edit"] is True)
        # 用 edA 自己的会话看，才是「我创建的」
        st, d = edA.get("/api/products")
        pa_edA = {p["name"]: p for p in d}["A的产品"]
        check("edA 视角看自己的产品：mine=True 且 can_edit=True",
              pa_edA["mine"] is True and pa_edA["can_edit"] is True,
              f"mine={pa_edA['mine']} can_edit={pa_edA['can_edit']}")
        pub_edA = {p["name"]: p for p in d}["公共产品"]
        check("edA 视角看公共产品：mine=False 且 can_edit=False（改不了）",
              pub_edA["mine"] is False and pub_edA["can_edit"] is False,
              f"mine={pub_edA['mine']} can_edit={pub_edA['can_edit']}")

        print("\n[3] 产品列表可见性")
        st, d = edA.get("/api/products")
        namesA = {p["name"] for p in d}
        check("edA 能看到自己的产品", "A的产品" in namesA, str(namesA))
        check("edA 能看到公共产品", "公共产品" in namesA, str(namesA))
        check("edA 【看不到】edB 的产品 ★核心", "B的产品" not in namesA, str(namesA))
        st, d = edB.get("/api/products")
        namesB = {p["name"] for p in d}
        check("edB 看不到 edA 的产品 ★核心", "A的产品" not in namesB, str(namesB))
        st, d = adm.get("/api/products")
        namesAd = {p["name"] for p in d}
        check("admin 能看到全部产品", {"A的产品", "B的产品", "公共产品"} <= namesAd, str(namesAd))

        print("\n[4] 产品 改 / 删 权限")
        st, d = edB.put(f"/api/products/{pa['id']}", {"name": "改A的", "intro": "x", "style": ""})
        check("edB 改 edA 的产品 → 403 ★核心", st == 403, f"HTTP {st} {d}")
        st, d = edB.delete(f"/api/products/{pa['id']}")
        check("edB 删 edA 的产品 → 403 ★核心", st == 403, f"HTTP {st} {d}")
        st, d = edA.put(f"/api/products/{ppub['id']}", {"name": "改公共", "intro": "x", "style": ""})
        check("edA 改【公共】产品 → 403（公共仅管理员可改）", st == 403, f"HTTP {st} {d}")
        st, d = edA.put(f"/api/products/{pa['id']}", {"name": "A的产品改", "intro": "x", "style": ""})
        check("edA 改自己的产品 → 200", st == 200, f"HTTP {st} {d}")
        st, d = adm.put(f"/api/products/{ppub['id']}", {"name": "公共产品改", "intro": "x", "style": ""})
        check("admin 改公共产品 → 200", st == 200, f"HTTP {st} {d}")

        print("\n[5] 主题 可见性 + 改删权限")
        st, _ = edA.post("/api/topics", {"name": "A主题", "desc": "", "product_id": 0})
        st, _ = edB.post("/api/topics", {"name": "B主题", "desc": "", "product_id": 0})
        st, _ = adm.post("/api/topics", {"name": "公共主题X", "desc": "", "product_id": 0})
        st, d = adm.get("/api/topics")
        T = {t["name"]: t for t in d}
        ta, tb, tpub = T["A主题"], T["B主题"], T["公共主题X"]
        check("admin 建的主题为公共", tpub["shared"] is True, f"owner_id={tpub['owner_id']}")
        st, d = edA.get("/api/topics")
        tnA = {t["name"] for t in d}
        check("edA 主题列表：含自己的 + 公共", {"A主题", "公共主题X"} <= tnA, str(sorted(tnA)))
        check("edA 主题列表：【不含】B主题 ★核心", "B主题" not in tnA, str(sorted(tnA)))
        st, d = edB.put(f"/api/topics/{ta['id']}", {"name": "改A主题", "desc": "", "product_id": 0})
        check("edB 改 edA 的主题 → 403 ★核心", st == 403, f"HTTP {st} {d}")
        st, d = edB.delete(f"/api/topics/{ta['id']}")
        check("edB 删 edA 的主题 → 403 ★核心", st == 403, f"HTTP {st} {d}")
        st, d = edA.delete(f"/api/topics/{tpub['id']}")
        check("edA 删公共主题 → 403（仅管理员）", st == 403, f"HTTP {st} {d}")
        st, d = edA.delete(f"/api/topics/{ta['id']}")
        check("edA 删自己的主题 → 200", st == 200, f"HTTP {st} {d}")

        print("\n[6] 批量归类：只作用于有权限的")
        st, _ = edA.post("/api/topics", {"name": "A2", "desc": "", "product_id": 0})
        st, d = adm.get("/api/topics")
        T = {t["name"]: t for t in d}
        st, d = edA.post("/api/topics/bulk-assign",
                         {"ids": [T["A2"]["id"], T["B主题"]["id"], T["公共主题X"]["id"]], "product_id": 0})
        check("批量归类混合勾选 → 200 且只改有权限的（updated=1, skipped=2）",
              st == 200 and d.get("updated") == 1 and d.get("skipped") == 2, f"HTTP {st} {d}")
        st, d = edA.post("/api/topics/bulk-assign", {"ids": [T["B主题"]["id"]], "product_id": 0})
        check("批量归类全是无权主题 → 403", st == 403, f"HTTP {st} {d}")

        print("\n[7] 发布时选用他人私有产品 → 403（且不启动任务）")
        st, d = edA.post("/api/accounts", {"name": "A号", "appid": "wx_iso_a", "secret": "sec1111111", "author": "A"})
        check("edA 建自己的公众号", st == 200, f"HTTP {st}")
        st, d = edA.get("/api/accounts")
        ea_id = (d or [{}])[0].get("id")
        st, d = edA.post("/api/run", {"topics": [{"name": "测试", "count": 1}],
                                     "product_id": pb["id"], "account_id": ea_id, "author": "A"})
        check("edA 用 edB 的私有产品发布 → 403 ★核心", st == 403, f"HTTP {st} {d}")

        print("\n[8] 同名主题在不同用户间允许（各自私有）")
        st, d = edB.post("/api/topics", {"name": "A2", "desc": "", "product_id": 0})
        check("edB 可建与 edA 同名的私有主题 → 200", st == 200, f"HTTP {st} {d}")
        st, d = edA.post("/api/topics", {"name": "公共主题X", "desc": "", "product_id": 0})
        check("edA 建与【公共主题】同名 → 400（可见范围内已存在）", st == 400, f"HTTP {st} {d}")

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
