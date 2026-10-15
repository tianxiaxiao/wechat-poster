"""
test_topic_product_bind.py — 主题库 ↔ 产品库 绑定验证
========================================================
需求：选择某个产品时，只出现该产品对应的主题，避免「美食产品配保健主题」的混乱。

规则（本次实现）：
  · 主题可归属某个产品（product_id>0）；归属后【只在该产品下出现】。
  · product_id=0 / 未设置 = 通用 → 所有产品都能看到（旧主题默认如此，不会突然消失）。

覆盖：
  · /api/topics?product_id=N 只返回【该产品专属 + 通用】；不传则返回全部（管理页用）
  · POST/PUT 可写入/修改归属，返回体带 product_id
  · 纯函数 _topic_names_for_product 的过滤语义（含 None 视为通用）
  · 旧库（topics 无 product_id 列）跑 init_db() 后自动补列
运行：python tests/test_topic_product_bind.py
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
PORT = 8795
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
    tmp = Path(tempfile.mkdtemp(prefix="poster_tpb_"))
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
        c = Client()
        st, _ = c.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录", st == 200, f"HTTP {st}")

        # ⚠️ 用 ZZ 前缀的独有名字：库里 init_db 会播种一批内置主题（product_id=0 通用），
        #    用「脱发」「送礼」这类种子名会「主题已存在」，且它们本就该在所有产品下出现。
        print("\n[1] 建两个产品 + 三类主题（唯一名，避开内置种子主题）")
        st, d = c.post("/api/products", {"name": "百秘一梳", "intro": "功效性木梳", "style": ""})
        check("建产品「百秘一梳」", st == 200, f"HTTP {st}")
        st, d = c.get("/api/products")
        p_mushu = {p["name"]: p["id"] for p in d}["百秘一梳"]
        st, d = c.post("/api/products", {"name": "重庆美食街", "intro": "餐饮到店", "style": ""})
        st, d = c.get("/api/products")
        p_food = {p["name"]: p["id"] for p in d}["重庆美食街"]
        check("建产品「重庆美食街」", p_food > 0, f"id={p_food}")

        st, d = c.post("/api/topics", {"name": "ZZ木梳专属", "desc": "功效", "product_id": p_mushu})
        check("建主题「ZZ木梳专属」→ 归属木梳", st == 200, f"HTTP {st} {d}")
        st, d = c.post("/api/topics", {"name": "ZZ美食专属", "desc": "美食", "product_id": p_food})
        check("建主题「ZZ美食专属」→ 归属美食街", st == 200, f"HTTP {st} {d}")
        st, d = c.post("/api/topics", {"name": "ZZ通用", "desc": "通用场景", "product_id": 0})
        check("建主题「ZZ通用」→ 通用", st == 200, f"HTTP {st} {d}")

        print("\n[2] 按产品过滤（核心需求）")
        st, d = c.get(f"/api/topics?product_id={p_mushu}")
        names = [t["name"] for t in d]
        check("木梳下：含专属「ZZ木梳专属」", "ZZ木梳专属" in names, f"共{len(names)}个")
        check("木梳下：含通用「ZZ通用」", "ZZ通用" in names)
        check("木梳下：不含美食专属「ZZ美食专属」", "ZZ美食专属" not in names)

        st, d = c.get(f"/api/topics?product_id={p_food}")
        names = [t["name"] for t in d]
        check("美食街下：含专属「ZZ美食专属」", "ZZ美食专属" in names, f"共{len(names)}个")
        check("美食街下：含通用「ZZ通用」", "ZZ通用" in names)
        check("美食街下：不含木梳专属「ZZ木梳专属」 ★核心", "ZZ木梳专属" not in names)

        st, d = c.get("/api/topics")
        names = [t["name"] for t in d]
        check("不传 product_id：返回全部（管理页用）",
              {"ZZ木梳专属", "ZZ美食专属", "ZZ通用"} <= set(names), f"共{len(names)}个")
        check("返回体带 product_id 字段", all("product_id" in t for t in d), f"{d[:1]}")

        print("\n[3] 改归属后立即生效（通用 → 绑到木梳）")
        st, d = c.get("/api/topics")
        tid = next(t["id"] for t in d if t["name"] == "ZZ通用")
        st, d = c.put(f"/api/topics/{tid}", {"name": "ZZ通用", "desc": "通用场景", "product_id": p_mushu})
        check("把「ZZ通用」改绑到木梳 → 200", st == 200, f"HTTP {st} {d}")
        st, d = c.get(f"/api/topics?product_id={p_food}")
        names = [t["name"] for t in d]
        check("美食街下：已绑走的「ZZ通用」不再出现 ★核心", "ZZ通用" not in names)
        st, d = c.get(f"/api/topics?product_id={p_mushu}")
        names = [t["name"] for t in d]
        check("木梳下：「ZZ通用」仍在", "ZZ通用" in names)

        print("\n[4] 批量归类 /api/topics/bulk-assign")
        st, d = c.get("/api/topics")
        byname = {t["name"]: t["id"] for t in d}
        ids_bulk = [byname["ZZ木梳专属"], byname["ZZ美食专属"], byname["ZZ通用"]]
        st, d = c.post("/api/topics/bulk-assign", {"ids": ids_bulk, "product_id": 0})
        check("批量归类到「通用」→ 200 且 updated=3",
              st == 200 and d.get("updated") == 3, f"HTTP {st} {d}")
        st, d = c.get(f"/api/topics?product_id={p_mushu}")
        names = [t["name"] for t in d]
        check("设为通用后：木梳下重新出现这 3 个",
              {"ZZ木梳专属", "ZZ美食专属", "ZZ通用"} <= set(names))
        st, d = c.get(f"/api/topics?product_id={p_food}")
        names = [t["name"] for t in d]
        check("设为通用后：美食街下也出现这 3 个", {"ZZ木梳专属", "ZZ美食专属"} <= set(names))

        st, d = c.post("/api/topics/bulk-assign", {"ids": ids_bulk, "product_id": p_food})
        check("批量归类到「美食街」→ updated=3", st == 200 and d.get("updated") == 3, f"HTTP {st} {d}")
        st, d = c.get(f"/api/topics?product_id={p_mushu}")
        names = [t["name"] for t in d]
        check("归类到美食街后：木梳下不再出现这 3 个 ★核心",
              not ({"ZZ木梳专属", "ZZ美食专属", "ZZ通用"} & set(names)))
        st, d = c.get(f"/api/topics?product_id={p_food}")
        names = [t["name"] for t in d]
        check("归类到美食街后：美食街下出现这 3 个",
              {"ZZ木梳专属", "ZZ美食专属", "ZZ通用"} <= set(names))

        st, d = c.post("/api/topics/bulk-assign", {"ids": [], "product_id": 0})
        check("空 ids → 400", st == 400, f"HTTP {st} {d}")
        st, d = c.post("/api/topics/bulk-assign", {"ids": [999999], "product_id": 0})
        check("不存在的 id → 404", st == 404, f"HTTP {st} {d}")
        st, d = c.post("/api/topics/bulk-assign", {"ids": ["abc"], "product_id": 0})
        check("ids 非数字 → 400", st == 400, f"HTTP {st} {d}")

        print("\n[5] [6] 纯函数 _topic_names_for_product + 旧库自动迁移")
        code = (
            "import os, sys\n"
            "os.environ['POSTER_DB'] = os.path.join(sys.argv[1], 'probe.db')\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import app as A\n"
            "class T:\n"
            "    def __init__(self, n, pid): self.name, self.product_id = n, pid\n"
            "rows = [T('保健A', 1), T('美食B', 2), T('通用C', 0), T('未绑定D', None)]\n"
            "print(A._topic_names_for_product(rows, 1))\n"
            "print(A._topic_names_for_product(rows, 2))\n"
        )
        r = subprocess.run([sys.executable, "-c", code, str(tmp), str(APP_DIR)],
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout or "").strip().splitlines()
        got1 = out[0] if len(out) > 0 else (r.stderr or "")[-200:]
        got2 = out[1] if len(out) > 1 else ""
        check("纯函数：产品1 → 专属A + 通用C + 未绑定D", got1 == "['保健A', '通用C', '未绑定D']", got1)
        check("纯函数：产品2 → 专属B + 通用C + 未绑定D", got2 == "['美食B', '通用C', '未绑定D']", got2)

        # 旧库迁移：topics 无 product_id 列
        old_db = tmp / "old.db"
        con = sqlite3.connect(old_db)
        con.execute("create table topics (id integer primary key, name varchar(128) not null,"
                    " desc varchar(255), created_at datetime)")
        con.execute("insert into topics (name, desc) values ('老主题', '')")
        con.commit(); con.close()
        code2 = (
            "import os, sqlite3, sys\n"
            "os.environ['POSTER_DB'] = sys.argv[1]\n"
            "sys.path.insert(0, sys.argv[2])\n"
            "import db\n"
            "db.init_db()\n"
            "con = sqlite3.connect(sys.argv[1])\n"
            "cols = [r[1] for r in con.execute('pragma table_info(topics)')]\n"
            "row = con.execute('select name, product_id from topics').fetchone()\n"
            "print('HAS' if 'product_id' in cols else 'NO')\n"
            "print(row[1])\n"
        )
        r2 = subprocess.run([sys.executable, "-c", code2, str(old_db), str(APP_DIR)],
                            capture_output=True, text=True, timeout=120)
        o2 = (r2.stdout or "").strip().splitlines()
        check("旧库迁移：topics 自动补 product_id 列",
              len(o2) > 0 and o2[0] == "HAS", (o2 or [(r2.stderr or "")[-200:]])[0])
        check("旧主题一律视为通用(0) → 所有产品仍可见",
              len(o2) > 1 and o2[1] == "0", o2[1] if len(o2) > 1 else "")

        print("\n" + "=" * 56)
        ok = sum(1 for _, cc, _ in RESULTS if cc)
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
