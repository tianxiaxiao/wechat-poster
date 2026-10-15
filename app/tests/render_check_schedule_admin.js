/**
 * render_check_schedule_admin.js — 「全局定时隐藏 + 管理员定时总览」渲染断言
 * ============================================================================
 * 为什么要这个脚本：接口返回 200 ≠ 页面真的隐藏了。本脚本用无头 Chrome 真实登录，
 * 读【计算样式】断言用户看得见的东西：
 *   · 单用户部署：老「全局定时」仍然可见（不能误伤单人部署）
 *   · 多人部署：老「全局定时」computed display === 'none'（真隐藏），替代提示出现
 *   · 管理员：看到「🗓 定时总览」按钮，点开后真的渲染出每个用户一行，并能代管保存
 *   · 编辑：老「全局定时」同样隐藏，且看不到管理员按钮与管理员引导语
 * 临时库 + 独立端口(8796)，定时全局关闭，不会触发真实发布。
 * 运行：node tests/render_check_schedule_admin.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT || "8796";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";

let pass = 0, fail = 0;
function check(name, cond, detail = "") {
  if (cond) { pass++; console.log("  ✅ " + name + (detail ? "   [" + detail + "]" : "")); }
  else { fail++; console.log("  ❌ " + name + (detail ? "   [" + detail + "]" : "")); }
}

const visible = (el) => getComputedStyle(el).display !== "none" && el.offsetParent !== null;

async function waitUp(timeoutMs = 60000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(BASE + "/health");
      if (r.ok) return true;
    } catch (e) { /* 还没起来 */ }
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

/** 在当前页里找「文本内容包含 text 的用户行」 */
async function findRow(scope, text) {
  const rows = await scope.$$('#sched-admin-list [data-uid]');
  for (const r of rows) {
    const t = await r.evaluate(e => e.textContent || "");
    if (t.includes(text)) return r;
  }
  return null;
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_render_"));
  const db = path.join(tmp, "test.db");
  const today = new Date().toISOString().slice(0, 10);

  // 建库并【关闭全局定时】——绝不能让测试实例真的去发文章
  execFileSync(PY, ["-c", [
    "import sqlite3, json, sys",
    "con = sqlite3.connect(sys.argv[1])",
    "con.execute('create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)')",
    "con.execute('insert or replace into settings (key, value) values (?, ?)',",
    "            ('daily_schedule', json.dumps({'enabled': False, 'time': '09:30', 'account_id': 0,",
    "                                           'product_id': 0, 'provider': 'deepseek', 'last_run_date': sys.argv[2]})))",
    "con.commit()"
  ].join("\n"), db, today], { stdio: "ignore" });

  const env = {
    ...process.env,
    POSTER_DB: db,
    ADMIN_USERNAME: "admin",
    ADMIN_INIT_PASSWORD: ADMIN_PW,
    APP_SECRET_KEY: "test-secret-key-for-render-check",
    COOKIE_SECURE: "never",
    PYTHONIOENCODING: "utf-8",
  };
  const log = fs.createWriteStream(path.join(tmp, "server.log"));
  const proc = spawn(PY, ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", PORT],
    { cwd: APP_DIR, env, stdio: ["ignore", "pipe", "pipe"] });
  proc.stdout.pipe(log); proc.stderr.pipe(log);

  let browser;
  try {
    if (!await waitUp()) {
      console.log("❌ 测试实例未能启动，日志尾部：");
      console.log(fs.readFileSync(path.join(tmp, "server.log"), "utf8").slice(-2500));
      process.exit(1);
    }

    browser = await chromium.launch({ executablePath: CHROME, headless: true });

    // ============ 阶段 A：单用户（仅 admin）→ 老入口必须还在 ============
    console.log("\n[A] 单用户部署：老「全局定时」应保持可见（不能误伤单人部署）");
    let ctx = await browser.newContext();
    let page = await ctx.newPage();
    await login(page, "admin", ADMIN_PW);
    await page.waitForSelector("#global-sched-row", { state: "attached", timeout: 15000 });
    await page.waitForTimeout(1500);            // 等 loadSchedule 拉完 /api/schedule
    let disp = await page.$eval("#global-sched-row", el => getComputedStyle(el).display);
    check("单用户：#global-sched-row 可见", disp !== "none", `display=${disp}`);

    // ============ 造出第二个用户（复用 admin 会话） ============
    const st = await page.evaluate(async () => {
      const r = await fetch("/api/users", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "edA", password: "EdA1234567", role: "editor" })
      });
      return r.status;
    });
    check("创建第二个用户 edA（进入多人部署）", st === 200, "HTTP " + st);

    // ============ 阶段 B：多人部署 + 管理员 ============
    console.log("\n[B] 多人部署（admin 视角）：老入口真隐藏 + 定时总览可代管");
    await page.reload({ waitUntil: "load" });
    await page.waitForSelector("#global-sched-row", { state: "attached", timeout: 15000 });
    await page.waitForTimeout(1800);
    const disp2 = await page.$eval("#global-sched-row", el => getComputedStyle(el).display);
    check("多人部署：#global-sched-row computed display:none（真隐藏）", disp2 === "none", `display=${disp2}`);
    check("多人部署：显示「定时按每人独立管理」提示",
      await page.$eval("#global-sched-hint", visible));
    check("admin 看到「🗓 定时总览」按钮",
      await page.$eval("#sched-admin-btn", visible));
    check("提示里带管理员「定时总览」引导",
      await page.$eval("#global-sched-hint-admin", el => getComputedStyle(el).display !== "none"));

    // 打开定时总览
    await page.click("#sched-admin-btn");
    await page.waitForSelector("#sched-admin-modal", { state: "visible", timeout: 10000 });
    await page.waitForFunction(
      () => document.querySelectorAll("#sched-admin-list [data-uid]").length >= 2, { timeout: 10000 });
    const nRows = await page.$$eval("#sched-admin-list [data-uid]", els => els.length);
    check("定时总览渲染出每个用户一行", nRows >= 2, `行数=${nRows}`);
    check("总览里能看到 edA", await page.$eval("#sched-admin-list", el => el.textContent.includes("edA")));

    // 代管：给 edA 开启定时并保存
    const edaRow = await findRow(page, "edA");
    check("定位到 edA 行", !!edaRow);
    if (edaRow) {
      await edaRow.$eval(".sa-enable", el => { if (!el.checked) el.click(); });
      await edaRow.$eval(".sa-time", el => { el.value = "09:15"; });
      await (await edaRow.$(".sa-save")).click();
      await page.waitForTimeout(1500);
      const msg = await page.$eval("#sched-admin-msg", el => (el.textContent || "").trim());
      check("代管保存后页面反馈成功", msg.includes("已保存"), `msg="${msg}"`);

      // 保存后面板自动刷新，回显应为「已开启 + 09:15」
      const edaRow2 = await findRow(page, "edA");
      if (edaRow2) {
        check("代管结果回显：edA 定时为开启",
          await edaRow2.$eval(".sa-enable", el => el.checked));
        check("代管结果回显：时间为 09:15",
          (await edaRow2.$eval(".sa-time", el => el.value)) === "09:15",
          await edaRow2.$eval(".sa-time", el => el.value));
      }
    }
    await page.click('#sched-admin-modal [data-close="sched-admin-modal"]');
    await page.waitForTimeout(300);

    // ============ 阶段 C：编辑视角 ============
    console.log("\n[C] 多人部署（编辑视角）：同样隐藏，且看不到管理员入口");
    const ctx2 = await browser.newContext();
    const page2 = await ctx2.newPage();
    await login(page2, "edA", "EdA1234567");
    await page2.waitForSelector("#global-sched-row", { state: "attached", timeout: 15000 });
    await page2.waitForTimeout(1800);
    const disp3 = await page2.$eval("#global-sched-row", el => getComputedStyle(el).display);
    check("编辑视角：#global-sched-row 也隐藏", disp3 === "none", `display=${disp3}`);
    check("编辑看不到「🗓 定时总览」按钮",
      await page2.$eval("#sched-admin-btn", el => getComputedStyle(el).display === "none" || el.offsetParent === null));
    check("编辑看不到管理员引导语",
      await page2.$eval("#global-sched-hint-admin", el => getComputedStyle(el).display === "none"));
    check("编辑仍能看到「定时按每人独立管理」提示",
      await page2.$eval("#global-sched-hint", visible));

    console.log("\n" + "=".repeat(56));
    console.log(`=== 渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
    process.exitCode = fail === 0 ? 0 : 1;
  } catch (e) {
    console.log("❌ 渲染断言执行异常：" + (e && e.stack || e));
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
    proc.kill();
  }
})();
