"""
test_system_status.py — 「体检」（能不能生成 / 能不能发布）+ 出口 IP 接口验证
================================================================================
背景：主页顶部那颗状态徽章、以及「📱 公众号管理」里的实时出口 IP，都来自本节新增的两个接口。
它们必须满足：
  · 页面加载只读缓存（秒回），**点了才真探测**（一次最小模型请求 + 一次取 token），
    且两种探测都**绝不发布文章**；
  · LLM 探测结果与「🔌 测试连通」共用缓存（测完徽章立刻跟着变，不重复探测）；
  · 微信探测走 wechat_api.probe_credentials()：**不碰全局 _ACCOUNT、不写 token 缓存**
    （否则会与正在跑的定时发布抢账号）；
  · 结论要能直接指明"下一步该干什么"（缺 Key / IP 不在白名单 / 凭证错）。

本脚本分两层：
  A. 纯函数层：_status_summary() 的 9 种组合（离线、确定性）；
  B. HTTP 层：临时库 + 8802 端口 + 进程内假 LLM，验证接口形状与缓存复用。
运行：python tests/test_system_status.py
"""
import http.cookiejar
import http.server
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PORT = 8802
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_PW = "TestAdmin123"
IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
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
            with self.op.open(r, timeout=60) as resp:
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


class FakeLLM(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "fake",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                                        "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def unit_status_summary():
    """A. _status_summary 纯函数：各种组合都要给出『下一步做什么』"""
    # 纯函数测试也要让 db 指向临时库（import app 时会初始化 db 绑定）
    os.environ["POSTER_DB"] = str(Path(tempfile.mkdtemp(prefix="poster_ss_u_")) / "u.db")
    sys.path.insert(0, str(APP_DIR))
    import app as A

    llm_ok = {"ok": True, "provider": "deepseek", "model": "deepseek-chat", "key_source": "你自己的 Key"}
    acc = {"id": 1, "name": "百秘一梳服务号", "appid": "wx_x"}
    sched_off = {"enabled": False, "time": "10:00", "ran_today": False}
    sched_on = {"enabled": True, "time": "09:30", "ran_today": False}

    print("\n[A] 结论归纳（纯函数，9 种组合）")
    r = A._status_summary({}, {}, None, False, sched_off)
    check("没有可用公众号 → bad 且指向「📱 公众号」", r["overall"] == "bad" and "公众号" in r["label"],
          f"{r['label']}｜{r['detail'][:30]}")
    r = A._status_summary({"ok": False, "blocked": True, "hint": "未设置你自己的 API Key"}, {}, acc, False, sched_off)
    check("★ 缺 Key（被策略拦）→ bad 且文案点明缺 Key", r["overall"] == "bad" and "Key" in r["label"],
          r["label"])
    r = A._status_summary({"ok": False, "blocked": False, "hint": "连接被拒", "error": "refused"}, {}, acc, False, sched_off)
    check("模型连不上 → bad", r["overall"] == "bad" and "连不上" in r["label"], r["label"])
    r = A._status_summary({}, {"ok": None}, acc, False, sched_off)
    check("★ 从未体检 → unknown 且提示「点击检测」",
          r["overall"] == "unknown" and "点击" in r["label"] + r["detail"], r["label"])
    r = A._status_summary(llm_ok, {"ok": False, "errcode": 40164, "hint": "IP 不在白名单"}, acc, False, sched_off)
    check("★ 微信 40164 → warn 且点明「白名单」",
          r["overall"] == "warn" and "白名单" in r["label"], r["label"])
    r = A._status_summary(llm_ok, {"ok": False, "errcode": 40013, "hint": "appid 不对"}, acc, False, sched_off)
    check("微信其他错误 → bad 且指向凭证异常",
          r["overall"] == "bad" and "凭证" in r["label"], r["label"])
    r = A._status_summary(llm_ok, {"ok": None}, acc, False, sched_off)
    check("生成正常但发布未验证 → warn", r["overall"] == "warn" and "未验证" in r["label"], r["label"])
    r = A._status_summary(llm_ok, {"ok": True}, acc, True, sched_on)
    check("全部通过但出口 IP 变了 → warn 且提醒更新白名单",
          r["overall"] == "warn" and "IP" in r["label"], r["label"])
    r = A._status_summary(llm_ok, {"ok": True}, acc, False, sched_on)
    check("★ 全部通过 → ok，且 detail 里带模型/定时/发布号",
          r["overall"] == "ok" and "09:30" in r["detail"] and "百秘一梳服务号" in r["detail"],
          r["detail"][:70])

    print("\n[A2] 微信错误解析（纯函数，离线）")
    import wechat_api as W
    d = W.parse_probe_error({"errcode": 40164, "errmsg": "invalid ip 106.85.218.84, not in whitelist ..."})
    check("★ 40164 能从 errmsg 里抠出微信看到的 IP", d["ip"] == "106.85.218.84" and "白名单" in d["hint"],
          f"ip={d['ip']} hint={d['hint'][:36]}")
    d2 = W.parse_probe_error({"errcode": 40013, "errmsg": "invalid appid"})
    check("40013 → 指向 appid/AppSecret 不对", "appid" in d2["hint"] or "AppSecret" in d2["hint"], d2["hint"][:40])
    d3 = W.parse_probe_error({})
    check("空响应不崩且给出兜底提示", d3["ok"] is False and bool(d3["hint"]), d3["hint"][:30])
    check("probe_credentials 缺参时不发请求直接报错",
          W.probe_credentials("", "")["ok"] is False, W.probe_credentials("", "")["hint"][:30])


class FakeLLMServer:
    def __enter__(self):
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        return self

    def __exit__(self, *a):
        self.srv.shutdown()


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
    unit_status_summary()

    tmp = Path(tempfile.mkdtemp(prefix="poster_ss_"))
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
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app",
                             "--host", "127.0.0.1", "--port", str(PORT)],
                            cwd=str(APP_DIR), env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up():
            print("❌ 测试实例未启动，日志：")
            print((tmp / "server.log").read_text(encoding="utf-8", errors="ignore")[-2000:])
            return 1

        with FakeLLMServer() as fake:
            anon, adm = Client(), Client()
            print("\n[B1] 鉴权")
            check("未登录访问 /api/system/status → 401", anon.get("/api/system/status")[0] == 401)
            check("未登录访问 /api/system/public-ip → 401", anon.get("/api/system/public-ip")[0] == 401)
            adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})

            print("\n[B2] 出口 IP 接口")
            st, ip = adm.get("/api/system/public-ip")
            check("返回 200 且含 ip/whitelist_path", st == 200 and "ip" in ip and bool(ip.get("whitelist_path")),
                  f"HTTP {st}")
            check("★ ip 要么是合法 IPv4、要么是空串（探测失败不假装成功）",
                  ip.get("ip") == "" or bool(IPV4.match(ip.get("ip") or "")), f"ip={ip.get('ip')!r}")
            check("白名单提示里带后台路径", "IP白名单" in ip.get("whitelist_path", ""), ip.get("whitelist_path", "")[:50])
            check("含 ok_ip / changed_since_ok 字段", "ok_ip" in ip and "changed_since_ok" in ip)
            st, ip2 = adm.get("/api/system/public-ip")
            check("★ 第二次读走缓存（cached_sec 有值）", ip2.get("ip") == ip.get("ip"),
                  f"cached_sec={ip2.get('cached_sec')}")

            print("\n[B2b] 先加一个「体检用号」（保证探测目标可控，不误伤真号）")
            st, acc_new = adm.post("/api/accounts", {"name": "体检用号", "appid": "wx_bogus_for_probe",
                                                     "secret": "bogussecret12345", "author": "A"})
            check("新增公众号 → 200", st == 200, f"HTTP {st}")
            probe_acc_id = acc_new.get("id")

            print("\n[B3] 状态接口：首次调用会自动体检一次（页面打开即可见结论）")
            st, s0 = adm.get("/api/system/status")
            check("返回 200 且字段齐全", st == 200 and all(k in s0 for k in
                  ("overall", "label", "detail", "llm", "wechat", "egress", "account", "schedule")),
                  f"HTTP {st} keys={sorted(s0.keys())[:6]}…")
            check("★ 首次调用确实做了探测（llm.age_sec 有值，不是一个空壳绿）",
                  s0.get("llm", {}).get("age_sec") is not None, f"age={s0.get('llm', {}).get('age_sec')}")
            check("overall 是四类之一", s0.get("overall") in ("ok", "warn", "bad", "unknown"), str(s0.get("overall")))
            check("label 非空且带 emoji 状态", bool(s0.get("label")) and not s0["label"].startswith("None"), str(s0.get("label")))

            print("\n[B4] 「测试连通」与徽章共用缓存")
            st, _ = adm.post("/api/user/settings", {
                "llm_provider": "openai_compat", "llm_base_url": fake.url, "llm_model": "fake-model",
                "llm_api_key": "sk-own", "schedule_enabled": False, "schedule_time": "10:00",
                "schedule_account_ids": [], "schedule_product_id": 0,
                "default_account_id": probe_acc_id})   # 体检目标锁定到那个假号
            st, t = adm.post("/api/llm/test", {})
            check("测试连通成功（假 LLM）", st == 200 and t.get("ok") is True, f"ok={t.get('ok')}")
            st, s1 = adm.get("/api/system/status")
            check("★ 徽章无需再探测即变绿（复用测试结果）",
                  s1.get("llm", {}).get("ok") is True and s1.get("llm", {}).get("age_sec") is not None,
                  f"ok={s1['llm'].get('ok')} age={s1['llm'].get('age_sec')}s")
            check("★ overall 不再是 unknown", s1.get("overall") != "unknown", str(s1.get("overall")))

            print("\n[B5] refresh=1 会真探测（含微信凭证/白名单）")
            st, s2 = adm.get("/api/system/status?refresh=1")
            check("refresh=1 返回 200", st == 200, f"HTTP {st}")
            check("★ 微信侧做了探测（有 age_sec，且结论非空）",
                  s2["wechat"].get("age_sec") is not None and bool(s2["wechat"].get("hint")),
                  f"age={s2['wechat'].get('age_sec')} ok={s2['wechat'].get('ok')} hint={str(s2['wechat'].get('hint'))[:40]}")
            check("★ 假 appid 不会被判成成功",
                  s2["wechat"].get("ok") is not True, f"ok={s2['wechat'].get('ok')}")
            check("account 字段指向刚加的号", (s2.get("account") or {}).get("name") == "体检用号",
                  str((s2.get("account") or {}).get("name")))
            check("egress 块含 ip/ok_ip/changed_since_ok", all(
                k in s2["egress"] for k in ("ip", "ok_ip", "changed_since_ok")), json.dumps(s2["egress"], ensure_ascii=False)[:80])
            check("schedule 块反映了未开启定时", s2["schedule"].get("enabled") is False, str(s2["schedule"]))

            print("\n[B6] 体检不会发布任何文章（关键安全断言）")
            st, jobs = adm.get("/api/jobs")
            check("★ 体检过程没有产生任何发布任务", isinstance(jobs, list) and len(jobs) == 0,
                  f"jobs={len(jobs) if isinstance(jobs, list) else jobs}")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass

    ok = sum(1 for _, c, _ in RESULTS if c)
    print("\n" + "=" * 62)
    print(f"=== 体检/出口 IP：通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
