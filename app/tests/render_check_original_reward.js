/**
 * render_check_original_reward.js — 「声明原创 / 开启赞赏」开关的渲染断言
 * ============================================================================
 * 为什么：原创/赞赏是微信官方 API 做不到、必须走 playwright 编辑器 UI 的能力。
 *   开关加了 ≠ 用户看得见 ≠ 没被 CSS 撑坏。本项目踩过「.llm-fields 把 checkbox 撑满宽、
 *   把按钮挤出视野」，所以这里必须**读计算样式 + 量几何**，而不是只看元素存在。
 * 无头 Chrome 真实登录 admin 后断言：
 *   [A] 两个开关都真实存在且用户看得见（display/visibility/尺寸/文案）
 *   [B] 几何正确：宽度≈14px(w-3.5)，没被 .llm-fields 的 width:100% 撑满（历史踩坑）
 *   [C] 首次进入默认都勾选（服务端 original/reward 默认 true）
 *   [D] 关闭后经 saveStored 落 localStorage → 刷新回显关闭；再开回也生效
 *   [E] 「🔒 原创/赞赏只在实际走 UI 流程时生效」的提示文案在页面可见
 * 临时库 + 独立端口(8797)，不触发真实发布、不建真实任务。
 * 运行：node tests/render_check_original_reward.js
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT_ORIG || "8797";
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
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_orig_r_"));
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
    await page.waitForTimeout(1200);

    console.log("\n[A] 两个开关真实存在且用户看得见（读计算样式）");
    const geo = await page.evaluate(() => {
      const ids = ["declare_original", "open_reward"];
      return ids.map((id) => {
        const el = document.getElementById(id);
        if (!el) return { id, missing: true };
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const label = el.closest("label");
        const lr = label ? label.getBoundingClientRect() : null;
        return {
          id,
          display: cs.display,
          visibility: cs.visibility,
          opacity: parseFloat(cs.opacity || "1"),
          w: Math.round(r.width), h: Math.round(r.height),
          labelText: label ? (label.textContent || "").replace(/\s+/g, " ").trim() : "",
          labelW: lr ? Math.round(lr.width) : 0,
          labelVisible: lr ? (lr.width > 0 && lr.height > 0) : false,
          // 历史踩坑：.llm-fields 的 input{width:100%} 会把 checkbox 撑满整行
          insideLlmFields: !!el.closest(".llm-fields"),
        };
      });
    });
    for (const g of geo) {
      check(`① #${g.id} 元素存在`, !g.missing);
      if (g.missing) continue;
      check(`① #${g.id} 未被隐藏（display≠none/visibility≠hidden/opacity>0）`,
        g.display !== "none" && g.visibility !== "hidden" && g.opacity > 0.01,
        `display=${g.display} vis=${g.visibility} opacity=${g.opacity}`);
      check(`① #${g.id} 有实际尺寸（用户看得见）`, g.w > 0 && g.h > 0, `${g.w}x${g.h}`);
      check(`① #${g.id} 所属 label 可见且文案非空`, g.labelVisible && g.labelText.length > 0,
        `"${g.labelText}"`);
    }

    console.log("\n[B] 几何正确：没被 .llm-fields 撑满宽（w-3.5≈14px）");
    for (const g of geo) {
      if (g.missing) continue;
      // .llm-fields 的 input{width:100%} 会让 checkbox 变成整行宽度（几百px）
      check(`② #${g.id} 不在 .llm-fields 内（避免被撑满宽）`, g.insideLlmFields === false);
      check(`② #${g.id} 宽度≈14px（未被撑满）`, g.w >= 10 && g.w <= 24, `w=${g.w}px`);
    }

    console.log("\n[C] 首次进入：两个开关默认都勾选");
    const def = await page.evaluate(() => ({
      orig: document.getElementById("declare_original").checked,
      reward: document.getElementById("open_reward").checked,
    }));
    check("③ 原创默认勾选", def.orig === true, "checked=" + def.orig);
    check("③ 赞赏默认勾选", def.reward === true, "checked=" + def.reward);

    console.log("\n[D] 保存→刷新回显（真实 saveStored/localStorage 往返，不建任务）");
    // 用真实的 saveStored 写入（与提交时同路径），再刷新验证 initOrigPref 的回显
    await page.evaluate(() => window.saveStored({ declare_original: false, open_reward: false }));
    await page.reload({ waitUntil: "load" });
    await page.waitForSelector("#declare_original", { timeout: 15000 });
    await page.waitForTimeout(800);
    const afterOff = await page.evaluate(() => ({
      orig: document.getElementById("declare_original").checked,
      reward: document.getElementById("open_reward").checked,
    }));
    check("④ 关闭后刷新 → 两个开关都回显为未勾选",
      afterOff.orig === false && afterOff.reward === false,
      `orig=${afterOff.orig} reward=${afterOff.reward}`);

    // 再开回来，确认可恢复
    await page.evaluate(() => window.saveStored({ declare_original: true, open_reward: true }));
    await page.reload({ waitUntil: "load" });
    await page.waitForSelector("#declare_original", { timeout: 15000 });
    await page.waitForTimeout(800);
    const afterOn = await page.evaluate(() => ({
      orig: document.getElementById("declare_original").checked,
      reward: document.getElementById("open_reward").checked,
    }));
    check("⑤ 再开回并刷新 → 两个开关都回显为勾选",
      afterOn.orig === true && afterOn.reward === true,
      `orig=${afterOn.orig} reward=${afterOn.reward}`);

    // 只关原创、保留赞赏：验证两个开关互相独立（不是同一个控件/同一个存储位）
    await page.evaluate(() => window.saveStored({ declare_original: false, open_reward: true }));
    await page.reload({ waitUntil: "load" });
    await page.waitForSelector("#declare_original", { timeout: 15000 });
    await page.waitForTimeout(800);
    const mixed = await page.evaluate(() => ({
      orig: document.getElementById("declare_original").checked,
      reward: document.getElementById("open_reward").checked,
    }));
    check("⑥ 只关原创 → 原创关、赞赏仍开（两开关互相独立）",
      mixed.orig === false && mixed.reward === true,
      `orig=${mixed.orig} reward=${mixed.reward}`);

    console.log("\n[E] 提示文案可见：原创/赞赏仅在走 UI 流程时生效");
    // ⚠️ 该提示在「公众号」弹窗内，默认 hidden；且 .settings-modal 多为 position:fixed，
    //    offsetParent 恒为 null → 不能拿它判可见性，必须用 getBoundingClientRect 量尺寸。
    await page.click("#acct-btn");
    await page.waitForSelector("#acct-modal:not(.hidden)", { timeout: 8000 });
    await page.waitForTimeout(700);
    const hint = await page.evaluate(() => {
      const nodes = Array.from(document.querySelectorAll(".hint"));
      const hit = nodes.find(n => /原创/.test(n.textContent) && /赞赏/.test(n.textContent));
      if (!hit) return { found: false };
      const r = hit.getBoundingClientRect();
      const cs = getComputedStyle(hit);
      return { found: true, w: Math.round(r.width), h: Math.round(r.height),
               display: cs.display, vis: cs.visibility,
               text: (hit.textContent || "").replace(/\s+/g, " ").trim().slice(0, 60) };
    });
    check("⑦ 提示存在", hint.found === true);
    check("⑦ 打开公众号弹窗后提示真实可见（量几何，非 offsetParent）",
      !!hint.found && hint.w > 0 && hint.h > 0 && hint.display !== "none" && hint.vis !== "hidden",
      hint.found ? `${hint.w}x${hint.h} display=${hint.display} "${hint.text}…"` : "");

  } catch (e) {
    fail++;
    console.log("  ❌ 脚本异常：" + (e && e.stack || e));
  } finally {
    if (browser) await browser.close();
    try { proc.kill("SIGKILL"); } catch (e) {}
    console.log(`\n==== 原创/赞赏开关渲染断言：${pass} 通过 / ${fail} 失败 ====`);
    process.exit(fail ? 1 : 0);
  }
})();
