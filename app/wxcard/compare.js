/* 对比脚本 v2：无头 Chrome 打开原 md2card，模拟真实 UI 操作（填 markdown → 开公众号模式 → 选主题），
 * 取【官方产物 HTML】与【实际生效的 #card-css 全文】，和我们的 render.js 逐项对比。 */
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");

const MD2CARD = "file:///D:/多平台图文生产系统/md2card-local/index.html";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";

(async () => {
  const [, , mdFile, themeId = "alibaba", outDir = "."] = process.argv;
  const md = fs.readFileSync(mdFile, "utf8");

  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(String(e.message).slice(0, 200)));
  await page.goto(MD2CARD, { waitUntil: "load" });
  await page.waitForTimeout(1500);

  // 1) 填入 markdown
  await page.evaluate(mdText => {
    const ta = document.querySelector("#md");
    ta.value = mdText;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  }, md);
  // 2) 开启公众号模式
  await page.evaluate(() => {
    const cb = document.querySelector("#wechat-mode");
    if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change", { bubbles: true })); }
  });
  await page.waitForTimeout(600);
  // 3) 点选目标公众号主题
  const clicked = await page.evaluate(tid => {
    const items = Array.from(document.querySelectorAll("#theme-grid .mp-theme-item"));
    const idx = (window.__md2card.data.mpThemes || []).findIndex(t => t.id === tid);
    if (idx < 0 || !items[idx]) return false;
    items[idx].click();
    return true;
  }, themeId);
  await page.waitForTimeout(600);

  const orig = await page.evaluate(() => {
    const r = window.__md2card.buildWechatClipboardHtml();
    const cssEl = document.querySelector("#card-css");
    return { html: r.html, themeName: r.themeName, usedMp: r.usedMp, css: cssEl ? cssEl.textContent : "" };
  });
  await browser.close();

  const mine = require("./render.js").buildWechatHtml(md, themeId);

  fs.mkdirSync(outDir, { recursive: true });
  const p = n => path.join(outDir, n);
  fs.writeFileSync(p(`orig_${themeId}.html`), orig.html, "utf8");
  fs.writeFileSync(p(`mine_${themeId}.html`), mine, "utf8");
  fs.writeFileSync(p(`orig_${themeId}.css`), orig.css, "utf8");

  console.log(JSON.stringify({
    theme: themeId, themeName: orig.themeName, usedMp: orig.usedMp, clicked,
    origBytes: orig.html.length, mineBytes: mine.length,
    identical: orig.html === mine,
    cssLen: orig.css.length, pageErrors: errors.slice(0, 3)
  }, null, 1));
})();
