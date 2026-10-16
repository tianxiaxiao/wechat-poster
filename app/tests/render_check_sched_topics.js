/**
 * render_check_sched_topics.js — 「定时任务主题」的**渲染断言**
 * ============================================================================
 * 需求（2026-10-16）：
 *   ① 标题栏体检徽章后面显示「🕒 定时任务」（没设定时 → 不显示）
 *   ② 主题区「共 N 个主题 × 1 篇 … ☑全选 ☐全不选」这一排**前移**，后面加「🕒 定时任务」按键
 *   ③ 点它显示哪些主题被勾选，可取消/选择；勾选即加入定时任务，取消即从定时任务移除
 *
 * 为什么必须写渲染断言：接口 200 不代表页面对。本项目已经踩过三次「代码对、页面错」
 * （二维码写 localhost、hidden 被 CSS 压制、按钮被挤出视野）。本脚本用无头 Chrome 真实登录，
 * 逐条读 **DOM + 计算样式 + 几何**，并且**回查接口**确认勾选真的落库（不是只改了页面）。
 *
 * 临时库 + 独立端口(8803)，不触发真实发布。
 * 运行：node tests/render_check_sched_topics.js      （SHOT=1 时截图到项目根 .tmp_layout/）
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT6 || "8803";
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

// 读某个元素：可见性 / 几何 / 是否被挤出所在容器
const READ_EL = (sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const cs = getComputedStyle(el);
  return {
    visible: el.offsetParent !== null,
    left: Math.round(r.left), right: Math.round(r.right),
    top: Math.round(r.top), bottom: Math.round(r.bottom),
    w: Math.round(r.width), h: Math.round(r.height),
    text: (el.textContent || "").trim(), cls: el.className,
    title: (el.getAttribute("title") || "").trim(),
    cursor: cs.cursor, display: cs.display,
  };
};

// 主题区工具条：是否排在主题列表【之前】、四个控件的顺序与是否溢出
const READ_TOOLBAR = () => {
  const tb = document.getElementById("topic-toolbar");
  const list = document.getElementById("topic-list");
  const hint = document.getElementById("topic-sel-hint");
  const all = document.getElementById("topic-sel-all-btn");
  const none = document.getElementById("topic-sel-none-btn");
  const sched = document.getElementById("topic-sched-btn");
  const g = (el) => el ? el.getBoundingClientRect() : null;
  const tbR = g(tb), liR = g(list);
  const inFields = !!(tb && tb.closest(".llm-fields"));
  const btns = [all, none, sched].map(b => b ? {
    id: b.id, text: (b.textContent || "").trim(),
    left: Math.round(b.getBoundingClientRect().left),
    right: Math.round(b.getBoundingClientRect().right),
    visible: b.offsetParent !== null,
  } : null);
  return {
    exists: !!tb, inLlmFields: inFields,
    tbTop: tbR ? Math.round(tbR.top) : null,
    tbBottom: tbR ? Math.round(tbR.bottom) : null,
    listTop: liR ? Math.round(liR.top) : null,
    // ★ 工具条必须在主题列表**上方**（用户要求"前移"）
    toolbarAboveList: !!(tbR && liR && tbR.bottom <= liR.top + 4),
    overflowX: tb ? tb.scrollWidth - tb.clientWidth : null,
    tbRight: tbR ? Math.round(tbR.right) : null,
    hintLeft: hint ? Math.round(g(hint).left) : null,
    hintText: hint ? (hint.textContent || "").trim() : "",
    btns,
  };
};

// 定时任务弹层：可见性 / 勾选框几何（不能被 .llm-fields 的 CSS 撑满宽）/ 三个按钮是否可见
// ⚠️ 这个函数会被序列化后丢进浏览器执行 —— 它**只能用自己内部的定义**，
//    引用外层 Node 作用域的变量会在页面里报 ReferenceError。
const READ_SCHED_MODAL = () => {
  // ⚠️ 弹层是 position:fixed —— fixed 元素的 offsetParent 恒为 null，
  //    用 offsetParent 判可见性会得到「永远不可见」的假阴性。
  const isVisible = (el) => {
    if (!el) return false;
    if (el.classList && el.classList.contains("hidden")) return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const m = document.getElementById("sched-topic-modal");
  if (!m) return null;
  const panel = m.querySelector(".settings-panel");
  const list = document.getElementById("sched-topic-list");
  const cbs = list ? [...list.querySelectorAll(".sched-topic-cb")] : [];
  const pr = panel ? panel.getBoundingClientRect() : null;
  const first = cbs[0] ? cbs[0].getBoundingClientRect() : null;
  const btn = (id) => {
    const b = document.getElementById(id);
    if (!b) return null;
    const r = b.getBoundingClientRect();
    return { visible: isVisible(b), right: Math.round(r.right),
             left: Math.round(r.left), w: Math.round(r.width) };
  };
  return {
    visible: isVisible(m),
    inLlmFields: !!(list && list.closest(".llm-fields")),
    panelRight: pr ? Math.round(pr.right) : null,
    count: cbs.length,
    checked: cbs.filter(c => c.checked).length,
    // ★ checkbox 被 .llm-fields 的 CSS 撑满宽是本项目踩过的坑 → 宽度必须是个正常的方框
    cbW: first ? Math.round(first.width) : null,
    cbH: first ? Math.round(first.height) : null,
    hint: (document.getElementById("sched-topic-hint") || {}).textContent || "",
    fromSel: btn("sched-topic-from-sel"), all: btn("sched-topic-all"), reset: btn("sched-topic-reset"),
  };
};

// 从接口读回「定时任务主题」，用来验证页面勾选真的落库了（而不是只改了 DOM）
async function apiSchedTopics(page) {
  return await page.evaluate(async () => {
    const r = await fetch("/api/user/settings");
    const d = await r.json();
    return (d && d.schedule_topic_ids) || [];
  });
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_st_r_"));
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
    await p.waitForSelector("#topic-toolbar", { state: "attached", timeout: 8000 });
    await p.waitForTimeout(1200);   // 等主题加载完

    console.log("\n[A] 标题栏「🕒 定时任务」标记：没设定时就必须不显示");
    await p.waitForFunction(() => {
      const b = document.getElementById("health-badge");
      return !!b && !!b.dataset.overall;
    }, { timeout: 40000 });
    let tag = await p.evaluate(READ_EL, "#sched-tag");
    check("★ 元素存在（初始 admin 未开定时 → 隐藏）", tag && tag.visible === false,
          tag ? `visible=${tag.visible} text=${tag.text}` : "缺失");
    // 开启定时（走接口，然后重读状态让标记刷新）
    await p.evaluate(async () => {
      await fetch("/api/user/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ llm_provider: "deepseek", llm_base_url: "", llm_model: "", llm_api_key: "",
                               default_account_id: 0, schedule_enabled: true, schedule_time: "09:30",
                               schedule_account_ids: [], schedule_product_id: 0 }),
      });
      // 走「设置面板保存」后的同一条路径：只刷新标记，不碰体检徽章结论
      await refreshSchedTag();
    });
    await p.waitForTimeout(800);
    const tagFromSettings = await p.evaluate(READ_EL, "#sched-tag");
    check("★ 保存设置后标记立即出现（走 refreshSchedTag，不用等体检）",
          tagFromSettings && tagFromSettings.visible === true, String(tagFromSettings && tagFromSettings.visible));
    // 体检接口这条来源也要能驱动同一个标记
    await p.evaluate(() => loadHealth(false));
    await p.waitForTimeout(1500);
    tag = await p.evaluate(READ_EL, "#sched-tag");
    check("★ 开了定时 → 显示出来", tag && tag.visible === true, tag ? `visible=${tag.visible}` : "缺失");
    check("★ 标记悬停提示里带着定时时刻", /09:30/.test(tag.title), tag.title);
    check("★ 未指定主题时悬停提示显示「全部主题」", /全部主题/.test(tag.title), tag.title);
    check("★ 标记是 sky 系底色（不是被重映射成品牌红的 blue-*）",
          /badge-sched/.test(tag.cls), tag.cls.slice(0, 40));
    check("★ 标记排在体检徽章之后", await p.evaluate(() => {
      const b = document.getElementById("health-badge"), s = document.getElementById("sched-tag");
      return b && s && s.getBoundingClientRect().left >= b.getBoundingClientRect().right - 2;
    }), "");
    await shot(p, "st_01_sched_tag.png", "header");

    console.log("\n[B] 主题区工具条：前移 + 新增「🕒 定时任务」按键");
    const tb = await p.evaluate(READ_TOOLBAR);
    check("工具条存在", tb && tb.exists === true, "");
    check("★ 工具条在主题列表**上方**（前移生效）", tb.toolbarAboveList === true,
          `tbBottom=${tb.tbBottom} listTop=${tb.listTop}`);
    check("★ 工具条不在 .llm-fields 内（否则 checkbox/按钮会被表单 CSS 撑坏）",
          tb.inLlmFields === false, String(tb.inLlmFields));
    check("工具条不横向溢出", tb.overflowX === 0, `overflowX=${tb.overflowX}`);
    check("计数文案在工具条里（共 N 个主题 × M 篇）", /个主题/.test(tb.hintText), tb.hintText);
    const [bAll, bNone, bSched] = tb.btns;
    check("★ 三个按键都可见", [bAll, bNone, bSched].every(b => b && b.visible),
          JSON.stringify(tb.btns.map(b => b && b.visible)));
    check("★ 顺序：全选 → 全不选 → 定时任务",
          bAll.left < bNone.left && bNone.left < bSched.left,
          `${bAll.left} < ${bNone.left} < ${bSched.left}`);
    check("★ 「🕒 定时任务」按键文案正确", /定时任务/.test(bSched.text), bSched.text);
    check("★ 三个按键都没被挤出工具条（本项目经典坑）",
          [bAll, bNone, bSched].every(b => b.right <= tb.tbRight + 1),
          JSON.stringify({ btns: tb.btns.map(b => b.right), tbRight: tb.tbRight }));
    await shot(p, "st_02_toolbar.png", "#run-form");

    console.log("\n[C] 点「🕒 定时任务」→ 弹层里能看到勾选状态");
    await p.click("#topic-sched-btn");
    await p.waitForSelector("#sched-topic-modal:not(.hidden)", { timeout: 8000 });
    await p.waitForTimeout(600);
    let sm = await p.evaluate(READ_SCHED_MODAL);
    check("弹层打开", sm && sm.visible === true, sm ? String(sm.visible) : "缺失");
    check("★ 列出了主题（不是空列表）", sm.count > 0, `count=${sm.count}`);
    check("★ 未指定时默认全部勾选（= 发全部主题）", sm.checked === sm.count,
          `checked=${sm.checked}/${sm.count}`);
    check("★ 提示写明当前是「全部主题」", /全部/.test(sm.hint), sm.hint);
    check("★ 勾选框不在 .llm-fields 内", sm.inLlmFields === false, String(sm.inLlmFields));
    check("★ 勾选框是正常的方框（没被 CSS 撑满宽）", sm.cbW !== null && sm.cbW <= 24 && sm.cbW >= 10,
          `w=${sm.cbW} h=${sm.cbH}`);
    check("★ 三个辅助按键都可见且没被挤出面板",
          [sm.fromSel, sm.all, sm.reset].every(b => b && b.visible && b.right <= sm.panelRight + 1),
          JSON.stringify([sm.fromSel, sm.all, sm.reset]));
    await shot(p, "st_03_sched_modal.png", "#sched-topic-modal .settings-panel");

    console.log("\n[D] 取消勾选 → 真的从定时任务里移除（回查接口，不是只看页面）");
    const before = await apiSchedTopics(p);
    check("初始未指定（接口返回空）", before.length === 0, JSON.stringify(before));
    await p.evaluate(() => {
      const cbs = [...document.querySelectorAll("#sched-topic-list .sched-topic-cb")];
      cbs[0].click();     // 取消第一个
    });
    await p.waitForTimeout(1200);
    const afterRm = await apiSchedTopics(p);
    check("★ 取消一个后：接口里变成「指定了 count-1 个」",
          afterRm.length === sm.count - 1, `接口=${afterRm.length} 期望=${sm.count - 1}`);
    const sm2 = await p.evaluate(READ_SCHED_MODAL);
    check("★ 页面上勾选数同步减少", sm2.checked === sm.count - 1,
          `checked=${sm2.checked}/${sm2.count}`);
    check("★ 提示从「全部主题」变成「只发勾选的 N 个」", /只发勾选的/.test(sm2.hint), sm2.hint);
    // 关掉再开 → 应该是持久化的（读的是服务器上的列表，不是页面变量）
    await p.click('[data-close="sched-topic-modal"]');
    await p.waitForTimeout(300);
    await p.click("#topic-sched-btn");
    await p.waitForSelector("#sched-topic-modal:not(.hidden)", { timeout: 8000 });
    await p.waitForTimeout(800);
    const sm3 = await p.evaluate(READ_SCHED_MODAL);
    check("★ 重开弹层仍保持取消状态（真的落库了）", sm3.checked === sm.count - 1,
          `checked=${sm3.checked}/${sm3.count}`);

    console.log("\n[E] 「用主界面勾选」/「↺ 全部主题」");
    // 先把主界面改成只勾 3 个
    await p.click('[data-close="sched-topic-modal"]');
    await p.waitForTimeout(300);
    // ⚠️ 不能直接 forEach 点 checkbox：每次 change 都会 renderTopicSel() 重建 innerHTML，
    //    后面的节点已经脱离文档，点到的是"尸体"。直接改全局选中集合再重渲染。
    await p.evaluate(() => {
      SEL_TOPICS = new Set(TOPICS.slice(0, 3).map(t => t.id));
      TMP_TOPICS = [];
      saveSelTopics();
      renderTopicSel();
    });
    await p.waitForTimeout(500);
    const mainSel = await p.evaluate(() => SEL_TOPICS.size);
    check("主界面已改成只勾 3 个", mainSel === 3, `SEL_TOPICS=${mainSel}`);
    await p.click("#topic-sched-btn");
    await p.waitForSelector("#sched-topic-modal:not(.hidden)", { timeout: 8000 });
    await p.waitForTimeout(500);
    await p.click("#sched-topic-from-sel");
    await p.waitForTimeout(1200);
    const fromSel = await apiSchedTopics(p);
    check("★ 「用主界面勾选」把定时任务主题设成主界面那 3 个", fromSel.length === 3,
          `接口=${fromSel.length}`);
    const sm4 = await p.evaluate(READ_SCHED_MODAL);
    check("★ 页面勾选数 = 3", sm4.checked === 3, `checked=${sm4.checked}`);
    await p.click("#sched-topic-reset");
    await p.waitForTimeout(1200);
    const afterReset = await apiSchedTopics(p);
    check("★ 「↺ 全部主题」清空指定（= 恢复发全部主题）", afterReset.length === 0,
          `接口=${JSON.stringify(afterReset)}`);
    const sm5 = await p.evaluate(READ_SCHED_MODAL);
    check("★ 清空后页面恢复全部勾选", sm5.checked === sm5.count, `checked=${sm5.checked}/${sm5.count}`);

    console.log("\n[F] 页面没有 JS 报错");
    check("★ 全程无 pageerror", jsErrors.length === 0, jsErrors.slice(0, 2).join(" | "));

    console.log("\n" + "=".repeat(58));
    console.log(`=== 定时任务主题 渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
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
