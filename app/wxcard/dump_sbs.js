/* 单会话导出：一次浏览器会话内产出「原系统产物」与「本工具产物」，
 * 并生成并排对照预览页 preview_compare.html。
 * 用法：node dump_sbs.js <md文件> */
"use strict";
const fs = require("fs");
const { chromium } = require("playwright-core");
const { buildWechatHtml } = require("./render.js");

const MD2CARD = "file:///D:/多平台图文生产系统/md2card-local/index.html";
const CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe";
const THEMES = [["alibaba", "阿里橙"], ["bytedance", "字节范"], ["notebook", "笔记本"],
                ["business", "商务简报"], ["japanese-magazine", "日本杂志"]];

(async () => {
  const mdFile = process.argv[2];
  const md = fs.readFileSync(mdFile, "utf8");
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage();
  await page.goto(MD2CARD, { waitUntil: "load" });
  await page.waitForTimeout(1500);
  await page.evaluate(m => {
    const ta = document.querySelector("#md");
    ta.value = m; ta.dispatchEvent(new Event("input", { bubbles: true }));
  }, md);
  await page.evaluate(() => {
    const cb = document.querySelector("#wechat-mode");
    if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event("change", { bubbles: true })); }
  });
  await page.waitForTimeout(500);

  const blocks = [];
  for (const [tid, name] of THEMES) {
    const idx = await page.evaluate(t => window.__md2card.data.mpThemes.findIndex(x => x.id === t), tid);
    await page.evaluate(i => document.querySelectorAll("#theme-grid .mp-theme-item")[i].click(), idx);
    await page.waitForTimeout(300);
    const orig = await page.evaluate(() => window.__md2card.buildWechatClipboardHtml().html);
    const mine = buildWechatHtml(md, tid);
    const same = orig === mine;
    console.log(`${same ? "✅" : "❌"} ${name}(${tid}) 一致=${same} orig=${orig.length} mine=${mine.length}`);
    blocks.push(
      `<section style="margin:0 0 8px"><b style="font:600 15px/1.6 sans-serif;color:#111">${name}（${tid}）`
      + `　<span style="color:${same ? "#0a7d32" : "#c00"}">${same ? "与原系统逐字节一致 ✅" : "存在差异 ❌"}</span></b></section>`
      + `<div style="display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap">`
      + `<div style="flex:1;min-width:280px"><div style="font:12px sans-serif;color:#888;margin-bottom:6px">原系统（md2card 公众号模式）</div>`
      + `<div style="background:#fff;border:1px solid #e5e5e5;border-radius:6px;padding:12px">${orig}</div></div>`
      + `<div style="flex:1;min-width:280px"><div style="font:12px sans-serif;color:#888;margin-bottom:6px">本工具（发布用排版）</div>`
      + `<div style="background:#fff;border:1px solid #e5e5e5;border-radius:6px;padding:12px">${mine}</div></div>`
      + `</div>`);
  }
  await browser.close();

  const page_html = '<!doctype html><html><head><meta charset="utf-8"><title>原系统 vs 本工具 · 公众号排版对照</title></head>'
    + '<body style="background:#f0f2f5;margin:0;padding:24px;font-family:sans-serif">'
    + '<h1 style="max-width:1200px;margin:0 auto 16px;font-size:20px;color:#111">公众号模式排版对照：原「多平台图文生产系统」 vs 本工具（逐字节一致）</h1>'
    + '<div style="max-width:1200px;margin:0 auto">' + blocks.join('<hr style="margin:28px 0;border:0;border-top:1px solid #ddd">') + "</div></body></html>";
  fs.writeFileSync("preview_compare.html", page_html, "utf8");
  console.log("\n预览已生成: wxcard/preview_compare.html");
})();
