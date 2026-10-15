/* 全量一致性回归：一个浏览器会话跑完 5 主题 × N 篇文章，
 * 逐条对比「原系统 buildWechatClipboardHtml 产物」与「我们 render.js 产物」是否逐字节一致。 */
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright-core");
const { buildWechatHtml } = require("./render.js");

const MD2CARD = "file:///D:/多平台图文生产系统/md2card-local/index.html";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const THEMES = ["alibaba", "bytedance", "notebook", "business", "japanese-magazine"];

(async () => {
  const files = process.argv.slice(2);
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage();
  await page.goto(MD2CARD, { waitUntil: "load" });
  await page.waitForTimeout(1500);

  let pass = 0, fail = 0;
  for (const f of files) {
    const md = fs.readFileSync(f, "utf8");
    await page.evaluate(mdText => {
      const ta = document.querySelector("#md");
      ta.value = mdText;
      ta.dispatchEvent(new Event("input", { bubbles: true }));
    }, md);
    await page.evaluate(() => {
      const cb = document.querySelector("#wechat-mode");
      if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change", { bubbles: true })); }
    });
    await page.waitForTimeout(400);
    for (const tid of THEMES) {
      const idx = await page.evaluate(t => (window.__md2card.data.mpThemes || []).findIndex(x => x.id === t), tid);
      await page.evaluate(i => { document.querySelectorAll("#theme-grid .mp-theme-item")[i].click(); }, idx);
      await page.waitForTimeout(250);
      const origHtml = await page.evaluate(() => window.__md2card.buildWechatClipboardHtml().html);
      const mineHtml = buildWechatHtml(md, tid);
      const same = origHtml === mineHtml;
      same ? pass++ : fail++;
      console.log(`${same ? "✅ identical" : "❌ DIFF"}  ${path.basename(f)}  ${tid}  orig=${origHtml.length} mine=${mineHtml.length}`);
      if (!same) {
        fs.writeFileSync(`diff_${path.basename(f)}_${tid}.orig.html`, origHtml, "utf8");
        fs.writeFileSync(`diff_${path.basename(f)}_${tid}.mine.html`, mineHtml, "utf8");
      }
    }
  }
  await browser.close();
  console.log(`\n=== 通过 ${pass} / 不一致 ${fail} ===`);
  process.exit(fail ? 1 : 0);
})();
