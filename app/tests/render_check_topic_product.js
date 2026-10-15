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
    page.on("dialog", async (d) => { await d.accept(); });   // 批量归类带 confirm，headless 需自动接受
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

    // ---- 排版健康度（量化断言）----
    // 背景：列表/批量条曾误放在 .llm-fields 容器内，被
    // `label{display:block}` + `input,select{width:100%}` 污染 →
    // 勾选框被撑成 446px、主题名压成宽度 0 竖排、每行高 136px。这里用尺寸把它钉死。
    console.log("\n[D2] 主题库排版健康度（量化断言，防回归）");
    const layout = await page.evaluate(() => {
      const rows = [...document.querySelectorAll("#topic-list-mgr [id^=row-topic-]")];
      const r0 = rows[0];
      const cb = r0 && r0.querySelector(".topic-row-cb");
      const nm = r0 && r0.querySelector(".text-sm.font-medium");
      const bar = document.querySelector("#topic-bulk-bar");
      const R = (el) => { const x = el.getBoundingClientRect(); return { w: Math.round(x.width), h: Math.round(x.height) }; };
      return {
        maxRowH: rows.length ? Math.max(...rows.map(r => Math.round(r.getBoundingClientRect().height))) : 0,
        rowOverflow: r0 ? r0.scrollWidth - r0.clientWidth : 0,
        cbW: cb ? R(cb).w : 0,
        nameW: nm ? R(nm).w : 0,
        nameH: nm ? R(nm).h : 0,
        barH: bar ? R(bar).h : 0,
      };
    });
    check("列表每行高度正常（≤70px，没被撑高）", layout.maxRowH > 0 && layout.maxRowH <= 70,
          `最大 ${layout.maxRowH}px`);
    check("行内无横向溢出", layout.rowOverflow <= 0, `溢出 ${layout.rowOverflow}px`);
    check("勾选框尺寸正常（≤20px，没被 width:100% 撑满）", layout.cbW > 0 && layout.cbW <= 20, `${layout.cbW}px`);
    check("主题名单行显示（未折成竖排）", layout.nameW > 100 && layout.nameH <= 30,
          `w=${layout.nameW} h=${layout.nameH}`);
    check("批量操作条单行排布（≤44px）", layout.barH > 0 && layout.barH <= 44, `${layout.barH}px`);

    // ---- 按归属产品分组 + 折叠 + 组内全选 ----
    console.log("\n[D3] 主题按归属产品分组列出");
    const groups = await page.$$eval("#topic-list-mgr [data-group]", gs => gs.map(g => ({
      pid: g.dataset.group,
      name: (g.querySelector(".group-head .font-semibold") || {}).textContent?.trim() || "",
      cnt: g.querySelectorAll(".topic-row-cb").length,
      head: (g.querySelector(".group-head") || {}).textContent?.replace(/\s+/g, " ").trim() || "",
    })));
    check("存在多个分组（通用 + 各产品）", groups.length >= 3,
          groups.map(g => `${g.name}:${g.cnt}`).join(" / "));
    check("「通用」组排在最前", groups[0] && groups[0].name.includes("通用"), groups[0] && groups[0].name);
    check("组标题带条数", groups.every(g => /（\d+）/.test(g.head)), groups[0] && groups[0].head);
    const sumCnt = groups.reduce((s, g) => s + g.cnt, 0);
    const allRows = await page.$$eval("#topic-list-mgr .topic-row-cb", els => els.length);
    check("各分组条数之和 = 全部主题数", sumCnt === allRows, `${sumCnt}/${allRows}`);

    const g2 = groups[1];
    await page.$eval(`#topic-list-mgr [data-group="${g2.pid}"] .group-head`, el => el.click());
    await page.waitForTimeout(300);
    check("点组头可折叠该组",
          await page.$eval(`#topic-list-mgr [data-group="${g2.pid}"] .group-body`, el => el.classList.contains("hidden")));
    check("折叠后箭头变 ▸",
          (await page.$eval(`#topic-list-mgr [data-group="${g2.pid}"] .group-caret`, el => el.textContent.trim())) === "▸");
    await page.$eval(`#topic-list-mgr [data-group="${g2.pid}"] .group-head`, el => el.click());
    await page.waitForTimeout(300);
    check("再点一次可展开",
          await page.$eval(`#topic-list-mgr [data-group="${g2.pid}"] .group-body`, el => !el.classList.contains("hidden")));

    await page.$eval(`#topic-list-mgr [data-group="${g2.pid}"] .group-cb`, el => el.click());
    await page.waitForTimeout(300);
    const gChecked = await page.$$eval(`#topic-list-mgr [data-group="${g2.pid}"] .topic-row-cb:checked`, els => els.length);
    check("组头勾选框 = 全选本组", gChecked === g2.cnt, `${gChecked}/${g2.cnt}`);
    const topCnt = await page.$eval("#topic-sel-count", el => el.textContent || "");
    check("顶部计数同步更新", topCnt.includes(`已选 ${g2.cnt} 条`), topCnt);

    // ---- 批量归类（勾选多条 → 一次改归属） ----
    console.log("\n[E] 主题库管理页：批量归类");
    // ⚠️ 弹层在 [D] 阶段已打开：先关闭再重开，保证干净状态（否则点击会被遮罩拦截）
    await page.$eval('#topic-modal [data-close="topic-modal"]', el => el.click());
    await page.waitForTimeout(300);
    await page.$eval("#topic-btn", el => el.click());
    await page.waitForFunction(() => document.querySelectorAll("#topic-list-mgr .topic-row-cb").length > 0,
                               { timeout: 8000 });
    const total = await page.$$eval("#topic-list-mgr .topic-row-cb", els => els.length);
    check("每行都有勾选框", total > 0, `共${total}行`);

    // 弹层内部是滚动容器，用元素自身 click() 触发（等价触发 change，且不受坐标遮挡影响）
    await page.$eval("#topic-sel-all", el => el.click());
    await page.waitForTimeout(300);
    const cntTxt = await page.$eval("#topic-sel-count", el => el.textContent || "");
    check(`全选后计数为「已选 ${total} 条」`, cntTxt.includes(`已选 ${total} 条`), cntTxt);
    const checkedN = await page.$$eval("#topic-list-mgr .topic-row-cb:checked", els => els.length);
    check("全选后所有行都被勾选", checkedN === total, `checked=${checkedN}/${total}`);

    await page.selectOption("#topic-bulk-product", String(made.id2));   // 归到「ZZ美食街」
    await page.$eval("#topic-bulk-apply", el => el.click());
    await page.waitForTimeout(1800);
    const bmsg = await page.$eval("#topic-bulk-msg", el => el.textContent || "");
    check("批量归类后显示成功提示", bmsg.includes("已归类"), bmsg);
    const badges = await page.$$eval("#topic-list-mgr [id^=row-topic-]", els => els.map(e => e.textContent || ""));
    check("管理页所有主题徽章都变成「ZZ美食街」",
          badges.length > 0 && badges.every(t => t.includes("ZZ美食街")), `共${badges.length}行`);

    // 归类后：木梳下应看不到任何主题（全被挪到美食街）★核心
    await page.$eval('#topic-modal [data-close="topic-modal"]', el => el.click());
    await page.waitForTimeout(400);
    await page.selectOption("#product_id", String(made.id1));
    await page.waitForTimeout(1500);
    const namesM = await page.$$eval("#topic-list .topic-cb", els => els.map(e => e.dataset.name));
    check("全部归类到美食街后：选木梳已无主题 ★核心", namesM.length === 0, `共${namesM.length}个`);
    await page.selectOption("#product_id", String(made.id2));
    await page.waitForTimeout(1500);
    const namesF = await page.$$eval("#topic-list .topic-cb", els => els.map(e => e.dataset.name));
    check("选美食街则有主题（归类生效）", namesF.length > 0, `共${namesF.length}个`);

    // ---- 主界面：一键全选 / 一键全不选 ----
    console.log("\n[F] 主界面：一键全选 / 全不选");
    const nBefore = await page.$$eval("#topic-list .topic-cb", els => els.length);
    const checkedBefore = await page.$$eval("#topic-list .topic-cb:checked", els => els.length);
    check("初始：当前产品的主题默认全被勾选", nBefore > 0 && checkedBefore === nBefore,
          `${checkedBefore}/${nBefore}`);

    // 顺带验证临时主题也会被「全不选」清掉
    await page.fill("#new_topic", "ZZ临时主题");
    await page.$eval("#add-topic-tmp", el => el.click());
    await page.waitForTimeout(300);
    const tmpN = await page.$$eval("#topic-list .tmp-cb", els => els.length);
    check("已加入 1 个临时主题（且默认勾选）", tmpN === 1, `tmp=${tmpN}`);

    await page.$eval("#topic-sel-none-btn", el => el.click());
    await page.waitForTimeout(400);
    const checkedNone = await page.$$eval("#topic-list .topic-cb:checked", els => els.length);
    check("点「全不选」→ 勾选数归 0 ★核心", checkedNone === 0, `checked=${checkedNone}`);
    const tmpAfter = await page.$$eval("#topic-list .tmp-cb", els => els.length);
    check("「全不选」把临时主题也清掉", tmpAfter === 0, `tmp=${tmpAfter}`);
    const hintNone = await page.$eval("#topic-sel-hint", el => el.textContent || "");
    check("「全不选」后计数显示 0 个主题", hintNone.includes("共 0 个主题"), hintNone);

    await page.$eval("#topic-sel-all-btn", el => el.click());
    await page.waitForTimeout(400);
    const checkedAll = await page.$$eval("#topic-list .topic-cb:checked", els => els.length);
    check("点「全选」→ 全部重新勾选 ★核心", checkedAll === nBefore, `checked=${checkedAll}/${nBefore}`);
    const hintAll = await page.$eval("#topic-sel-hint", el => el.textContent || "");
    check("「全选」后计数恢复", hintAll.includes(`共 ${nBefore} 个主题`), hintAll);

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
