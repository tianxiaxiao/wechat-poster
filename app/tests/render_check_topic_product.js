/**
 * render_check_topic_product.js — 「主题随产品过滤」渲染断言
 * ============================================================================
 * 为什么：接口返回正确 ≠ 页面显示正确。本脚本用无头 Chrome 真实登录，读 DOM 断言：
 *   · 选产品 A → 主题 chips 只出现【A 专属 + 通用】，不出现 B 专属（★核心需求）
 *   · 切到产品 B → 列表随切换而变（A 专属消失、B 专属出现）
 *   · 顶部范围提示显示当前产品名
 *   · 主题库管理页显示每条主题的归属徽章（产品名 / 通用）
 * 临时库 + 独立端口(8794)，全局定时关闭，不触发真实发布。
 * 运行：node tests/render_check_topic_product.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT2 || "8794";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";

let pass = 0, fail = 0;
function check(name, cond, detail = "") {
  if (cond) { pass++; console.log("  ✅ " + name + (detail ? "   [" + detail + "]" : "")); }
  else { fail++; console.log("  ❌ " + name + (detail ? "   [" + detail + "]" : "")); }
}

async function waitUp(timeoutMs = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try { const r = await fetch(BASE + "/health"); if (r.ok) return true; } catch (e) { /* not yet */ }
    await new Promise(r => setTimeout(r, 600));
  }
  return false;
}

async function login(page, user, pw) {
  await page.goto(BASE + "/login", { waitUntil: "load" });
  await page.fill("#u", user);
  await page.fill("#p", pw);
  await page.click("#btn");
  await page.waitForSelector("#run-form", { timeout: 20000 });
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_rtp_"));
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
                APP_SECRET_KEY: "test-secret-key-for-render-check", COOKIE_SECURE: "never",
                PYTHONIOENCODING: "utf-8" };
  const log = fs.createWriteStream(path.join(tmp, "server.log"));
  const proc = spawn(PY, ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", PORT],
    { cwd: APP_DIR, env, stdio: ["ignore", "pipe", "pipe"] });
  proc.stdout.pipe(log); proc.stderr.pipe(log);

  let browser;
  try {
    if (!await waitUp()) {
      console.log("❌ 测试实例未启动，日志：");
      console.log(fs.readFileSync(path.join(tmp, "server.log"), "utf8").slice(-2000));
      process.exit(1);
    }
    browser = await chromium.launch({ executablePath: CHROME, headless: true });
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    await login(page, "admin", ADMIN_PW);

    // ---- 造数据：2 个产品 + 3 个主题（唯一名，避开内置种子主题） ----
    console.log("\n[A] 造数据（2 产品 + 3 主题）");
    const made = await page.evaluate(async () => {
      const post = async (url, body) => (await fetch(url, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
      const p1 = await post("/api/products", { name: "ZZ木梳", intro: "功效木梳", style: "" });
      const p2 = await post("/api/products", { name: "ZZ美食街", intro: "餐饮", style: "" });
      const prods = await (await fetch("/api/products")).json();
      const id1 = prods.find(p => p.name === "ZZ木梳").id;
      const id2 = prods.find(p => p.name === "ZZ美食街").id;
      await post("/api/topics", { name: "ZZ仅木梳", desc: "", product_id: id1 });
      await post("/api/topics", { name: "ZZ仅美食", desc: "", product_id: id2 });
      await post("/api/topics", { name: "ZZ两者通用", desc: "", product_id: 0 });
      return { id1, id2 };
    });
    check("建产品与主题成功", made.id1 > 0 && made.id2 > 0, JSON.stringify(made));

    // reload 让启动期的异步初始化全部收敛（否则并发的 loadProducts/loadTopics 会互相覆盖）
    await page.reload({ waitUntil: "load" });
    await page.waitForSelector("#run-form", { timeout: 15000 });
    await page.waitForFunction((id) => !!document.querySelector(`#product_id option[value="${id}"]`),
                               made.id1, { timeout: 10000 });
    await page.waitForTimeout(1200);   // 等启动时那次 loadTopics 跑完

    // ---- 选产品 A（木梳）→ 主题只应有 A 专属 + 通用 ----
    console.log("\n[B] 选「ZZ木梳」→ 主题列表随产品过滤（★核心）");
    await page.selectOption("#product_id", String(made.id1));
    await page.waitForTimeout(1500);
    const valB = await page.$eval("#product_id", el => el.value);
    let names = await page.$$eval("#topic-list .topic-cb", els => els.map(e => e.dataset.name));
    check(`B 阶段确实选中目标产品(id=${made.id1})`, String(valB) === String(made.id1), `实际 value=${valB}`);
    check("木梳下出现专属「ZZ仅木梳」", names.includes("ZZ仅木梳"), `共${names.length}个`);
    check("木梳下出现通用「ZZ两者通用」", names.includes("ZZ两者通用"));
    check("木梳下【不出现】美食专属「ZZ仅美食」 ★核心", !names.includes("ZZ仅美食"));

    // ---- 切到产品 B（美食街）→ 列表随切换而变 ----
    console.log("\n[C] 切到「ZZ美食街」→ 列表立即变化");
    await page.selectOption("#product_id", String(made.id2));
    await page.waitForTimeout(1500);
    names = await page.$$eval("#topic-list .topic-cb", els => els.map(e => e.dataset.name));
    check("美食街下出现专属「ZZ仅美食」", names.includes("ZZ仅美食"), `共${names.length}个`);
    check("美食街下出现通用「ZZ两者通用」", names.includes("ZZ两者通用"));
    check("美食街下【不出现】木梳专属「ZZ仅木梳」 ★核心", !names.includes("ZZ仅木梳"));
    const scope = await page.$eval("#topic-scope-hint", el => el.textContent || "");
    check("顶部范围提示显示当前产品名", scope.includes("ZZ美食街"), `"${scope}"`);

    // ---- 主题库管理页：归属徽章 ----
    console.log("\n[D] 主题库管理页显示归属徽章");
    await page.click("#topic-btn");
    await page.waitForSelector("#topic-list-mgr", { state: "visible", timeout: 8000 });
    await page.waitForFunction(() => document.querySelectorAll("#topic-list-mgr [id^=row-topic-]").length > 0,
                               { timeout: 8000 });
    const mgrText = await page.$eval("#topic-list-mgr", el => el.textContent || "");
    check("管理页显示归属徽章「ZZ木梳」", mgrText.includes("ZZ木梳"));
    check("管理页显示归属徽章「ZZ美食街」", mgrText.includes("ZZ美食街"));
    check("管理页显示「通用」徽章", mgrText.includes("通用"));
    const optText = await page.$eval("#topic_product", el => el.textContent || "");
    check("新增主题的归属下拉含「通用」与两个产品",
          optText.includes("通用") && optText.includes("ZZ木梳") && optText.includes("ZZ美食街"),
          optText.slice(0, 60));

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
