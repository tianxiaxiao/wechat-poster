/**
 * render_check_users.js — 用户管理排版断言
 * ============================================================================
 * 背景：用户行原先把 8 个元素挤在一行（内容 ~810px 塞进 470px），横向溢出 122~152px，
 *      导致「保存 / 删除」按钮被挤出视野 —— 接口正常但按钮点不到。
 * 本脚本用无头 Chrome 读 DOM/几何，把这件事钉死：
 *   · 面板已放宽（≥600px）
 *   · 列表与每一行都没有横向溢出
 *   · 每行「保存」「删除」按钮都在行内、可见（关键回归点）
 *   · 超长用户名不会把行撑破
 * 临时库 + 独立端口(8791)，不触发真实发布。
 * 运行：node tests/render_check_users.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_USERS_PORT || "8791";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";

let pass = 0, fail = 0;
function check(name, cond, detail = "") {
  if (cond) { pass++; console.log("  ✅ " + name + (detail ? "   [" + detail + "]" : "")); }
  else { fail++; console.log("  ❌ " + name + (detail ? "   [" + detail + "]" : "")); }
}

async function waitUp(t = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < t) {
    try { const r = await fetch(BASE + "/health"); if (r.ok) return true; } catch (e) {}
    await new Promise(r => setTimeout(r, 600));
  }
  return false;
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_rcu_"));
  const db = path.join(tmp, "test.db");
  const today = new Date().toISOString().slice(0, 10);
  execFileSync(PY, ["-c", [
    "import sqlite3, json, sys",
    "con = sqlite3.connect(sys.argv[1])",
    "con.execute('create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)')",
    "con.execute('insert or replace into settings (key, value) values (?, ?)',",
    "            ('daily_schedule', json.dumps({'enabled': False, 'time': '09:30', 'account_id': 0,",
    "                                           'product_id': 0, 'provider': 'deepseek', 'last_run_date': sys.argv[2]})))",
    "con.commit()"
  ].join("\n"), db, today], { stdio: "ignore" });

  const env = { ...process.env, POSTER_DB: db, ADMIN_USERNAME: "admin", ADMIN_INIT_PASSWORD: ADMIN_PW,
                APP_SECRET_KEY: "k", COOKIE_SECURE: "never", PYTHONIOENCODING: "utf-8" };
  const log = fs.createWriteStream(path.join(tmp, "s.log"));
  const proc = spawn(PY, ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", PORT],
    { cwd: APP_DIR, env, stdio: ["ignore", "pipe", "pipe"] });
  proc.stdout.pipe(log); proc.stderr.pipe(log);

  let browser;
  try {
    if (!await waitUp()) {
      console.log("❌ 测试实例未启动：");
      console.log(fs.readFileSync(path.join(tmp, "s.log"), "utf8").slice(-1500));
      process.exit(1);
    }
    browser = await chromium.launch({ executablePath: CHROME, headless: true });
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    page.on("dialog", async d => { await d.accept(); });
    await page.goto(BASE + "/login", { waitUntil: "load" });
    await page.fill("#u", "admin"); await page.fill("#p", ADMIN_PW); await page.click("#btn");
    await page.waitForSelector("#run-form", { timeout: 20000 });

    console.log("\n[A] 造数据：含超长用户名与长姓名");
    // ⚠️ 用户名规则 [A-Za-z0-9_.@-]{3,32}，超 32 位会被 400 拒绝；POST 返回 {ok,user} 无顶层 id，
    //    所以这里按 HTTP 状态判断成功。
    const made = await page.evaluate(async () => {
      const post = async (b) => (await fetch("/api/users", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })).status;
      return [
        await post({ username: "zhangsan_marketing_2026", name: "张三丰（市场部）", password: "Passw0rd123", role: "editor" }),
        await post({ username: "averyverylongusernameforlayout", name: "李四（备用管理员）", password: "Passw0rd123", role: "admin" }),
        await post({ username: "wangxiaoming", name: "王小明", password: "Passw0rd123", role: "viewer" }),
      ];
    });
    check("创建 3 个测试用户（HTTP 200）", made.length === 3 && made.every(c => c === 200), JSON.stringify(made));

    await page.$eval("#users-btn", el => el.click());
    await page.waitForFunction(() => document.querySelectorAll("#users-list [data-uid]").length >= 4, { timeout: 10000 });
    await page.waitForTimeout(600);

    console.log("\n[B] 面板与列表宽度");
    const geo = await page.evaluate(() => {
      const panel = document.querySelector("#users-modal .settings-panel");
      const list = document.querySelector("#users-list");
      const rows = [...document.querySelectorAll("#users-list [data-uid]")];
      const R = (el) => { const r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; };
      return {
        panelW: panel ? Math.round(panel.getBoundingClientRect().width) : 0,
        listOverflow: list ? list.scrollWidth - list.clientWidth : -1,
        rows: rows.map(r => {
          const rb = r.getBoundingClientRect();
          const save = r.querySelector('[data-act="save"]');
          const del = r.querySelector('[data-act="del"]');
          const uname = r.querySelector(".truncate");
          const inRow = (el) => {
            if (!el || el.offsetParent === null) return false;
            const b = el.getBoundingClientRect();
            return b.width > 10 && b.right <= rb.right + 1 && b.left >= rb.left - 1;
          };
          return {
            h: Math.round(rb.height),
            overflow: r.scrollWidth - r.clientWidth,
            saveVisible: inRow(save),
            delVisible: inRow(del),
            nameOverflow: uname ? r.scrollWidth - r.clientWidth : 0,
            childCount: r.children.length,
          };
        }),
      };
    });
    check("面板已放宽（≥600px）", geo.panelW >= 600, `${geo.panelW}px`);
    check("列表无横向溢出", geo.listOverflow <= 0, `溢出 ${geo.listOverflow}px`);
    check("每一行都无横向溢出", geo.rows.every(r => r.overflow <= 0),
          geo.rows.map(r => r.overflow).join(","));
    check("每行都是两行式布局（2 个子块）", geo.rows.every(r => r.childCount === 2),
          geo.rows.map(r => r.childCount).join(","));
    check("每行高度合理（≤100px）", geo.rows.every(r => r.h > 0 && r.h <= 100), geo.rows.map(r => r.h).join(","));

    console.log("\n[C] 【核心】每行「保存 / 删除」按钮都在行内且可见");
    check("所有行的「保存」按钮可见且未被挤出", geo.rows.every(r => r.saveVisible),
          geo.rows.map((r, i) => `${i}:${r.saveVisible ? "ok" : "BAD"}`).join(" "));
    check("所有行的「删除」按钮可见且未被挤出", geo.rows.every(r => r.delVisible),
          geo.rows.map((r, i) => `${i}:${r.delVisible ? "ok" : "BAD"}`).join(" "));

    console.log("\n[D] 超长用户名不撑破行");
    const longRow = await page.evaluate(() => {
      const r = [...document.querySelectorAll("#users-list [data-uid]")]
        .find(x => (x.textContent || "").includes("averyverylongusernameforlayout"));
      if (!r) return null;
      const u = r.querySelector(".truncate");
      const rb = r.getBoundingClientRect();
      return {
        rowOverflow: r.scrollWidth - r.clientWidth,
        unameInsideRow: u ? u.getBoundingClientRect().right <= rb.right + 1 : false,
        unameClipped: u ? u.scrollWidth > u.clientWidth + 1 : false,   // 仅供参考：够宽就完整显示
      };
    });
    check("超长用户名所在行无横向溢出", longRow && longRow.rowOverflow <= 0,
          longRow ? `溢出 ${longRow.rowOverflow}px` : "未找到该行");
    check("超长用户名完整落在行内（未越界；够宽则完整显示，不够则省略号）",
          longRow && longRow.unameInsideRow,
          longRow ? `clipped=${longRow.unameClipped}` : "");

    console.log("\n[E] 功能仍可用：改角色后保存有反馈");
    const saved = await page.evaluate(async () => {
      const r = [...document.querySelectorAll("#users-list [data-uid]")]
        .find(x => (x.textContent || "").includes("wangxiaoming"));
      const sel = r.querySelector('[data-f="role"]');
      sel.value = "editor";
      r.querySelector('[data-act="save"]').click();
      await new Promise(res => setTimeout(res, 1500));
      return (document.querySelector("#nu_msg")?.textContent || "").trim();
    });
    check("保存后页面给出反馈", saved.includes("已保存"), `msg="${saved}"`);

    console.log("\n" + "=".repeat(56));
    console.log(`=== 渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
    process.exitCode = fail === 0 ? 0 : 1;
  } catch (e) {
    console.log("❌ 执行异常：" + (e && e.stack || e));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
    proc.kill();
  }
})();
