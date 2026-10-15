/**
 * render_check_llm_test.js — 「🔌 测试连通」按键 + 空状态提示不换行的渲染断言
 * ============================================================================
 * 为什么：接口返回正常 ≠ 页面显示正常。本脚本用无头 Chrome 真实登录后：
 *   · 断言「测试连通」按钮**真的在「取消」与「保存我的设置」之间**（按 DOM 顺序 + 屏幕坐标双重判定）
 *   · 断言状态文案真的跟着结果变色（读**计算样式**的 color，不是只看 class）
 *   · 用进程内的假 LLM 服务跑通「✅ 连通正常」，再用无人监听的端口跑通「❌ 失败」
 *   · 断言 `#user_acct_list` 的「暂无公众号…添加。」在**一行内**显示（不折行）
 * 临时库 + 独立端口(8799)，全局定时关闭，不触发真实发布。
 * 运行：node tests/render_check_llm_test.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const http = require("http");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT3 || "8799";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";

let pass = 0, fail = 0;
function check(name, cond, detail = "") {
  if (cond) { pass++; console.log("  ✅ " + name + (detail ? "   [" + detail + "]" : "")); }
  else { fail++; console.log("  ❌ " + name + (detail ? "   [" + detail + "]" : "")); }
}

// SHOT=1 时把界面截到项目根 .tmp_layout/（便于肉眼核对，默认关闭不影响断言）
const SHOT = process.env.SHOT === "1";
const OUT = path.resolve(APP_DIR, "..", ".tmp_layout");
async function shot(page, name, sel) {
  if (!SHOT) return;
  fs.mkdirSync(OUT, { recursive: true });
  await (sel ? page.locator(sel) : page).screenshot({ path: path.join(OUT, name) });
  console.log("     📸 .tmp_layout/" + name);
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

/** 读设置页按钮行几何 + 状态样式的通用取值器 */
const READ_ROW = () => {
  const g = (id) => {
    const e = document.getElementById(id);
    if (!e) return null;
    const r = e.getBoundingClientRect();
    const cs = getComputedStyle(e);
    return { x: r.x, y: r.y, right: r.right, w: r.width, h: r.height,
             text: (e.textContent || "").trim(), cls: e.className,
             color: cs.color, display: cs.display, disabled: !!e.disabled };
  };
  const cancelEl = document.getElementById("settings-cancel");
  const btnRow = cancelEl ? cancelEl.parentElement : null;         // 只含三个按钮
  const wrap = btnRow ? btnRow.parentElement : null;               // 状态 + 按钮行
  return {
    order: btnRow ? [...btnRow.children].map(c => c.id || c.tagName) : [],
    wrapOrder: wrap ? [...wrap.children].map(c => c.id || c.tagName) : [],
    rowOverflow: btnRow ? btnRow.scrollWidth - btnRow.clientWidth : -1,
    cancel: g("settings-cancel"), test: g("settings-test"),
    save: g("settings-save"), status: g("llm-test-status"),
  };
};

/** 读 `#user_acct_list` 空状态提示是否单行 */
const READ_EMPTY_HINT = () => {
  const box = document.getElementById("user_acct_list");
  const el = box ? box.firstElementChild : null;
  if (!el) return null;
  const cs = getComputedStyle(el);
  const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.5;
  return { text: (el.textContent || "").trim(), h: el.getBoundingClientRect().height,
           lineHeight: lh, fontSize: parseFloat(cs.fontSize), whiteSpace: cs.whiteSpace,
           overflowX: el.scrollWidth - el.clientWidth, boxOverflowX: box.scrollWidth - box.clientWidth,
           gridColumn: cs.gridColumnStart + " / " + cs.gridColumnEnd };
};

(async () => {
  // ---- 进程内假 LLM（OpenAI 兼容），供「✅ 连通正常」用 ----
  const fake = http.createServer((req, res) => {
    req.on("data", () => {});
    req.on("end", () => {
      const payload = JSON.stringify({
        id: "chatcmpl-fake", object: "chat.completion", created: 0, model: "fake-model",
        choices: [{ index: 0, message: { role: "assistant", content: "pong" }, finish_reason: "stop" }],
      });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(payload);
    });
  });
  await new Promise(r => fake.listen(0, "127.0.0.1", r));
  const fakeUrl = `http://127.0.0.1:${fake.address().port}/v1`;
  // 无人监听的端口（先占后放）→ 用于「❌ 连接失败」
  const probe = http.createServer();
  await new Promise(r => probe.listen(0, "127.0.0.1", r));
  const deadUrl = `http://127.0.0.1:${probe.address().port}/v1`;
  await new Promise(r => probe.close(r));

  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_llmt_r_"));
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
    page.on("dialog", async (d) => { await d.accept(); });
    await login(page, "admin", ADMIN_PW);

    // ---------- [A] 按钮位置：必须在「取消」与「保存我的设置」之间 ----------
    console.log("\n[A] 「测试连通」按钮的位置（★核心需求）");
    await page.click("#gear-btn");
    await page.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await page.waitForSelector("#settings-test", { state: "attached", timeout: 8000 });
    let row = await page.evaluate(READ_ROW);
    check("按钮行同时存在「取消 / 测试连通 / 保存我的设置」",
          !!row.cancel && !!row.test && !!row.save,
          row.order.join(" → "));
    check("DOM 顺序为 取消→测试连通→保存", JSON.stringify(row.order) ===
          JSON.stringify(["settings-cancel", "settings-test", "settings-save"]),
          row.order.join(" → "));
    check("状态显示区排在按钮行之前（独立一行）", row.wrapOrder[0] === "llm-test-status",
          row.wrapOrder.join(" → "));
    check("★ 屏幕坐标上测试键位于「取消」右侧", row.cancel && row.test && row.cancel.right <= row.test.x + 1,
          `取消右边缘=${row.cancel && Math.round(row.cancel.right)} / 测试键左边缘=${row.test && Math.round(row.test.x)}`);
    check("★ 屏幕坐标上测试键位于「保存」左侧", row.test && row.save && row.test.right <= row.save.x + 1,
          `测试键右边缘=${row.test && Math.round(row.test.right)} / 保存左边缘=${row.save && Math.round(row.save.x)}`);
    check("★ 状态区在按钮行的**上方**（不抢宽度）", row.status && row.cancel && row.status.y < row.cancel.y,
          `状态y=${row.status && Math.round(row.status.y)} / 按钮y=${row.cancel && Math.round(row.cancel.y)}`);
    check("★ 三个按钮同处一行（状态文案再多也不换行）",
          row.cancel && row.test && row.save &&
          Math.abs(row.cancel.y - row.test.y) < 2 && Math.abs(row.test.y - row.save.y) < 2,
          `y: 取消=${row.cancel && Math.round(row.cancel.y)} 测试=${row.test && Math.round(row.test.y)} 保存=${row.save && Math.round(row.save.y)}`);
    check("按钮文字正确（取消 / 测试连通 / 保存我的设置）",
          row.cancel.text === "取消" && /测试连通/.test(row.test.text) && row.save.text === "保存我的设置",
          `${row.cancel.text} | ${row.test.text} | ${row.save.text}`);
    check("按钮行无横向溢出（加了测试键也不挤）", row.rowOverflow <= 0, `溢出 ${row.rowOverflow}px`);
    check("初始状态文案为空（不显示陈旧的测试结果）", row.status && row.status.text === "",
          JSON.stringify(row.status && row.status.text));
    check("空状态也占位（status 有高度，出现文案时不抖动）",
          row.status && row.status.h >= 14, `h=${row.status && row.status.h}px`);

    // ---------- [B] 点测试 → 成功（假 LLM 在线） ----------
    console.log("\n[B] 点击「测试连通」→ ✅ 连通正常");
    await page.selectOption("#provider", "openai_compat");
    await page.fill("#base_url", fakeUrl);
    await page.fill("#model", "fake-model");
    await page.fill("#api_key", "sk-render-canary");
    await page.click("#settings-test");
    await page.waitForFunction(
      () => /连通|❌/.test(document.getElementById("llm-test-status").textContent || ""),
      { timeout: 30000 });
    row = await page.evaluate(READ_ROW);
    check("★ 状态显示「连通正常」", /连通正常/.test(row.status.text), row.status.text);
    check("状态里带上了实际模型名", /fake-model/.test(row.status.text), row.status.text);
    check("状态里带上了耗时(ms)", /\d+\s*ms/.test(row.status.text), row.status.text);
    // 读计算样式的颜色判断"真的变绿了"，而不是只改了 class
    const rgb = (s) => (s.match(/\d+/g) || []).map(Number);
    const cOk = rgb(row.status.color);
    check("★ 成功态文字颜色为绿色系（读计算样式）", cOk[1] > cOk[0] && cOk[1] > cOk[2],
          `color=${row.status.color}`);
    check("测试完成后按钮恢复可点", row.test.disabled === false, `disabled=${row.test.disabled}`);
    check("状态区可见（非隐藏）", row.status.display !== "none", row.status.display);
    await shot(page, "llmtest_01_ok.png", "#settings-modal .settings-panel");

    // ---------- [C] 点测试 → 失败（端口无人监听） ----------
    console.log("\n[C] 指向无人监听的端口 → ❌ 失败提示");
    await page.fill("#base_url", deadUrl);
    await page.click("#settings-test");
    await page.waitForFunction(
      () => { const t = document.getElementById("llm-test-status").textContent || ""; return /❌/.test(t); },
      { timeout: 40000 });
    row = await page.evaluate(READ_ROW);
    check("★ 失败时状态以 ❌ 开头并给出人话提示", /❌/.test(row.status.text) && row.status.text.length >= 6,
          row.status.text);
    const cErr = rgb(row.status.color);
    check("★ 失败态文字颜色为红色系（读计算样式）", cErr[0] > cErr[1] && cErr[0] > cErr[2],
          `color=${row.status.color}`);
    check("失败后按钮也恢复可点（可反复重试）", row.test.disabled === false, `disabled=${row.test.disabled}`);
    await shot(page, "llmtest_02_fail.png", "#settings-modal .settings-panel");

    // ---------- [D] 切换 provider 会清空旧结果 ----------
    console.log("\n[D] 换 Provider 后清空上次结果（避免误读）");
    await page.selectOption("#provider", "deepseek");
    await page.waitForTimeout(400);
    row = await page.evaluate(READ_ROW);
    check("切换 provider 后状态被清空", row.status.text === "", JSON.stringify(row.status.text));

    // ---------- [E] 「暂无公众号…添加。」保持一行 ----------
    console.log("\n[E] 空状态提示不换行（★核心需求）");
    // 真实走一遍「一个公众号都没有」的场景：删光已播种的号 → 重新拉取并渲染
    const removed = await page.evaluate(async () => {
      const list = await (await fetch("/api/accounts")).json();
      for (const a of (list || [])) await fetch("/api/accounts/" + a.id, { method: "DELETE" });
      await loadAccounts();                 // 重新拉真实列表
      renderUserAcctCheckboxes([]);         // 用真实渲染函数渲染空列表
      return (list || []).map(a => a.name);
    });
    check("已清空公众号，进入空状态场景", await page.evaluate(
      () => document.querySelectorAll("#user_acct_list .user-acct-cb").length === 0),
      "删除=" + JSON.stringify(removed));
    let hint = await page.evaluate(READ_EMPTY_HINT);
    check("空状态提示存在且文案含「添加」", !!hint && hint.text.includes("添加"), hint ? hint.text : "未找到");
    check("★ 提示高度 = 单行（未折行）", hint && hint.h <= hint.lineHeight + 3,
          hint ? `h=${hint.h.toFixed(1)}px / lineHeight=${hint.lineHeight}px` : "");
    check("★ 提示横向无溢出/无滚动条", hint && hint.overflowX <= 0 && hint.boxOverflowX <= 0,
          hint ? `自身溢出=${hint.overflowX} 容器溢出=${hint.boxOverflowX}` : "");
    check("提示使用 whitespace:nowrap 锁定单行", hint && hint.whiteSpace === "nowrap",
          hint ? hint.whiteSpace : "");
    check("提示跨满两列栅格（col-span-2 生效）", hint && /span 2|1 \/ -1/.test(hint.gridColumn),
          hint ? hint.gridColumn : "");
    const fullText = await page.evaluate(
      () => [...document.querySelectorAll("#user_acct_list div")].map(d => (d.textContent || "").trim()).join("|"));
    check("提示文案完整未被截断", fullText.includes("添加。"), fullText);
    // 与「标签行」对照：标签行也必须是单行（否则说明整块宽度仍不够）
    const labelOne = await page.evaluate(() => {
      const els = [...document.querySelectorAll("#settings-modal .text-xs, #settings-modal .hint")];
      const el = els.find(e => (e.textContent || "").includes("勾选要定时发布的公众号"));
      if (!el) return null;
      const cs = getComputedStyle(el);
      const lh = parseFloat(cs.lineHeight) || parseFloat(cs.fontSize) * 1.5;
      return { h: el.getBoundingClientRect().height, lineHeight: lh };
    });
    check("「勾选要定时发布的公众号…」标签也是单行", labelOne && labelOne.h <= labelOne.lineHeight + 3,
          labelOne ? `h=${labelOne.h.toFixed(1)}px / lineHeight=${labelOne.lineHeight}px` : "未找到");
    await shot(page, "llmtest_03_empty_hint.png", "#settings-modal .settings-panel");

    // ---------- [F] 重开弹层仍会清空 ----------
    console.log("\n[F] 关闭再打开设置 → 状态清空");
    await page.click("#settings-close");
    await page.waitForTimeout(300);
    await page.click("#gear-btn");
    await page.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await page.waitForTimeout(500);
    row = await page.evaluate(READ_ROW);
    check("重开设置后状态区为空", row.status.text === "", JSON.stringify(row.status.text));

    console.log("\n" + "=".repeat(56));
    console.log(`=== 渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
  } catch (e) {
    console.log("❌ 断言脚本异常：" + (e && e.stack || e));
    fail++;
  } finally {
    if (browser) await browser.close().catch(() => {});
    try { proc.kill(); } catch (e) {}
    try { fake.close(); } catch (e) {}
  }
  process.exit(fail === 0 ? 0 : 1);
})();
