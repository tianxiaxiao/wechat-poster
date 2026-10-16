"""
test_schedule_topics.py — 「定时任务要发哪些主题」验证
======================================================
背景：以前定时任务固定发【该产品下全部主题】。2026-10-16 改成
      主界面主题区「🕒 定时任务」里勾哪些就发哪些，勾选即加入、取消即移除。

本脚本走临时库 + 独立端口（8798），不碰线上数据、不触发真实发布。覆盖：
  [A] 纯函数：_filter_topics_for_schedule（未指定 / 指定子集 / 指定全不匹配 / 产品过滤仍然生效）
  [B] 纯函数：_parse_int_list 吃脏数据不炸
  [C] 接口：POST /api/user/schedule-topics 鉴权 / 正常写入 / 读回 / 清空
  [D] 越权：不存在的主题 → 400；别人私有的主题 → 400
  [E] ★「模型设置」整份保存（不带 schedule_topic_ids）**不会清空**定时任务主题
  [F] 体检接口 /api/system/status 的 schedule 带 topic_ids / topics_all
  [G] 管理员定时总览能看到每人指定的主题
  [H] 迁移：旧库（无 schedule_topic_ids 列）经 init_db() 自动补列，且幂等
运行：python tests/test_schedule_topics.py
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

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method,
                                   headers={"Content-Type": "application/json"})
        try:
            with self.op.open(r, timeout=25) as resp:
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


def wait_up(timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(BASE + "/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


def unit_tests(db_path):
    """[A][B][H] 纯函数/迁移单测：不启服务，直接 import app。

    ⚠️⚠️ 必须**先**把 POSTER_DB 指到临时库再 import app —— 否则会在**真实库**上跑迁移/种子，
       等于拿生产数据做实验。这里踩过一次：漏设环境变量时，import app 拿的是
       app/data/poster.db（本地真实库），PRAGMA 读出来当然没有新列。
    """
    os.environ["POSTER_DB"] = str(db_path)
    sys.path.insert(0, str(APP_DIR))
    import app as A  # noqa: E402
    assert str(A.db_api.DB_PATH) == str(db_path), f"DB 没指向临时库：{A.db_api.DB_PATH}"

    class T:
        def __init__(self, tid, name, pid):
            self.id, self.name, self.product_id = tid, name, pid

    rows = [T(1, "偏头痛", 1), T(2, "失眠", 1), T(3, "通用主题", 0), T(4, "美食探店", 2)]
    prod1 = A._topic_names_for_product(rows, 1)      # 偏头痛 / 失眠 / 通用主题
    check("产品过滤：产品1 = 专属2个 + 通用1个", prod1 == ["偏头痛", "失眠", "通用主题"], str(prod1))

    print("\n[A] 定时任务主题过滤（纯函数）")
    got, before = A._filter_topics_for_schedule(rows, prod1, set())
    check("★ 未指定(sel_ids 空) → 原样返回全部", got == prod1 and before == 3, f"{got} before={before}")
    got, before = A._filter_topics_for_schedule(rows, prod1, {1, 3})
    check("★ 指定子集 → 只发指定的那几个", got == ["偏头痛", "通用主题"], str(got))
    check("★ 返回筛选前条数用于日志", before == 3, str(before))
    got, _ = A._filter_topics_for_schedule(rows, prod1, {4})
    # ★ 关键：用户勾了别的产品(美食)的主题，而定时用的产品是木梳 → 一个都不匹配
    check("★ 指定的主题不属于该产品 → 结果为空（调用方据此告警，不能静默跳过）", got == [], str(got))
    got, _ = A._filter_topics_for_schedule(rows, prod1, {1, 4})
    check("★ 部分匹配：只保留既被指定又属于该产品的", got == ["偏头痛"], str(got))

    print("\n[B] _parse_int_list 吃脏数据不炸")
    check("正常 JSON", A._parse_int_list("[1,2,3]") == [1, 2, 3], str(A._parse_int_list("[1,2,3]")))
    check("空字符串 → []", A._parse_int_list("") == [], str(A._parse_int_list("")))
    check("坏 JSON → []（宁可当未指定，也别带脏数据跑定时）",
          A._parse_int_list("{oops") == [], str(A._parse_int_list("{oops")))
    check("混入非数字 → 跳过而非报错", A._parse_int_list('[1,"x",3]') == [1, 3],
          str(A._parse_int_list('[1,"x",3]')))
    check("None → []", A._parse_int_list(None) == [], "")

    # [H] ★ 生产库是**旧库**：create_all 不会给已存在的表加列，必须靠 _migrate_schema 补。
    #     这一步错了，定时一跑就崩（读不存在的列）。所以模拟"旧库"删掉列再迁移回来。
    print("\n[H] 迁移：旧库补列（生产库就是旧库，这步错了定时直接崩）")
    dbp = str(A.db_api.DB_PATH)
    A.init_db()          # import 时不会建表（init_db 在服务启动事件里），这里显式建一次
    con = sqlite3.connect(dbp)
    cols = [r[1] for r in con.execute("PRAGMA table_info(user_settings)").fetchall()]
    check("新库建出来就带 schedule_topic_ids 列", "schedule_topic_ids" in cols, str(cols))
    try:
        con.execute("ALTER TABLE user_settings DROP COLUMN schedule_topic_ids")
        con.commit()
        cols2 = [r[1] for r in con.execute("PRAGMA table_info(user_settings)").fetchall()]
        check("已模拟成旧库（列被删掉）", "schedule_topic_ids" not in cols2, str(cols2))
        A.init_db()
        cols3 = [r[1] for r in con.execute("PRAGMA table_info(user_settings)").fetchall()]
        check("★ init_db() 迁移后列回来了（旧库升级不会一篇不发）",
              "schedule_topic_ids" in cols3, str(cols3))
        A.init_db()
        check("重复迁移不报错（幂等）", True, "")
    except sqlite3.OperationalError as e:
        check("SQLite 支持 DROP COLUMN 以做迁移模拟", False, str(e))
    finally:
        con.close()
    return A


def main():
    tmp = Path(tempfile.mkdtemp(prefix="poster_st_"))
    db = tmp / "test.db"
    con = sqlite3.connect(db)
    con.execute("create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)")
    con.execute("insert or replace into settings (key, value) values (?, ?)",
                ("daily_schedule", json.dumps({"enabled": False, "time": "09:30", "account_id": 0,
                                               "product_id": 0, "provider": "deepseek",
                                               "last_run_date": ""})))
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

    print("[0] 纯函数单测（不启服务）")
    A = unit_tests(db)

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

        print("\n[C1] 准备主题（admin 建 2 个；edB 建 1 个私有的）")
        # ⚠️ 先建产品：主题要挂 product_id=1，产品不存在时建主题会被拒
        # ⚠️ 主题名别用「偏头痛/失眠」等——init_db() 会种子一批默认主题，同名会被拒
        st, d = adm.post("/api/products", {"name": "百秘一梳", "intro": "木梳"})
        check("建产品", st == 200, f"HTTP {st}")
        st, d = adm.post("/api/topics", {"name": "定时任务测试甲", "desc": "", "product_id": 1})
        t1 = (d or {}).get("id") if st == 200 else None
        check("建主题「定时任务测试甲」", st == 200 and t1, f"HTTP {st} {d if st != 200 else ''}")
        st, d = adm.post("/api/topics", {"name": "定时任务测试乙", "desc": "", "product_id": 1})
        t2 = (d or {}).get("id") if st == 200 else None
        check("建主题「定时任务测试乙」", st == 200 and t2, f"HTTP {st} {d if st != 200 else ''}")

        st, d = adm.post("/api/users", {"username": "edB", "password": "EdB1234567", "role": "editor"})
        check("新建编辑 edB", st == 200, f"HTTP {st}")
        edB = Client(); edB.post("/api/auth/login", {"username": "edB", "password": "EdB1234567"})
        st, d = edB.post("/api/topics", {"name": "edB私有主题", "desc": "", "product_id": 1})
        t3 = (d or {}).get("id") if st == 200 else None
        check("edB 建自己的私有主题", st == 200 and t3, f"HTTP {st}")

        print("\n[C2] POST /api/user/schedule-topics")
        anon = Client()
        st, d = anon.post("/api/user/schedule-topics", {"topic_ids": [t1]})
        check("★ 未登录 → 401", st == 401, f"HTTP {st}")
        st, d = adm.post("/api/user/schedule-topics", {"topic_ids": [t1, t2]})
        check("设置定时任务主题 → 200", st == 200 and (d or {}).get("ok") is True, f"HTTP {st} {d}")
        check("返回落库的 id 列表", sorted((d or {}).get("topic_ids") or []) == sorted([t1, t2]),
              str((d or {}).get("topic_ids")))
        check("非空时 all_topics=False", (d or {}).get("all_topics") is False, str((d or {}).get("all_topics")))

        st, d = adm.get("/api/user/settings")
        ids = (d or {}).get("schedule_topic_ids") or []
        check("★ /api/user/settings 能读回定时任务主题", st == 200 and sorted(ids) == sorted([t1, t2]),
              f"HTTP {st} {ids}")

        print("\n[C3] 清空 = 恢复「发全部主题」")
        st, d = adm.post("/api/user/schedule-topics", {"topic_ids": []})
        check("清空 → 200 且 all_topics=True", st == 200 and (d or {}).get("all_topics") is True, f"HTTP {st} {d}")
        st, d = adm.get("/api/user/settings")
        check("★ 读回为空列表（= 未指定）", ((d or {}).get("schedule_topic_ids") or []) == [],
              str((d or {}).get("schedule_topic_ids")))

        print("\n[D] 越权与参数校验")
        st, d = adm.post("/api/user/schedule-topics", {"topic_ids": [99999]})
        check("★ 不存在的主题 → 400", st == 400, f"HTTP {st} {d}")
        # 管理员看得见全部主题（含别人私有的）→ 允许；越权校验真正要挡的是**普通用户拿别人的主题**
        st, d = adm.post("/api/user/schedule-topics", {"topic_ids": [t1, t3]})
        check("管理员可以用别人的私有主题（管理员可见全部，符合多租户规则）", st == 200, f"HTTP {st} {d}")
        st, d = edB.post("/api/user/schedule-topics", {"topic_ids": [t1]})
        check("★ 普通用户用【公共主题】→ 200", st == 200, f"HTTP {st} {d}")
        # edC 建一个自己的私有主题，让 edB 去拿 → 必须 400
        st, d = adm.post("/api/users", {"username": "edC", "password": "EdC1234567", "role": "editor"})
        edC = Client(); edC.post("/api/auth/login", {"username": "edC", "password": "EdC1234567"})
        st, d = edC.post("/api/topics", {"name": "edC私有主题", "desc": "", "product_id": 1})
        t4 = (d or {}).get("id") if st == 200 else None
        check("edC 建自己的私有主题", st == 200 and t4, f"HTTP {st}")
        st, d = edB.post("/api/user/schedule-topics", {"topic_ids": [t4]})
        check("★ 普通用户拿【别人私有的主题】→ 400（多租户隔离）", st == 400, f"HTTP {st} {d}")
        st, d = edB.post("/api/user/schedule-topics", {"topic_ids": ["abc"]})
        check("非数字 → 400", st == 400, f"HTTP {st} {d}")
        st, d = edB.get("/api/user/settings")
        check("★ 失败的写入没有污染已有配置（edB 仍只有公共主题 t1）",
              ((d or {}).get("schedule_topic_ids") or []) == [t1],
              str((d or {}).get("schedule_topic_ids")))
        # 复位 admin 的定时任务主题，后面的用例从这里接着用
        adm.post("/api/user/schedule-topics", {"topic_ids": []})

        print("\n[E] ★「模型设置」整份保存不能清空定时任务主题")
        st, _ = adm.post("/api/user/schedule-topics", {"topic_ids": [t1, t2]})
        st, d = adm.post("/api/user/settings", {
            "llm_provider": "deepseek", "llm_base_url": "", "llm_model": "", "llm_api_key": "",
            "default_account_id": 0, "schedule_enabled": True, "schedule_time": "09:30",
            "schedule_account_ids": [], "schedule_product_id": 1,
        })
        check("保存模型设置 → 200", st == 200, f"HTTP {st} {d}")
        st, d = adm.get("/api/user/settings")
        ids = (d or {}).get("schedule_topic_ids") or []
        check("★ 整份保存后定时任务主题还在（这是最容易踩的坑：默认 [] 会把用户勾的清空）",
              sorted(ids) == sorted([t1, t2]), str(ids))

        print("\n[F] 体检接口的 schedule 字段")
        st, d = adm.get("/api/system/status")
        sc = (d or {}).get("schedule") or {}
        check("status 返回 schedule", st == 200 and isinstance(sc, dict), f"HTTP {st}")
        check("★ schedule.enabled 反映本人定时开关", sc.get("enabled") is True, str(sc.get("enabled")))
        check("★ schedule.topic_ids 带着定时任务主题", sorted(sc.get("topic_ids") or []) == sorted([t1, t2]),
              str(sc.get("topic_ids")))
        check("★ topics_all=False（已指定主题）", sc.get("topics_all") is False, str(sc.get("topics_all")))

        print("\n[G] 管理员定时总览能看到每人指定的主题")
        st, d = adm.get("/api/admin/user-schedules")
        check("定时总览 → 200", st == 200, f"HTTP {st}")
        row = next((u for u in ((d or {}).get("users") or []) if u.get("username") == "admin"), None)
        check("★ 总览里带 schedule_topic_ids",
              row is not None and sorted(row.get("schedule_topic_ids") or []) == sorted([t1, t2]),
              str((row or {}).get("schedule_topic_ids")))
        # ⚠️ 别用 sorted 比中文（code point 顺序反直觉：乙 U+4E59 < 甲 U+7532）→ 用集合比
        check("★ 总览里带主题名（便于一眼看懂）",
              row is not None and set(row.get("schedule_topic_names") or []) == {"定时任务测试甲", "定时任务测试乙"},
              str((row or {}).get("schedule_topic_names")))

        print("\n[H] 操作日志留痕")
        st, d = adm.get("/api/audit")
        rows = d if isinstance(d, list) else ((d or {}).get("items") or [])
        check("定时任务主题改动有审计记录",
              any("定时任务主题" in json.dumps(r, ensure_ascii=False) for r in rows[:40]),
              str([r.get("action") for r in rows[:6]]))

        print("\n" + "=" * 60)
        ok = sum(1 for _, c, _ in RESULTS if c)
        bad = len(RESULTS) - ok
        print(f"=== 定时任务主题：通过 {ok} / 共 {len(RESULTS)}；失败 {bad} ===")
        return 1 if bad else 0
    finally:
        proc.kill()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
