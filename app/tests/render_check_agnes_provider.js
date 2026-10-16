/**
 * render_check_agnes_provider.js — Agnes AI 作为可选云端模型出现在下拉中的渲染断言
 * ============================================================================
 * 为什么：接口能返回 agnes（后端已单测）≠ 页面下拉真的有、切过去 base_url 真的被锁定。
 *   本脚本用无头 Chrome 真实登录后：
 *   · 断言「模型来源」下拉真的出现 agnes 选项（读 <option> 值，不是只看 JS 变量）
 *   · 断言选中 agnes 后 #base_url 被预填为官方地址且 readOnly=true（仿 DeepSeek 锁定逻辑）
 *   · 断言 provider 标签显示「🤖 Agnes AI」
 *   · 断言「快捷预设」里能一键填入 agnes-3.0-flash
 * 临时库 + 独立端口(8797)，全局定时关闭，不触发真实发布。
 * 运行：node tests/render_check_agnes_provider.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT_AGNES || "8797";
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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_agnes_r_"));
  const db = path.join(tmp, "test.db");
  const today = new Date().toISOString().slice(0, 10);
  const { execFileSync } = require("child_process");
  execFileSync(PY, ["-c", [
    "import sqlite3, json, sys",
    "con = sqlite3.connect(sys.argv[1])",
    "con.execute('create table if not exists settings (key varchar(64) primary key, value text, updated_at datetime)')",
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
    page.on("dialog", async (d) => { await d.accept(); });
    await login(page, "admin", ADMIN_PW);

    console.log("\n[A] 模型来源下拉出现 Agnes 选项");
    await page.click("#gear-btn");
    // 等设置页所有异步加载完成（user/settings + accounts + products），避免随后覆盖我们对 agnes 的选择。
    // 这是测试时序问题，真实用户打开齿轮后等加载完再选不受影响。
    await page.waitForTimeout(3500);
    await page.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await page.waitForSelector("#provider", { state: "attached", timeout: 8000 });
    const opts = await page.evaluate(() =>
      [...document.getElementById("provider").options].map(o => o.value));
    check("下拉含 agnes 选项", opts.includes("agnes"), opts.join(","));
    check("下拉仍含原有 deepseek / ollama / openai_compat",
          ["deepseek", "ollama", "openai_compat"].every(o => opts.includes(o)), opts.join(","));

    console.log("\n[B] 选中 agnes → base_url 预填且锁定只读");
    await page.selectOption("#provider", "agnes");
    await page.waitForTimeout(400); // 等 JS 应用锁定逻辑
    const bu = await page.evaluate(() => {
      const e = document.getElementById("base_url");
      return { v: e.value, ro: e.readOnly, ph: e.placeholder };
    });
    check("base_url 预填为 Agnes 官方地址", bu.v === "https://api.agnes-ai.cn/v1", bu.v);
    check("base_url 锁定只读（仿 DeepSeek）", bu.ro === true, "readOnly=" + bu.ro);
    const model = await page.evaluate(() => document.getElementById("model").value);
    check("model 预填为 agnes-3.0-flash", model === "agnes-3.0-flash", model);

    console.log("\n[C] provider 标签显示 Agnes 文案");
    const tag = await page.evaluate(() => {
      const e = document.getElementById("provider-tag");
      return e ? { text: e.textContent.trim(), cls: e.className } : null;
    });
    check("标签文案含 Agnes", tag && /Agnes/.test(tag.text), tag && tag.text);
    check("标签使用 badge-provider-agnes 配色类", tag && tag.cls.includes("badge-provider-agnes"), tag && tag.cls);

    console.log("\n[D] 快捷预设可一键填入 Agnes");
    const hasAgnesPreset = await page.evaluate(() => {
      const sel = document.getElementById("preset");
      if (!sel) return null;
      return [...sel.options].some(o => /Agnes/i.test(o.textContent));
    });
    check("预设下拉含 Agnes 预设项", hasAgnesPreset === true, "result=" + hasAgnesPreset);

  } catch (e) {
    fail++;
    console.log("  ❌ 脚本异常：" + (e && e.stack || e));
  } finally {
    if (browser) await browser.close();
    try { proc.kill("SIGKILL"); } catch (e) {}
    console.log(`\n==== Agnes provider 渲染断言：${pass} 通过 / ${fail} 失败 ====`);
    process.exit(fail ? 1 : 0);
  }
})();
