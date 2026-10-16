/**
 * render_check_quality_refine.js — 「质量精修」开关的渲染断言
 * ============================================================================
 * 为什么：后端单测已验证 refine_article + _gen_with_dedup 接入；但「用户能在设置页
 *   看到开关、勾选后真的落库、并能再勾回来」属于肉眼看不见的接线，必须断言。
 * 无头 Chrome 真实登录 admin 后：
 *   · 断言「AI 模型设置」弹窗里真的有 #quality_refine 开关
 *   · 断言默认勾选（服务端 quality_refine 默认 true）
 *   · 取消勾选 → 保存 → 读 /api/user/settings 确认 quality_refine===false（API 持久化）
 *   · 重新打开设置 → 复选框反映已关闭（UI 回显）
 *   · 再勾上 → 保存 → 确认 quality_refine===true（可开回来）
 * 临时库 + 独立端口(8796)，不触发真实发布。
 * 运行：node tests/render_check_quality_refine.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT_QREFINE || "8796";
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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_qrefine_r_"));
  const db = path.join(tmp, "test.db");
  const today = new Date().toISOString().slice(0, 10);
  const { execFileSync } = require("child_process");
  execFileSync(PY, ["-c", [
    "import sqlite3, sys",
    "con = sqlite3.connect(sys.argv[1])",
    "con.execute('create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)')",
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
    page.on("dialog", async (d) => { await d.accept(); });
    await login(page, "admin", ADMIN_PW);

    console.log("\n[A] 设置弹窗含「质量精修」开关且默认勾选");
    await page.click("#gear-btn");
    await page.waitForTimeout(3500); // 等 user/settings + accounts + products 异步加载完成
    await page.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await page.waitForSelector("#quality_refine", { timeout: 8000 });
    const has = await page.$("#quality_refine");
    check("① 质量精修开关存在于设置弹窗", !!has);
    const defChecked = await page.evaluate(() => document.getElementById("quality_refine").checked);
    check("② 默认勾选（服务端 quality_refine 默认 true）", defChecked === true, "checked=" + defChecked);

    console.log("\n[B] 取消勾选 → 保存 → 确认 API 持久化");
    await page.uncheck("#quality_refine");
    await page.click("#settings-save");
    await page.waitForTimeout(1800); // 等 POST /api/user/settings 完成
    const apiOff = await page.evaluate(async () => {
      const r = await fetch("/api/user/settings"); const d = await r.json();
      return d.quality_refine;
    });
    check("③ 保存后 /api/user/settings 返回 quality_refine=false", apiOff === false, "value=" + apiOff);

    console.log("\n[C] 重新打开设置 → 复选框反映已关闭");
    // 保存成功路径会自动 closeSettings()（index.html:1468），故无需点 ×。
    // 直接再次点齿轮：gear-btn 处理器会重新跑 loadUserSettings，从 API 回显开关状态。
    await page.click("#gear-btn");
    await page.waitForTimeout(3000);
    await page.waitForSelector("#quality_refine", { timeout: 8000 });
    const reChecked = await page.evaluate(() => document.getElementById("quality_refine").checked);
    check("④ 重新打开设置 → 开关反映已关闭", reChecked === false, "checked=" + reChecked);

    console.log("\n[D] 再勾上 → 保存 → 确认可开回来");
    await page.check("#quality_refine");
    await page.click("#settings-save");
    await page.waitForTimeout(1800);
    const apiOn = await page.evaluate(async () => {
      const r = await fetch("/api/user/settings"); const d = await r.json();
      return d.quality_refine;
    });
    check("⑤ 重新勾选保存 → quality_refine=true", apiOn === true, "value=" + apiOn);

  } catch (e) {
    fail++;
    console.log("  ❌ 脚本异常：" + (e && e.stack || e));
  } finally {
    if (browser) await browser.close();
    try { proc.kill("SIGKILL"); } catch (e) {}
    console.log(`\n==== 质量精修开关渲染断言：${pass} 通过 / ${fail} 失败 ====`);
    process.exit(fail ? 1 : 0);
  }
})();
