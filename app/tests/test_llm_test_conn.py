"""
test_llm_test_conn.py — 「🔌 测试连通」按键的后端验证
================================================================
为什么：设置页新增了一个「测试大模型连通」按键，它必须
  · 用**当前表单里的配置**去测（未填的字段回退到该用户已保存的设置）
  · 回显的地址/模型与**真正生成时用的**完全一致（否则用户会被误导）
  · 成功/失败都要给出人话提示；失败时不能只说"失败"
  · 绝不回显 API Key 明文
本脚本用**进程内的假 LLM 服务**把成功与失败两条路都真实跑通（不依赖外网、不定时）。
临时库 + 独立端口(8798)，不碰线上数据。
运行：python tests/test_llm_test_conn.py
"""
import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_PW = "TestAdmin123"
CANARY = "sk-LEAKCANARY-should-never-appear"
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


# ---------- 进程内假 LLM（OpenAI 兼容） ----------
class FakeLLM(BaseHTTPRequestHandler):
    """POST /v1/chat/completions        → 200 正常补全
       POST /authfail/chat/completions  → 401 模拟 Key 无效"""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        if self.path.startswith("/authfail"):
            body = json.dumps({"error": {"message": "Incorrect API key provided",
                                         "type": "invalid_request_error"}}).encode()
            self.send_response(401)
        else:
            body = json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                             "finish_reason": "stop"}],
            }).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


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
    fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
    fake_port = fake.server_address[1]
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_url = f"http://127.0.0.1:{fake_port}/v1"
    authfail_url = f"http://127.0.0.1:{fake_port}/authfail"
    dead_url = f"http://127.0.0.1:{free_port()}/v1"      # 无人监听 → 连接被拒

    tmp = Path(tempfile.mkdtemp(prefix="poster_llmt_"))
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

        print(f"（假 LLM: {fake_url}）")
        anon = Client()
        adm = Client()
        st, _, _ = adm.post("/api/auth/login", {"username": "admin", "password": ADMIN_PW})
        check("管理员登录", st == 200, f"HTTP {st}")

        print("\n[1] 权限与参数校验")
        st, d, _ = anon.post("/api/llm/test", {"provider": "deepseek"})
        check("未登录测试 → 401", st == 401, f"HTTP {st}")
        st, d, _ = adm.post("/api/llm/test", {"provider": "not-a-provider"})
        check("非法 provider → 400", st == 400, f"HTTP {st} {d}")

        print("\n[2] 连通成功（假 LLM 在线）")
        st, d, raw = adm.post("/api/llm/test", {"provider": "openai_compat", "base_url": fake_url,
                                               "model": "my-test-model", "api_key": CANARY})
        check("openai_compat 探测 → 200 且 ok=True", st == 200 and d.get("ok") is True,
              f"HTTP {st} ok={d.get('ok')} hint={d.get('hint')} err={str(d.get('error'))[:80]}")
        check("回显实际使用的模型名", d.get("model") == "my-test-model", str(d.get("model")))
        check("回显实际请求的 base_url（= 表单填的）", d.get("base_url") == fake_url, str(d.get("base_url")))
        check("ok 时有耗时字段且 >=0", isinstance(d.get("elapsed_ms"), int) and d["elapsed_ms"] >= 0,
              f"{d.get('elapsed_ms')}ms")
        check("ok 时 hint 为人话", "连通" in str(d.get("hint")), str(d.get("hint")))
        check("ok 时 error 为空", not d.get("error"), str(d.get("error"))[:60])
        check("★ 响应里不含 API Key 明文", CANARY not in raw, "canary 已脱敏")
        check("★ 只回显 Key 来源不回显 Key",
              d.get("key_source") in ("你填写的", "你自己的 Key", "服务端环境变量", "未设置", "占位(ollama)",
                                      "管理员共享", "管理员共享（服务端默认）", "不需要（本地/自建模型）"),
              f"key_source={d.get('key_source')}")

        print("\n[3] 鉴权失败（假 LLM 返回 401）")
        st, d, _ = adm.post("/api/llm/test", {"provider": "openai_compat", "base_url": authfail_url,
                                             "model": "m", "api_key": "sk-wrong"})
        check("401 → ok=False", st == 200 and d.get("ok") is False, f"HTTP {st} ok={d.get('ok')}")
        check("★ 提示明确指出是鉴权/Key 问题", "鉴权" in str(d.get("hint")), str(d.get("hint")))
        check("失败时保留底层错误摘要（便于排查）", bool(str(d.get("error") or "").strip()),
              str(d.get("error"))[:70])

        print("\n[4] 连不上（端口无人监听）")
        st, d, _ = adm.post("/api/llm/test", {"provider": "openai_compat", "base_url": dead_url,
                                             "model": "m", "api_key": "sk-x"})
        check("连接被拒 → ok=False", st == 200 and d.get("ok") is False, f"HTTP {st} ok={d.get('ok')}")
        check("★ 提示为人话且非空", bool(str(d.get("hint") or "").strip()) and len(str(d.get("hint"))) >= 4,
              str(d.get("hint")))
        check("失败也回显了目标地址（知道在测哪里）", d.get("base_url") == dead_url, str(d.get("base_url")))

        print("\n[5] ollama 通道（走 SDK 分支）")
        st, d, raw = adm.post("/api/llm/test", {"provider": "ollama", "base_url": fake_url,
                                               "model": "qwen-test", "api_key": CANARY})
        check("ollama 探测 → ok=True", st == 200 and d.get("ok") is True,
              f"HTTP {st} ok={d.get('ok')} err={str(d.get('error'))[:80]}")
        check("ollama 回显 is_local=True", d.get("is_local") is True, str(d.get("is_local")))
        check("★ ollama 响应也不含 Key 明文", CANARY not in raw, "canary 已脱敏")

        print("\n[6] 未传参时回退到「该用户已保存的设置」")
        st, _, _ = adm.post("/api/user/settings", {
            "llm_provider": "openai_compat", "llm_base_url": fake_url,
            "llm_model": "saved-model", "llm_api_key": "",
            "schedule_enabled": False, "schedule_time": "10:00",
            "schedule_account_ids": [], "schedule_product_id": 0,
        })
        check("先保存个人设置（provider 指向假 LLM）", st == 200, f"HTTP {st}")
        st, d, _ = adm.post("/api/llm/test", {})
        check("★ 空 body 测试 → 用已保存的配置且 ok=True", st == 200 and d.get("ok") is True,
              f"HTTP {st} ok={d.get('ok')} err={str(d.get('error'))[:70]}")
        check("★ 回显用的是已保存的 base_url", d.get("base_url") == fake_url, str(d.get("base_url")))
        check("★ 回显用的是已保存的 model", d.get("model") == "saved-model", str(d.get("model")))
        check("provider 回退为已保存的 openai_compat", d.get("provider") == "openai_compat", str(d.get("provider")))

        print("\n[7] 操作留痕")
        st, d, _ = adm.get("/api/audit?limit=80")
        rows = d.get("items") if isinstance(d, dict) else d
        acts = [r.get("action") for r in (rows or [])]
        check("操作日志含 llm.test 记录", "llm.test" in acts, f"最近动作={acts[:6]}")
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            fake.shutdown()
        except Exception:
            pass

    ok = sum(1 for _, c, _ in RESULTS if c)
    print("\n" + "=" * 60)
    print(f"=== 测试连通接口：通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
