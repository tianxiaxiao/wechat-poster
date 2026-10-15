/**
 * inspect_users_layout.js — 用户管理排版诊断（截图 + 量化）
 * 用法: node tests/inspect_users_layout.js [输出目录]
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.INSPECT_USERS_PORT || "8792";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";
const OUT = process.argv[2] || path.resolve(APP_DIR, "..", ".tmp_layout");

async function waitUp(t = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < t) {
    try { const r = await fetch(BASE + "/health"); if (r.ok) return true; } catch (e) {}
    await new Promise(r => setTimeout(r, 600));
  }
  return false;
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_usr_"));
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
    if (!await waitUp()) { console.log("服务未启动"); process.exit(1); }
    browser = await chromium.launch({ executablePath: CHROME, headless: true });
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
    const page = await ctx.newPage();
    page.on("dialog", async d => { await d.accept(); });
    await page.goto(BASE + "/login", { waitUntil: "load" });
    await page.fill("#u", "admin"); await page.fill("#p", ADMIN_PW); await page.click("#btn");
    await page.waitForSelector("#run-form", { timeout: 20000 });

    // 造几个用户：长用户名、中文名、带姓名备注
    await page.evaluate(async () => {
      const post = async (b) => (await fetch("/api/users", { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })).json();
      await post({ username: "zhangsan_marketing_2026", name: "张三丰（市场部）", password: "Passw0rd123", role: "editor" });
      await post({ username: "wangxiaoming", name: "王小明", password: "Passw0rd123", role: "viewer" });
      await post({ username: "li_admin_backup", name: "李四（备用管理员）", password: "Passw0rd123", role: "admin" });
    });

    await page.$eval("#users-btn", el => el.click());
    await page.waitForFunction(() => document.querySelectorAll("#users-list [data-uid]").length >= 4, { timeout: 10000 });
    await page.waitForTimeout(600);

    await page.screenshot({ path: path.join(OUT, "users_01_full.png") });
    const panel = await page.$("#users-modal .settings-panel");
    if (panel) await panel.screenshot({ path: path.join(OUT, "users_02_panel.png") });

    const m = await page.evaluate(() => {
      const panel = document.querySelector("#users-modal .settings-panel");
      const list = document.querySelector("#users-list");
      const rows = [...document.querySelectorAll("#users-list [data-uid]")];
      const R = (el) => { const r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; };
      return {
        panel: panel ? { ...R(panel), sw: panel.scrollWidth, cw: panel.clientWidth } : null,
        list: list ? { ...R(list), sw: list.scrollWidth, cw: list.clientWidth } : null,
        rows: rows.map(r => ({
          ...R(r), sw: r.scrollWidth, cw: r.clientWidth,
          text: (r.textContent || "").replace(/\s+/g, " ").trim().slice(0, 40),
          childCount: r.children.length,
        })),
      };
    });
    console.log(JSON.stringify(m, null, 1));
    console.log("截图目录: " + OUT);
  } catch (e) {
    console.log("异常: " + (e && e.stack || e));
  } finally {
    if (browser) await browser.close().catch(() => {});
    proc.kill();
  }
})();
