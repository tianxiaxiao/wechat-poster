/**
 * render_check_header_align.js — 表头「退出」按钮**右侧对齐**渲染断言
 * ============================================================================
 * 需求（2026-10-16）：标题栏加了「⚪ 体检」和「🕒 定时任务」徽章后，「退出」按钮被挤出
 * 右侧对齐列。根因：第一行是 flex-nowrap，且 <h1> 是 shrink-0 + whitespace-nowrap，
 * 徽章把 h1 撑宽后整行无法收缩/换行 → 向右溢出 → 退出被推到屏幕外。
 *
 * 本脚本在**窄视口（820px，平板/小窗）** + **最坏情况（两个徽章都显示、体检显示长报错文案）**
 * 下，用无头 Chrome 真实读 DOM 几何，断言：
 *   ① 整页没有横向溢出（documentElement.scrollWidth <= innerWidth）—— 退出没被推出去
 *   ② 「退出」在视口内（logout.right <= innerWidth）
 *   ③ 「退出」右对齐（logout.right ≈ header 右边缘，误差 ≤ 2px）
 *
 * 临时库 + 独立端口(8804)，不触发真实发布。
 * 运行：node tests/render_check_header_align.js      （SHOT=1 截图到 .tmp_layout/）
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT7 || "8804";
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const PY = "C:/Users/Administrator/.workbuddy/binaries/python/envs/poster/Scripts/python.exe";
const ADMIN_PW = "TestAdmin123";

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

// ⚠️ 会被序列化进浏览器执行 —— 只能用内部定义，不能引用外层 Node 变量
const READ_HEADER = () => {
  const header = document.querySelector("header");
  const logout = document.getElementById("logout-btn");
  const box = document.getElementById("hdr-badges");
  const track = document.getElementById("hdr-badges-track");
  const hr = header ? header.getBoundingClientRect() : null;
  const lr = logout ? logout.getBoundingClientRect() : null;
  return {
    headerRight: hr ? Math.round(hr.right) : null,
    headerLeft: hr ? Math.round(hr.left) : null,
    logoutRight: lr ? Math.round(lr.right) : null,
    logoutLeft: lr ? Math.round(lr.left) : null,
    logoutVisible: !!logout && logout.offsetParent !== null,
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    // 徽章轨道：是否单行（高度不暴涨 = 没换行）、是否触发左右滚动、是否真的溢出
    boxW: box ? Math.round(box.clientWidth) : null,
    trackScrollW: track ? Math.round(track.scrollWidth) : null,
    trackH: track ? Math.round(track.getBoundingClientRect().height) : null,
    trackScroll: track ? track.classList.contains("scroll") : null,
  };
};

// 把徽章逼到最坏情况：两个都显示 + 体检显示长报错文案
const FORCE_WORST = () => {
  const s = document.getElementById("sched-tag");
  if (s) s.classList.remove("hidden");
  const h = document.getElementById("health-badge");
  if (h) h.textContent = "🔴 模型或公众号异常";
  if (typeof layoutHdrBadges === "function") layoutHdrBadges();
};

// 制造"徽章内容明显超宽"：体检显示一段很长的文案，触发左右滚动（而不是换行）
const FORCE_OVERFLOW = () => {
  const s = document.getElementById("sched-tag");
  if (s) s.classList.remove("hidden");
  const h = document.getElementById("health-badge");
  if (h) h.textContent = "🔴 模型或公众号异常，且这段文案故意写得很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长";
  if (typeof layoutHdrBadges === "function") layoutHdrBadges();
};

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_ha_r_"));
  const db = path.join(tmp, "test.db");
  const env = { ...process.env, POSTER_DB: db, ADMIN_USERNAME: "admin", ADMIN_INIT_PASSWORD: ADMIN_PW,
                APP_SECRET_KEY: "test-secret-key-for-render-check", COOKIE_SECURE: "never",
                PYTHONIOENCODING: "utf-8", DEEPSEEK_API_KEY: "sk-FAKE-KEY-FOR-RENDER-CHECK" };
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
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const p = await ctx.newPage();
    p.on("dialog", async (d) => { await d.accept(); });
    const jsErrors = [];
    p.on("pageerror", (e) => jsErrors.push(String(e)));
    await login(p, "admin", ADMIN_PW);
    await p.waitForTimeout(800);

    console.log("\n[A] 默认态（宽屏 1440）");
    let h = await p.evaluate(READ_HEADER);
    check("★ 整页无横向溢出", h.scrollW <= h.innerW + 1, `scrollW=${h.scrollW} innerW=${h.innerW}`);
    check("★ 退出按钮可见", h.logoutVisible === true, String(h.logoutVisible));
    check("★ 退出在视口内", h.logoutRight <= h.innerW, `logoutRight=${h.logoutRight} innerW=${h.innerW}`);
    check("★ 退出右侧对齐（≈ header 右缘）", Math.abs(h.logoutRight - h.headerRight) <= 2,
          `logoutRight=${h.logoutRight} headerRight=${h.headerRight}`);
    check("★ 徽章轨道单行（没换行成两排）", h.trackH <= 44, `trackH=${h.trackH}`);

    console.log("\n[B] 最坏情况：两徽章都显示 + 体检长报错文案（宽屏 1440）");
    await p.evaluate(FORCE_WORST);
    await p.waitForTimeout(400);
    h = await p.evaluate(READ_HEADER);
    check("★ 整页无横向溢出", h.scrollW <= h.innerW + 1, `scrollW=${h.scrollW} innerW=${h.innerW}`);
    check("★ 退出在视口内", h.logoutRight <= h.innerW, `logoutRight=${h.logoutRight}`);
    check("★ 退出右侧对齐（≈ header 右缘）", Math.abs(h.logoutRight - h.headerRight) <= 2,
          `logoutRight=${h.logoutRight} headerRight=${h.headerRight}`);
    check("★ 徽章轨道单行（没换行成两排）", h.trackH <= 44, `trackH=${h.trackH}`);
    await shot(p, "ha_01_worst_wide.png");

    console.log("\n[C] 最坏情况 + 窄视口（820px，平板/小窗）—— 旧布局会溢出把退出推出去");
    await p.setViewportSize({ width: 820, height: 900 });
    await p.waitForTimeout(400);
    h = await p.evaluate(READ_HEADER);
    check("★ 整页无横向溢出（退出没被推出去）", h.scrollW <= h.innerW + 1, `scrollW=${h.scrollW} innerW=${h.innerW}`);
    check("★ 退出可见", h.logoutVisible === true, String(h.logoutVisible));
    check("★ 退出在视口内", h.logoutRight <= h.innerW, `logoutRight=${h.logoutRight} innerW=${h.innerW}`);
    check("★ 退出右侧对齐（≈ header 右缘）", Math.abs(h.logoutRight - h.headerRight) <= 2,
          `logoutRight=${h.logoutRight} headerRight=${h.headerRight}`);
    check("★ 徽章轨道单行（没换行成两排）", h.trackH <= 44, `trackH=${h.trackH}`);
    await shot(p, "ha_02_worst_narrow.png");

    console.log("\n[E] 徽章内容明显超宽 → 触发左右滚动（而不是换行）");
    await p.evaluate(FORCE_OVERFLOW);
    await p.waitForTimeout(400);
    h = await p.evaluate(READ_HEADER);
    check("★ 确实溢出了徽章区宽度", h.trackScrollW > h.boxW, `trackScrollW=${h.trackScrollW} boxW=${h.boxW}`);
    check("★ 溢出时加上了「左右滚动」class", h.trackScroll === true, String(h.trackScroll));
    check("★ 仍然单行（靠滚动而非换行解决，绝不两排）", h.trackH <= 44, `trackH=${h.trackH}`);
    check("★ 整页仍无横向溢出", h.scrollW <= h.innerW + 1, `scrollW=${h.scrollW} innerW=${h.innerW}`);
    check("★ 退出仍右对齐", Math.abs(h.logoutRight - h.headerRight) <= 2, `logoutRight=${h.logoutRight} headerRight=${h.headerRight}`);
    await shot(p, "ha_03_overflow_scroll.png");

    console.log("\n[D] 全程无 JS 报错");
    check("★ 无 pageerror", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));

    console.log("\n" + "=".repeat(58));
    console.log(`=== 表头退出对齐 渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
    if (fail) process.exitCode = 1;
  } catch (e) {
    console.error("运行异常：", e);
    fail++;
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close().catch(() => {});
    proc.kill("SIGKILL");
    try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (e) {}
  }
})();
