/**
 * render_check_key_policy.js — 多用户 Key 策略的**渲染断言**（读计算样式/DOM，不只看接口）
 * ============================================================================
 * 为什么：接口返回正确 ≠ 页面显示正确。本脚本用无头 Chrome 真实登录两种角色，验证：
 *   · 「我的设置」里 API Key 对**所有** provider 都可编辑（DeepSeek 只锁 base_url，不再锁 Key）
 *   · Key 归属状态提示真实显示（管理员=服务端 Key；普通用户缺 Key=红色警告）
 *   · 普通用户缺 Key 时保存：**面板不关闭** + 明确提示"发布会被拦"
 *   · 「测试连通」被权限拦下时显示 ⛔ 与原因（而不是假装网络失败）
 *   · 「用户管理」每行有「授权共享模型」勾选框；管理员行禁用；授权后徽章变为「共享 Key（已授权）」
 * 临时库 + 独立端口(8801)，不触发真实发布、不打真实大模型。
 * 运行：node tests/render_check_key_policy.js      （SHOT=1 时截图到项目根 .tmp_layout/）
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT4 || "8801";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";
const ED_PW = "Editor12345";

let pass = 0, fail = 0;
function check(name, cond, detail = "") {
  if (cond) { pass++; console.log("  ✅ " + name + (detail ? "   [" + detail + "]" : "")); }
  else { fail++; console.log("  ❌ " + name + (detail ? "   [" + detail + "]" : "")); }
}
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
const READ_HINT = () => {
  const el = document.getElementById("key-policy-hint");
  if (!el) return null;
  const cs = getComputedStyle(el);
  return { text: (el.textContent || "").trim(), status: el.dataset.status || "",
           color: cs.color, display: cs.display, h: el.getBoundingClientRect().height };
};
const READ_KEYFIELD = () => {
  const a = document.getElementById("api_key"), b = document.getElementById("base_url");
  return { apiReadOnly: a.readOnly, apiDisabled: a.disabled, apiType: a.type,
           baseReadOnly: b.readOnly, apiVal: a.value };
};

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_kp_r_"));
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
                PYTHONIOENCODING: "utf-8",
                DEEPSEEK_API_KEY: "sk-SERVER-DEFAULT-KEY",        // 服务端默认 Key（= 管理员的）
                OPENAI_COMPAT_API_KEY: "sk-SERVER-COMPAT-KEY",
                OPENAI_COMPAT_BASE_URL: "https://qwen.dadaobo.cn/v1" };
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

    // ===== 管理员会话 =====
    const ctxA = await browser.newContext();
    const adm = await ctxA.newPage();
    adm.on("dialog", async (d) => { await d.accept(); });
    await login(adm, "admin", ADMIN_PW);

    console.log("\n[A] 管理员：API Key 必须可编辑（本次核心）");
    await adm.click("#gear-btn");
    await adm.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await adm.waitForSelector("#key-policy-hint", { state: "attached", timeout: 8000 });
    // ⚠️ 必须等"确实拿到了状态"：元素没有 data-status 时 dataset.status 是 undefined，
//    写成 `!== ""` 恒为真 → 等于没等，会读到空提示（曾因此误报 3 条失败）
    await adm.waitForFunction(() => {
      const el = document.getElementById("key-policy-hint");
      return !!el && !!el.dataset.status;
    }, { timeout: 15000 });
    let hint = await adm.evaluate(READ_HINT);
    check("「我的设置」有 Key 归属状态提示", !!hint && hint.text.length > 0, hint ? hint.text.slice(0, 60) : "缺失");
    check("★ 管理员状态=shared（用服务端默认 Key）", hint.status === "shared", hint.status);
    check("管理员提示里说明自己是 Key 所有者", /管理员|服务端/.test(hint.text), hint.text.slice(0, 70));

    await adm.selectOption("#provider", "deepseek");
    await adm.waitForTimeout(300);
    let kf = await adm.evaluate(READ_KEYFIELD);
    check("DeepSeek：base_url 仍是只读（官方固定地址）", kf.baseReadOnly === true, `readOnly=${kf.baseReadOnly}`);
    check("★ DeepSeek：API Key **可编辑**（不再锁死）", kf.apiReadOnly === false && kf.apiDisabled === false,
          `readOnly=${kf.apiReadOnly} disabled=${kf.apiDisabled}`);
    check("API Key 是 password 类型（不明文展示）", kf.apiType === "password", kf.apiType);

    await adm.selectOption("#provider", "openai_compat");
    await adm.waitForTimeout(300);
    kf = await adm.evaluate(READ_KEYFIELD);
    check("切到 openai_compat：base_url 可编辑", kf.baseReadOnly === false, `readOnly=${kf.baseReadOnly}`);

    await adm.fill("#api_key", "sk-admin-typed-after-switch");
    await adm.waitForTimeout(200);
    hint = await adm.evaluate(READ_HINT);
    check("★ 未获授权的 provider 下切过来也能**填进** Key（曾经的只读锁残留已修）",
          (await adm.$eval("#api_key", el => el.value)) === "sk-admin-typed-after-switch", "已输入");
    check("改动后提示变为「未保存」状态（不再显示过期结论）", hint.status === "dirty", hint.status);
    await shot(adm, "kp_01_admin_settings.png", "#settings-modal .settings-panel");

    console.log("\n[B] 普通用户：没有自己的 Key → 明确红色警告 + 发布会被拦");
    await adm.click("#settings-close");
    const ctxB = await browser.newContext();
    const ed = await ctxB.newPage();
    ed.on("dialog", async (d) => { await d.accept(); });
    // 用管理员会话建一个编辑账号（注意：这里**不打开**用户管理弹层，
    // 否则遮罩会挡住后面 [C] 对 #users-btn 的点击）
    const created = await adm.evaluate(async () => {
      const r = await fetch("/api/users", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "edA", password: "Editor12345", name: "编辑A", role: "editor" }) });
      return (await r.json()).ok;
    });
    check("管理员建编辑账号 edA", created === true, String(created));
    await login(ed, "edA", ED_PW);

    await ed.click("#gear-btn");
    await ed.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await ed.waitForFunction(() => {
      const el = document.getElementById("key-policy-hint");
      return !!el && !!el.dataset.status;
    }, { timeout: 15000 });
    hint = await ed.evaluate(READ_HINT);
    check("★ 普通用户未填 Key → 状态=missing", hint.status === "missing", hint.status);
    const rgb = (s) => (s.match(/\d+/g) || []).map(Number);
    let c = rgb(hint.color);
    check("★ 警告文案是红色（读计算样式）", c[0] > c[1] && c[0] > c[2], `color=${hint.color}`);
    check("警告明确说「未设置你自己的 API Key」", hint.text.includes("未设置你自己的 API Key"), hint.text.slice(0, 70));
    check("警告里给出了解决办法（自己填 / 请管理员授权）",
          hint.text.includes("自己的") && hint.text.includes("管理员"), hint.text.slice(0, 90));
    kf = await ed.evaluate(READ_KEYFIELD);
    check("★ 普通用户也能在框里填自己的 Key", kf.apiReadOnly === false && kf.apiDisabled === false,
          `readOnly=${kf.apiReadOnly}`);
    await shot(ed, "kp_02_editor_missing.png", "#settings-modal .settings-panel");

    // 保存：应提示警告且**不关闭**面板
    await ed.click("#settings-save");
    await ed.waitForTimeout(1500);
    const stillOpen = await ed.$eval("#settings-modal", el => !el.classList.contains("hidden"));
    check("★ 缺 Key 保存后**面板不关闭**（方便当场补 Key）", stillOpen === true, `打开=${stillOpen}`);
    hint = await ed.evaluate(READ_HINT);
    check("保存后仍显示 missing 警告", hint.status === "missing", hint.status);

    // 测试连通：应被权限拦下，显示 ⛔
    await ed.click("#settings-test");
    await ed.waitForFunction(
      () => /⛔|❌|✅/.test(document.getElementById("llm-test-status").textContent || ""),
      { timeout: 30000 });
    const tst = await ed.$eval("#llm-test-status", el => (el.textContent || "").trim());
    check("★ 「测试连通」被权限拦下（⛔ 而非假装网络失败）", tst.startsWith("⛔"), tst.slice(0, 80));
    check("拦截信息里指向「授权」", tst.includes("授权"), tst.slice(0, 90));

    console.log("\n[C] 管理员的「授权共享模型」开关");
    await adm.click("#users-btn");
    await adm.waitForSelector("#users-list", { state: "visible", timeout: 8000 });
    await adm.waitForFunction(() => document.querySelectorAll("#users-list [data-uid]").length >= 2,
                              { timeout: 8000 });
    const rows = await adm.evaluate(() => [...document.querySelectorAll("#users-list [data-uid]")].map(r => ({
      uid: r.dataset.uid,
      uname: (r.querySelector(".text-xs.font-semibold") || {}).textContent || "",
      hasCb: !!r.querySelector('[data-f="llm_use_shared"]'),
      cbDisabled: !!(r.querySelector('[data-f="llm_use_shared"]') || {}).disabled,
      badge: (r.querySelector('span[title], span.rounded') || {}).textContent || "",
      badgeAll: (r.textContent || "").trim(),
    })));
    const rowA = rows.find(r => r.uname.trim() === "admin");
    const rowE = rows.find(r => r.uname.trim() === "edA");
    check("★ 每行都有「授权共享模型」勾选框", !!rowE && rowE.hasCb, JSON.stringify(rows.map(r => [r.uname, r.hasCb])));
    check("★ 管理员自己的勾选框是禁用的（本身就是 Key 所有者）", !!rowA && rowA.cbDisabled === true,
          `disabled=${rowA && rowA.cbDisabled}`);
    check("普通用户行显示「缺 Key」徽章", !!rowE && rowE.badgeAll.includes("缺 Key"), rowE && rowE.badgeAll.slice(0, 60));
    await shot(adm, "kp_03_users_grant.png", "#users-modal .settings-panel");

    await adm.evaluate((uid) => {
      const row = document.querySelector(`#users-list [data-uid="${uid}"]`);
      row.querySelector('[data-f="llm_use_shared"]').click();
    }, rowE.uid);
    await adm.evaluate((uid) => {
      const row = document.querySelector(`#users-list [data-uid="${uid}"]`);
      row.querySelector('[data-act="save"]').click();
    }, rowE.uid);
    await adm.waitForFunction(
      () => {
        const r = [...document.querySelectorAll("#users-list [data-uid]")]
          .find(x => x.querySelector('[data-f="llm_use_shared"]') && !x.querySelector('[data-f="llm_use_shared"]').disabled);
        return r && r.textContent.includes("共享 Key");
      }, { timeout: 10000 });
    const badgeAfter = await adm.evaluate((uid) => {
      const r = document.querySelector(`#users-list [data-uid="${uid}"]`);
      return (r.textContent || "").trim();
    }, rowE.uid);
    check("★ 勾选保存后徽章变为「共享 Key（已授权）」", badgeAfter.includes("共享 Key"), badgeAfter.slice(0, 80));

    console.log("\n[D] 授权后：普通用户界面同步变化");
    await ed.reload({ waitUntil: "load" });
    await ed.click("#gear-btn");
    await ed.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await ed.waitForFunction(() => {
      const el = document.getElementById("key-policy-hint");
      return !!el && !!el.dataset.status;
    }, { timeout: 15000 });
    hint = await ed.evaluate(READ_HINT);
    check("★ 授权后普通用户状态=shared", hint.status === "shared", hint.status);
    check("提示说明来自「管理员共享」", hint.text.includes("管理员共享"), hint.text.slice(0, 80));
    c = rgb(hint.color);
    check("shared 状态与「缺 Key」的红色明显不同（不再吓人）",
          hint.color !== "rgb(225, 29, 72)", `color=${hint.color}`);
    await shot(ed, "kp_04_editor_shared.png", "#settings-modal .settings-panel");

    await adm.evaluate((uid) => {
      const r = document.querySelector(`#users-list [data-uid="${uid}"]`);
      r.querySelector('[data-f="llm_use_shared"]').click();
      r.querySelector('[data-act="save"]').click();
    }, rowE.uid);
    await adm.waitForFunction(
      (uid) => {
        const r = document.querySelector(`#users-list [data-uid="${uid}"]`);
        return r && r.textContent.includes("缺 Key");
      }, rowE.uid, { timeout: 10000 });
    check("取消授权后徽章回到「缺 Key」", true, "已回退");

    console.log("\n" + "=".repeat(58));
    console.log(`=== Key 策略渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
  } catch (e) {
    console.log("❌ 断言脚本异常：" + (e && e.stack || e));
    fail++;
  } finally {
    if (browser) await browser.close().catch(() => {});
    try { proc.kill(); } catch (e) {}
  }
  process.exit(fail === 0 ? 0 : 1);
})();
