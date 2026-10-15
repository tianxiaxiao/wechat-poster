/**
 * render_check_status_ip.js — 「能否生成/发布状态 + 取 Key 直达 + 出口 IP」的**渲染断言**
 * ============================================================================
 * 为什么：这三处全是"用户看得见、接口测不到"的东西。接口 200 但页面看不见/被挤出视野
 *         在本项目已经踩过三次（二维码写 localhost、hidden 被 CSS 压制、按钮溢出）。
 * 本脚本用无头 Chrome 真实登录，逐条读 **DOM + 计算样式 + 几何**，验证：
 *   [A] 顶部徽章 #health-badge：在模型标签之后、可点、结论会真的落到页面上（不是永远"体检中"）
 *       ★ 徽章颜色确实随状态切换（badge-warn/badge-fail 有真实背景色，不是空 class）
 *   [B] 「🔑 去 xx 获取 Key」：按来源换平台、新标签打开、认不出的来源**隐藏**而不是乱跳
 *   [C] 公众号弹层里的出口 IP：IP 条在 .llm-fields **之外**（否则会被表单样式撑坏）、
 *       按钮不被挤出视野、IP 是合法 IPv4 或明确"未取到"（不假装成功）
 * 临时库 + 独立端口(8802)，不触发真实发布。
 * 运行：node tests/render_check_status_ip.js      （SHOT=1 时截图到项目根 .tmp_layout/）
 */
"use strict";
const path = require("path");
const fs = require("fs");
const os = require("os");
const { execFileSync, spawn } = require("child_process");
const { chromium } = require(path.resolve(__dirname, "../../wechat/node_modules/playwright-core"));

const APP_DIR = path.resolve(__dirname, "..");
const PORT = process.env.RENDER_PORT5 || "8802";
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
const IPV4 = /^(\d{1,3}\.){3}\d{1,3}$/;

// 读徽章：文本 / class / 背景色 / 几何 / 与模型标签的相对位置
const READ_BADGE = () => {
  const b = document.getElementById("health-badge");
  const tag = document.getElementById("provider-tag");
  if (!b) return null;
  const cs = getComputedStyle(b);
  const r = b.getBoundingClientRect(), tr = tag ? tag.getBoundingClientRect() : null;
  return {
    text: (b.textContent || "").trim(), cls: b.className, bg: cs.backgroundColor, color: cs.color,
    overall: b.dataset.overall || "", title: b.title || "", cursor: cs.cursor,
    tagName: b.tagName, h: Math.round(r.height), top: Math.round(r.top), right: Math.round(r.right),
    afterTag: !!(tr && r.left >= tr.right - 2),
    sameLine: !!(tr && Math.abs(r.top - tr.top) < 24),
    visible: b.offsetParent !== null,
  };
};
// 读「去获取 Key」链接
const READ_KEYGET = () => {
  const a = document.getElementById("key-get");
  if (!a) return null;
  return { href: a.getAttribute("href") || "", text: (a.textContent || "").trim(),
           target: a.getAttribute("target") || "", rel: a.getAttribute("rel") || "",
           hidden: a.classList.contains("hidden"), visible: a.offsetParent !== null,
           hint: (document.getElementById("key-src-hint") || {}).textContent || "" };
};
// 读出口 IP 条
const READ_IPBAR = () => {
  const bar = document.getElementById("acct-ipbar");
  const ip = document.getElementById("acct-ip");
  if (!bar || !ip) return null;
  const br = bar.getBoundingClientRect();
  const btn = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { right: Math.round(r.right), left: Math.round(r.left), visible: el.offsetParent !== null };
  };
  const link = document.getElementById("acct-mp-link");
  const row = document.getElementById("acct-ip-row");
  const ipr = ip.getBoundingClientRect();
  const mpr = link ? link.getBoundingClientRect() : null;
  const rr = row ? row.getBoundingClientRect() : null;
  return {
    inLlmFields: !!bar.closest(".llm-fields"),
    ipText: (ip.textContent || "").trim(), ipData: ip.dataset.ip || "",
    ipWidth: Math.round(ipr.width),
    // ★ 「打开微信开发者平台」必须和出口 IP 在同一排（同一行的垂直中心差 < 12px）
    mpSameRow: !!(mpr && Math.abs((mpr.top + mpr.height / 2) - (ipr.top + ipr.height / 2)) < 12),
    rowOverflowX: rr ? row.scrollWidth - row.clientWidth : null,
    rowH: rr ? Math.round(rr.height) : null,
    overflowX: bar.scrollWidth - bar.clientWidth,
    barRight: Math.round(br.right), h: Math.round(br.height),
    refresh: btn("acct-ip-refresh"), copy: btn("acct-ip-copy"), mp: btn("acct-mp-link"),
    mpHref: link ? link.getAttribute("href") : "", mpTarget: link ? link.getAttribute("target") : "",
    mpText: link ? (link.textContent || "").trim() : "",
    note: (document.getElementById("acct-ip-note") || {}).textContent || "",
    note2: (document.getElementById("acct-ip-note2") || {}).textContent || "",
    visible: bar.offsetParent !== null,
  };
};

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "poster_si_r_"));
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
                // ⚠️ 故意给一个**假** Key：模型体检必然失败，正好验证徽章会变红而不是假装绿
                DEEPSEEK_API_KEY: "sk-FAKE-KEY-FOR-RENDER-CHECK" };
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
    await login(p, "admin", ADMIN_PW);

    console.log("\n[A] 顶部状态徽章：模型标签之后就能看到「能不能生成/发布」");
    await p.waitForSelector("#health-badge", { state: "attached", timeout: 8000 });
    // ⚠️ 必须等"结论真的落到了页面上"：初始文案是「⚪ 体检中…」，
    //    只判断元素存在等于没等 —— 用 data-overall 出现作为信号（undefined 会一直为真）
    await p.waitForFunction(() => {
      const b = document.getElementById("health-badge");
      return !!b && !!b.dataset.overall;
    }, { timeout: 40000 });
    let bd = await p.evaluate(READ_BADGE);
    check("徽章存在且可见", bd && bd.visible === true, bd ? bd.text : "缺失");
    check("★ 徽章排在模型标签之后（用户要求的显示位置）", bd.afterTag === true,
          `afterTag=${bd.afterTag}`);
    check("★ 与模型标签同一行（没把标题撑成两行）", bd.sameLine === true, `sameLine=${bd.sameLine}`);
    check("徽章高度正常（单行小胶囊，≤40px）", bd.h <= 40, `h=${bd.h}px`);
    check("★ 结论不是永远的「体检中…」", bd.overall.length > 0 && !/体检中/.test(bd.text),
          `overall=${bd.overall} text=${bd.text}`);
    check("overall 是四类之一", ["ok", "warn", "bad", "unknown"].includes(bd.overall), bd.overall);
    check("是 button 且鼠标是可点样式", bd.tagName === "BUTTON" && bd.cursor === "pointer",
          `${bd.tagName}/${bd.cursor}`);
    check("★ title 明说不会发布文章（打消顾虑）", /不会发布文章/.test(bd.title), bd.title.slice(0, 40));

    console.log("\n[A2] 徽章颜色确实随状态切换（不是挂了个空 class）");
    // 直接把四种 class 逐个套上去读背景色：空 class / 拼错 class 会在这里露馅
    const clsBg = await p.evaluate(() => {
      const b = document.getElementById("health-badge");
      const keep = b.className;
      const out = {};
      for (const c of ["badge-ok", "badge-warn", "badge-fail", "badge-pending"]) {
        b.className = "step-badge " + c + " cursor-pointer";
        out[c] = getComputedStyle(b).backgroundColor;
      }
      b.className = keep;
      return out;
    });
    const uniq = new Set(Object.values(clsBg));
    check("★ 四种状态背景色互不相同（颜色真的生效）", uniq.size === 4, JSON.stringify(clsBg));
    check("badge-warn 有实际底色（不是透明）",
          clsBg["badge-warn"] && clsBg["badge-warn"] !== "rgba(0, 0, 0, 0)", clsBg["badge-warn"]);
    check("badge-fail 有实际底色（不是透明）",
          clsBg["badge-fail"] && clsBg["badge-fail"] !== "rgba(0, 0, 0, 0)", clsBg["badge-fail"]);
    // 假 Key → 体检必然失败 → 徽章应该是红色（badge-fail）
    check("★ 假 Key 下徽章呈失败色（不假装绿）", bd.overall === "bad" && /badge-fail/.test(bd.cls),
          `overall=${bd.overall} cls=${bd.cls}`);
    await shot(p, "si_01_badge.png", "header");

    console.log("\n[A3] 点徽章 = 重新体检（有加载态，且不会发文章）");
    let clicked = false;
    p.evaluate(() => {
      window.__jobsBefore = 0;
      fetch("/api/jobs").then(r => r.json()).then(j => { window.__jobsBefore = (j || []).length; });
    });
    // ⚠️ 不能用「点完 sleep 150ms 再读」：接口快的时候已经换回结论了（曾因此误报）。
    //    改成挂 MutationObserver 记录**所有**文案变化，加载态出现过就算数。
    await p.evaluate(() => {
      window.__seq = [];
      const b = document.getElementById("health-badge");
      window.__seq.push((b.textContent || "").trim());
      window.__mo = new MutationObserver(() => window.__seq.push((b.textContent || "").trim()));
      window.__mo.observe(b, { childList: true, characterData: true, subtree: true });
    });
    await p.click("#health-badge");
    clicked = true;
    await p.waitForTimeout(300);
    const during = await p.evaluate(() => (document.getElementById("health-badge").textContent || "").trim());
    const seq = await p.evaluate(() => window.__seq.slice());
    check("★ 点击后出现加载态（⏳ 体检中…）", seq.some(t => /体检中/.test(t)),
          JSON.stringify(seq) + " now=" + during);
    await p.waitForFunction(() => {
      const b = document.getElementById("health-badge");
      return !!b && !!b.dataset.overall && !/体检中|状态未知/.test(b.textContent || "");
    }, { timeout: 40000 });
    const after = await p.evaluate(READ_BADGE);
    check("★ 体检完成后重新给出结论", after.overall.length > 0, `${after.overall} ${after.text}`);
    check("★ 体检过程没有产生发布任务",
          (await p.evaluate(async () => (await (await fetch("/api/jobs")).json()).length)) === 0
          && clicked, "jobs=0");

    console.log("\n[B] AI 模型设置：「去获取 Key」直达对应平台");
    await p.click("#gear-btn");
    await p.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await p.selectOption("#provider", "deepseek");
    await p.waitForTimeout(300);
    let kg = await p.evaluate(READ_KEYGET);
    check("★ DeepSeek：链接可见", kg && kg.visible === true, `hidden=${kg && kg.hidden}`);
    check("★ DeepSeek：链接指向官方发 Key 页", /platform\.deepseek\.com\/api_keys/.test(kg.href), kg.href);
    check("链接文案标明了平台名", /DeepSeek/.test(kg.text), kg.text);
    check("新标签页打开且带 noopener", kg.target === "_blank" && /noopener/.test(kg.rel),
          `${kg.target}/${kg.rel}`);

    // 换 openai_compat + 各家 base_url：应按主机名换平台
    await p.selectOption("#provider", "openai_compat");
    const cases = [
      ["https://api.siliconflow.cn/v1", /siliconflow\.cn/, /硅基流动/],
      ["https://dashscope.aliyuncs.com/compatible-mode/v1", /bailian\.console\.aliyun\.com/, /百炼/],
      ["https://open.bigmodel.cn/api/paas/v4", /open\.bigmodel\.cn/, /智谱/],
    ];
    for (const [url, reHref, reName] of cases) {
      await p.fill("#base_url", url);
      await p.waitForTimeout(250);
      const g = await p.evaluate(READ_KEYGET);
      check(`★ base_url=${new URL(url).host} → 直达对应平台`,
            reHref.test(g.href) && reName.test(g.text) && g.visible === true, `${g.text} → ${g.href}`);
    }
    await p.fill("#base_url", "");
    await p.waitForTimeout(250);
    kg = await p.evaluate(READ_KEYGET);
    check("★ 认不出的来源：链接隐藏（不乱跳到别人的平台）", kg.hidden === true && kg.visible === false,
          `hidden=${kg.hidden}`);
    check("隐藏时给出兜底说明", /没有统一的发 Key 页面|请在你的网关/.test(kg.hint), kg.hint.slice(0, 40));
    await shot(p, "si_02_keyget.png", "#settings-modal .settings-panel");

    console.log("\n[C] 公众号账号管理：出口 IP + 打开后台");
    await p.click("#settings-close");
    await p.click("#acct-btn");
    await p.waitForSelector("#acct-modal:not(.hidden)", { timeout: 8000 });
    await p.waitForFunction(() => {
      const el = document.getElementById("acct-ip");
      return !!el && !/查询中/.test(el.textContent || "");
    }, { timeout: 30000 });
    let ib = await p.evaluate(READ_IPBAR);
    check("IP 条可见", ib && ib.visible === true, ib ? `h=${ib.h}px` : "缺失");
    check("★ IP 条在 .llm-fields **之外**（否则会被表单样式撑成满宽）", ib.inLlmFields === false,
          `inLlmFields=${ib.inLlmFields}`);
    check("★ IP 是合法 IPv4 或明确「未取到」（不拿空串假装成功）",
          IPV4.test(ib.ipText) || ib.ipText === "未取到", `ip=${ib.ipText}`);
    check("IP 同时写进 data-ip（供复制用）", ib.ipData === ib.ipText || ib.ipText === "未取到",
          `data=${ib.ipData}`);
    check("★ IP 条内不横向溢出", ib.overflowX <= 0, `overflow=${ib.overflowX}px`);
    check("★ 三个按钮/链接都没被挤出视野（本项目经典坑）",
          [ib.refresh, ib.copy, ib.mp].every(b => b && b.visible && b.right <= ib.barRight + 1),
          JSON.stringify({ refresh: ib.refresh, copy: ib.copy, mp: ib.mp, barRight: ib.barRight }));
    // 2026-10-16：「开发接口管理」已迁移到微信开发者平台，旧入口 mp.weixin.qq.com 不再管白名单
    check("★ 按钮文案是「打开微信开发者平台」", /打开微信开发者平台/.test(ib.mpText), ib.mpText);
    check("★ 指向 developers.weixin.qq.com/platform 且新标签打开",
          /developers\.weixin\.qq\.com\/platform/.test(ib.mpHref) && ib.mpTarget === "_blank",
          `${ib.mpHref} target=${ib.mpTarget}`);
    check("★ 第一排 = 白名单填写路径（含「API IP白名单」）", /API IP白名单/.test(ib.note), ib.note.slice(0, 60));
    check("★ 路径是「微信开发者平台 → 公众号/服务号 → 基础信息」",
          /微信开发者平台/.test(ib.note) && /公众号\/服务号/.test(ib.note) && /基础信息/.test(ib.note),
          ib.note.slice(0, 60));
    check("★ 不再出现迁移前的旧路径（安全中心 / 基本配置）",
          !/安全中心/.test(ib.note) && !/基本配置/.test(ib.note), ib.note.slice(0, 60));
    check("★ 第二排 = 上次验证通过的 IP", /^上次验证通过的 IP：/.test(ib.note2.trim()), ib.note2.slice(0, 60));
    check("★ 两排文案不混在一起（第一排不含「上次验证通过」）", !/上次验证通过/.test(ib.note), ib.note.slice(0, 40));
    await shot(p, "si_03_ipbar.png", "#acct-modal .settings-panel");

    console.log("\n[C2] 打开微信开发者平台固定在出口 IP 那一排（不换行、不溢出）");
    check("★ 「打开微信开发者平台」与 IP 框同一排", ib.mpSameRow === true, `sameRow=${ib.mpSameRow}`);
    check("★ IP 那一行不横向溢出", ib.rowOverflowX !== null && ib.rowOverflowX <= 0,
          `rowOverflow=${ib.rowOverflowX}px rowH=${ib.rowH}px`);
    check("★ IP 那一行是单行高度（没被挤成两行）", ib.rowH !== null && ib.rowH <= 34, `rowH=${ib.rowH}px`);

    console.log("\n[C3] IP 框固定大小：查询中 / 有结果，宽度必须一致");
    // ⚠️ 同样不能靠固定 sleep：挂 MutationObserver 记录每次文案变化时的实际宽度
    await p.evaluate(() => {
      window.__w = [];
      const el = document.getElementById("acct-ip");
      window.__w.push(el.offsetWidth);
      window.__mo2 = new MutationObserver(() => window.__w.push(el.offsetWidth));
      window.__mo2.observe(el, { childList: true, characterData: true, subtree: true });
    });
    await p.click("#acct-ip-refresh");
    await p.waitForTimeout(150);
    const during2 = await p.evaluate(() => (document.getElementById("acct-ip").textContent || "").trim());
    await p.waitForFunction(() => {
      const el = document.getElementById("acct-ip");
      return !!el && !/查询中/.test(el.textContent || "");
    }, { timeout: 30000 });
    const after2 = await p.evaluate(READ_IPBAR);
    const widths = await p.evaluate(() => { window.__mo2.disconnect(); return window.__w.slice(); });
    check("★ 刷新时有「查询中…」反馈", /查询中/.test(during2), during2);
    check("★ 刷新后仍给出确定结论", IPV4.test(after2.ipText) || after2.ipText === "未取到", after2.ipText);
    check("★ 整个过程中 IP 框宽度恒定（不因文案长短跳动）",
          widths.length >= 2 && new Set(widths).size === 1,
          `采集到的宽度=${JSON.stringify(widths)}`);
    check("★ 第二排仍显示上次验证通过的 IP", /上次验证通过的 IP：/.test(after2.note2), after2.note2.slice(0, 50));

    console.log("\n[C4] AppID / AppSecret 的获取位置提示（2026-10-16 改口径）");
    const ph = await p.evaluate(() => ({
      appid: document.getElementById("acct_appid").placeholder,
      secret: document.getElementById("acct_secret").placeholder,
      appidW: document.getElementById("acct_appid").clientWidth,
    }));
    check("★ AppID 输入框提示改为「微信开发者平台-公众号/服务号-基础信息」",
          /微信开发者平台-公众号\/服务号-基础信息/.test(ph.appid), ph.appid);
    check("★ AppSecret 输入框提示同样改口径",
          /微信开发者平台-公众号\/服务号-基础信息/.test(ph.secret), ph.secret);
    check("★ 旧口径「公众号后台-设置与开发」已不再出现",
          !/公众号后台-设置与开发/.test(ph.appid + ph.secret), ph.appid);
    check("提示不会被输入框截断（框够宽）", ph.appidW >= 300, `w=${ph.appidW}px`);

    console.log("\n[D] 保存模型设置后：旧结论标记过期（不继续显示绿/红）");
    await p.click('[data-close="acct-modal"]');
    await p.click("#acct-btn");                              // 关掉再开，回到设置弹层不方便，重开一次即可
    await p.waitForTimeout(200);
    await p.click('[data-close="acct-modal"]');
    await p.click("#gear-btn");
    await p.waitForSelector("#settings-modal:not(.hidden)", { timeout: 8000 });
    await p.selectOption("#provider", "deepseek");
    await p.fill("#api_key", "sk-changed-by-render-check");
    await p.click("#settings-save");
    await p.waitForTimeout(600);
    const bd2 = await p.evaluate(READ_BADGE);
    check("★ 保存后徽章回到「未体检 · 点击体检」（不沿用旧结论）",
          bd2.overall === "unknown" && /点击体检/.test(bd2.text), `${bd2.overall} ${bd2.text}`);

    console.log("\n" + "=".repeat(58));
    console.log(`=== 状态/取Key/出口IP 渲染断言：通过 ${pass} / 共 ${pass + fail}；失败 ${fail} ===`);
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
