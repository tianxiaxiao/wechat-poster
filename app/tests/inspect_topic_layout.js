/**
 * inspect_topic_layout.js — 主题库排版诊断（截图 + 量化）
 * 用法: node tests/inspect_topic_layout.js [输出目录]
 * 造一批长短不一的主题，打开「📚 主题库」截图，并打印每行的实际尺寸/是否溢出。
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.INSPECT_PORT || "8793";
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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_insp_"));
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

    // 造数据：长短不一 + 不同归属 + 有/无备注
    await page.evaluate(async () => {
      const post = async (u, b) => (await fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })).json();
      await post("/api/products", { name: "百秘一梳", intro: "功效木梳", style: "" });
      await post("/api/products", { name: "重庆美食街", intro: "餐饮", style: "" });
      const ps = await (await fetch("/api/products")).json();
      const id1 = ps[0].id;
      const rows = [
        ["短", "有备注", id1],
        ["中等长度主题名称", "备注一句话说明", 0],
        ["特别长的主题名称测试排版是否会被挤乱啊啊啊啊啊", "这是一段比较长的备注文字用来测试截断效果", id1],
        ["鼻炎", "", 0],
        ["送礼场景（长辈/领导/同事/朋友通用）", "节日送礼", id1],
      ];
      for (const [n, d, p] of rows) await post("/api/topics", { name: n, desc: d, product_id: p });
    });

    await page.$eval("#topic-btn", el => el.click());
    await page.waitForFunction(() => document.querySelectorAll("#topic-list-mgr [id^=row-topic-]").length >= 5, { timeout: 10000 });
    await page.waitForTimeout(800);

    // 截图：整个弹层 + 列表区域
    await page.screenshot({ path: path.join(OUT, "01_full.png") });
    const panel = await page.$("#topic-modal .settings-panel");
    if (panel) await panel.screenshot({ path: path.join(OUT, "02_panel.png") });
    const list = await page.$("#topic-list-mgr");
    if (list) await list.screenshot({ path: path.join(OUT, "03_list.png") });

    // 量化每行
    const metrics = await page.evaluate(() => {
      const panel = document.querySelector("#topic-modal .settings-panel");
      const rows = [...document.querySelectorAll("#topic-list-mgr [id^=row-topic-]")];
      const rect = (el) => { const r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x), y: Math.round(r.y) }; };
      return {
        panelWidth: panel ? Math.round(panel.getBoundingClientRect().width) : 0,
        panelScroll: panel ? { sw: panel.scrollWidth, cw: panel.clientWidth, sh: panel.scrollHeight, ch: panel.clientHeight } : null,
        rows: rows.map(r => {
          const nameEl = r.querySelector(".text-sm.font-medium");
          const descEl = r.querySelector(".text-xs.text-slate-500");
          const cb = r.querySelector(".topic-row-cb");
          const btns = r.querySelectorAll("button");
          return {
            row: rect(r),
            rowSW: r.scrollWidth, rowCW: r.clientWidth,
            name: nameEl ? { ...rect(nameEl), text: nameEl.textContent.trim() } : null,
            // 名称行是否折行：高度 / 行高
            nameLines: nameEl ? Math.round(nameEl.getBoundingClientRect().height / 20) : 0,
            desc: descEl ? { ...rect(descEl), text: (descEl.textContent || "").trim().slice(0, 20) } : null,
            cb: cb ? rect(cb) : null,
            btnCount: btns.length,
          };
        }),
      };
    });
    console.log(JSON.stringify(metrics, null, 1));
    console.log("截图目录: " + OUT);
  } catch (e) {
    console.log("异常: " + (e && e.stack || e));
  } finally {
    if (browser) await browser.close().catch(() => {});
    proc.kill();
  }
})();
