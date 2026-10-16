"""
test_ui_main_page.py — 主界面 UI 渲染断言（隔离实例，不碰线上数据）
====================================================================
用途：前端改动后自动核对「页头单行 / 模型只显示来源 / 改密码入口在用户管理」等，
避免只改代码不验证渲染结果（改完 CSS/HTML 但页面显示错的坑踩过多次）。

运行：python tests/test_ui_main_page.py
说明：用临时数据库在 8798 端口起独立实例，自己创建管理员并登录，
      浏览器（playwright-core + 本地 Chrome）里读真实计算样式与元素位置。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
WXCARD = APP_DIR / "wxcard"
PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
ADMIN_PW = "UiTest12345"
NODE = None
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


def find_node():
    import shutil as sh
    env = (os.getenv("WX_NODE") or "").strip()
    if env and Path(env).exists():
        return env
    try:
        sys.path.insert(0, str(APP_DIR))
        import wxcard
        return str(wxcard.find_node())
    except Exception:
        return sh.which("node")


def wait_up(port, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.6)
    return False


JS_MAIN = """
(() => {
  const row = document.querySelector('header > div');            // 页头第一排
  const h1 = row.querySelector('h1');
  const tag = row.querySelector('#provider-tag');
  const kids = [...row.children];
  const center = e => { const r = e.getBoundingClientRect(); return r.top + r.height / 2; };
  const centers = kids.map(center);
  // 单行判定：用 Range 量标题文字实际占了几行（比 line-height 可靠）
  const rng = document.createRange(); rng.selectNodeContents(h1.firstChild);
  const lines = [...rng.getClientRects()].filter(r => r.height > 1).length;
  return {
    titleFont: getComputedStyle(h1).fontSize,
    titleLines: lines,
    rowCentersSpread: Math.round(Math.max(...centers) - Math.min(...centers)),
    providerText: tag.textContent.trim(),
    providerHasModel: /deepseek-chat|gpt-|qwen|glm|claude|kimi|doubao/i.test(tag.textContent),
    // 只看页头第一排的按钮（第二排是功能入口，不算）
    // 2026-10-16：#health-badge（状态徽章）与 #sched-tag（定时任务标记）都是**状态指示**不是导航按钮，
    // 统计"头部还剩几个导航按钮"时必须排除，否则新增一个状态徽章就会把这个断言打挂。
    firstRowButtons: [...row.querySelectorAll('button')]
      .filter(b => b.id !== 'health-badge' && b.id !== 'sched-tag')
      .map(b => b.textContent.trim()).filter(Boolean),
    // 2026-10-16：标题行新增「能否生成/发布」徽章，必须排在模型标签**之后**
    healthAfterTag: (() => {
      const b = document.getElementById('health-badge');
      if (!b || !tag) return null;
      return b.getBoundingClientRect().left >= tag.getBoundingClientRect().right - 2;
    })(),
    healthText: (document.getElementById('health-badge') || {}).textContent || '',
    // 2026-10-16：「🕒 定时任务」标记：没开定时时必须**整块不显示**
    schedTag: (() => {
      const s = document.getElementById('sched-tag');
      if (!s) return null;
      return { hidden: s.classList.contains('hidden'), text: s.textContent.trim() };
    })(),
    pwFormInUsers: !!document.querySelector('#users-modal #pw-btn'),
    notifyDefaultChecked: !!document.querySelector('#notify_me')?.checked,
    notifyInForm: !!document.querySelector('#run-form #notify_me'),
    pwFormInHeader: !!row.querySelector('#pw-btn'),
    curUser: document.querySelector('#cur-user')?.textContent.trim() || ''
  };
})()
"""


def main():
    node = find_node()
    if not node:
        print("❌ 找不到 node，无法做 UI 断言")
        return 1
    try:
        from playwright.sync_api import sync_playwright        # noqa
    except Exception:
        print("⚠️ 未安装 playwright(python)，改用 node 版 playwright-core 执行浏览器断言")
        return run_with_node_playwright(node)

    return run_with_node_playwright(node)


def run_with_node_playwright(node):
    """用 wxcard/node_modules 里的 playwright-core（本机既有），通过 node 脚本执行断言"""
    tmp = Path(tempfile.mkdtemp(prefix="poster_uitest_"))
    db = tmp / "ui.db"
    con = sqlite3.connect(db)
    con.execute("create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)")
    con.execute("insert or replace into settings (key, value) values (?, ?)",
                ("daily_schedule", json.dumps({"enabled": False, "time": "09:30", "account_id": 0,
                                               "product_id": 0, "provider": "deepseek",
                                               "last_run_date": time.strftime("%Y-%m-%d")})))
    con.commit(); con.close()
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # 故意不传 ADMIN_INIT_PASSWORD：让系统随机生成初始密码，
    # 这样能顺带验证「初始密码登录 → 强制改密弹层自动弹出」这条链路
    env.update({"POSTER_DB": str(db), "ADMIN_USERNAME": "admin",
                "APP_SECRET_KEY": "ui-test-secret", "COOKIE_SECURE": "never"})
    log = open(tmp / "server.log", "wb")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1",
                             "--port", str(PORT)], cwd=str(APP_DIR), env=env, stdout=log,
                            stderr=subprocess.STDOUT)
    try:
        if not wait_up(PORT):
            print("❌ 隔离实例未启动：")
            print((tmp / "server.log").read_text(encoding="utf-8", errors="ignore")[-2000:])
            return 1
        pwfile = db.parent / "管理员初始密码.txt"
        for _ in range(30):
            if pwfile.exists():
                break
            time.sleep(0.5)
        admin_pw = ""
        if pwfile.exists():
            for line in pwfile.read_text(encoding="utf-8").splitlines():
                if "初始密码" in line:
                    admin_pw = line.split("：", 1)[-1].strip()
        if not admin_pw:
            print("❌ 未取到初始管理员密码（文件未生成）")
            return 1
        shot_users = (WXCARD / "ui_users.png").as_posix()
        shot_header = (WXCARD / "ui_header_row.png").as_posix()
        script = tmp / "ui_check.js"
        script.write_text(f"""
const {{ chromium }} = require('playwright-core');
(async () => {{
  const b = await chromium.launch({{ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true }});
  const ctx = await b.newContext({{ viewport: {{ width: 1060, height: 620 }}, deviceScaleFactor: 2 }});
  const p = await ctx.newPage();
  const errs = []; p.on('pageerror', e => errs.push(String(e).slice(0,150)));
  // 先用 API 登录（拿到会话 Cookie，浏览器上下文会自动带上），比填表单稳
  const lr = await p.request.post('{BASE}/api/auth/login', {{
      data: {{ username: 'admin', password: '{admin_pw}' }} }});
  const loginStatus = lr.status();
  await p.goto('{BASE}/', {{ waitUntil: 'load' }});
  await p.waitForTimeout(1500);
  const diag = {{ loginStatus, url: p.url(), hasPwModal: await p.evaluate(() => !!document.querySelector('#pw-modal')) }};
  if (!diag.hasPwModal) {{ console.log(JSON.stringify({{ ...diag, fatal: '主页面未加载到（弹层缺失）' }})); await b.close(); return; }}
  // 初始密码登录 → 强制改密弹层应自动出现；关掉它继续做布局断言
  const forced = await p.evaluate(() => !document.querySelector('#pw-modal').classList.contains('hidden'));
  if (forced) {{ await p.click('#pw-modal .modal-x'); await p.waitForTimeout(400); }}
  const r = await p.evaluate({json.dumps(JS_MAIN)});
  r.forcedPwModal = forced;
  await p.click('#users-btn'); await p.waitForTimeout(1000);
  const inUsers = await p.evaluate(() => document.querySelector('#users-modal').innerText.includes('修改我的密码'));
  await p.click('#pw-btn'); await p.waitForTimeout(500);
  const opened = await p.evaluate(() => !document.querySelector('#pw-modal').classList.contains('hidden')
                                       && document.querySelector('#users-modal').classList.contains('hidden'));
  await p.click('#pw-modal .modal-x'); await p.waitForTimeout(300);
  await p.click('#users-btn'); await p.waitForTimeout(900);
  await p.screenshot({{ path: '{shot_users}' }});
  await p.click('#users-modal .modal-x'); await p.waitForTimeout(300);
  await p.screenshot({{ path: '{shot_header}', clip: {{ x: 0, y: 0, width: 1060, height: 130 }} }});

  // [7] 复现用户场景：老浏览器 localStorage 里已存 notify_me:false → 应被一次性迁移成「勾选」
  const ctx2 = await b.newContext({{ viewport: {{ width: 1060, height: 620 }} }});
  await ctx2.addInitScript(() => {{
    try {{ localStorage.setItem('bias_publish_llm_v1', JSON.stringify({{ notify_me: false, provider: 'deepseek' }})); }} catch (e) {{}}
  }});
  const p2 = await ctx2.newPage();
  await ctx2.request.post('{BASE}/api/auth/login', {{ data: {{ username: 'admin', password: '{admin_pw}' }} }});
  await p2.goto('{BASE}/', {{ waitUntil: 'load' }});
  await p2.waitForTimeout(1200);
  const mig = await p2.evaluate(() => {{
    const st = JSON.parse(localStorage.getItem('bias_publish_llm_v1') || '{{}}');
    return {{ checked: !!document.querySelector('#notify_me')?.checked, marker: st.notify_pref_v1 === true }};
  }});
  console.log(JSON.stringify({{ ...r, inUsers, entryOpensModal: opened, errs, loginStatus, mig }}));
  await b.close();
}})();
""", encoding="utf-8")
        out = subprocess.run([node, str(script)], capture_output=True, text=True,
                             cwd=str(app_dir_for_playwright()), timeout=180,
                             env=dict(os.environ, NODE_PATH=str(PLAYWRIGHT_NODE_MODULES)))
        raw = (out.stdout or "").strip().splitlines()
        if not raw:
            print("❌ 浏览器脚本无输出：", (out.stderr or "")[-800:])
            return 1
        d = json.loads(raw[-1])
        if d.get("fatal"):
            print("❌", d)
            return 1

        print("\n[1] 页头一排显示")
        check("标题单行显示（未换行）", d.get("titleLines") == 1, f"占 {d.get('titleLines')} 行，字号 {d.get('titleFont')}")
        _sp = d.get("rowCentersSpread")
        check("页头各元素垂直居中同一行", _sp is not None and _sp <= 10,
              f"中心差 {_sp}px（0 表示完全对齐）")
        check("标题字号已适配", d.get("titleFont") == "20px", d.get("titleFont", ""))

        print("\n[2] 模型只显示来源")
        check("模型标签显示来源", "DeepSeek" in (d.get("providerText") or ""), d.get("providerText", ""))
        check("模型标签不含模型名", not d.get("providerHasModel"), d.get("providerText", ""))

        print("\n[3] 主界面只留退出按钮")
        btns = d.get("firstRowButtons") or []
        # 2026-10-16：标题行新增了「能否生成/发布」的状态徽章（#health-badge），
        # 它是**状态指示**不是导航按钮，这里把它排除后再断言"只剩退出"。
        nav_btns = [b for b in btns if "体检" not in b and "状态" not in b and b != "⚪ 体检中…"]
        check("头部导航按钮只剩「退出」", nav_btns == ["退出"], str(btns))
        check("★ 标题行有生成/发布状态徽章（模型标签之后）",
              bool(d.get("healthText")) and d.get("healthAfterTag") is True,
              f"health={d.get('healthText')!r} afterTag={d.get('healthAfterTag')}")
        # 2026-10-16：新账号默认没开定时 ⇒ 「🕒 定时任务」标记必须**不显示**（避免"看着有其实没有"）
        _st = d.get("schedTag") or {}
        check("★ 未开启定时时「🕒 定时任务」标记不显示", _st.get("hidden") is True, str(_st))
        check("头部没有改密码按钮", not d.get("pwFormInHeader"), "")
        check("显示当前登录用户", "管理员" in (d.get("curUser") or ""), d.get("curUser", ""))

        print("\n[4] 改密码已移入用户管理")
        check("用户管理内有「修改我的密码」", d.get("inUsers") and d.get("pwFormInUsers"), "")
        check("点该入口会打开改密表单", d.get("entryOpensModal"), "")
        check("强制改密弹层仍会在初始密码登录时弹出", d.get("forcedPwModal"), "")

        print("\n[5] 手动发布的通知开关")
        check("「完成后微信通知」默认勾选（首次使用）", d.get("notifyDefaultChecked"), "")
        check("通知开关在发布表单里", d.get("notifyInForm"), "")
        _mig = d.get("mig") or {}
        check("老浏览器存着的 notify_me:false 会被迁移为勾选", _mig.get("checked"), str(_mig))
        check("迁移后打上 notify_pref_v1 标记（之后尊重用户选择）", _mig.get("marker"), "")

        print("\n[6] 页面无脚本报错")
        check("无 JS 异常", not d.get("errs"), str(d.get("errs"))[:120])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    ok = sum(1 for _, c, _ in RESULTS if c)
    print("\n" + "=" * 52)
    print(f"=== 通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


def app_dir_for_playwright():
    return WXCARD


PLAYWRIGHT_NODE_MODULES = Path(r"D:\测试公众号批量生成\wechat\node_modules")


if __name__ == "__main__":
    sys.exit(main())
