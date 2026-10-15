// probe_original.js — 打开发布页，dump "原创" 设置行的完整 DOM 结构
const { chromium } = require('playwright-core');
const EXE = 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe';
const PROFILE = 'C:/Users/Administrator/.workbuddy/wx-profile';
(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, { executablePath: EXE, headless: false, viewport: { width: 1500, height: 950 }, args: ['--window-position=0,0'], ignoreDefaultArgs: ['--enable-automation'] });
  const page = ctx.pages()[0] || await ctx.newPage();
  await page.goto('https://mp.weixin.qq.com/', { waitUntil: 'domcontentloaded', timeout: 40000 });
  await page.waitForTimeout(4000);
  const token = (page.url().match(/token=(\d+)/) || [])[1] || '';
  await page.goto('https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit&action=edit&isNew=1&type=77&token=' + token + '&lang=zh_CN', { waitUntil: 'domcontentloaded', timeout: 40000 });
  await page.waitForTimeout(6000);
  // dump 原创/赞赏 两行的 outerHTML
  const html = await page.evaluate(function () {
    var groups = Array.from(document.querySelectorAll('.appmsg-editor__setting-group, .setting-group'));
    var out = [];
    groups.forEach(function (g) {
      var t = (g.textContent || '').replace(/\s+/g, ' ').slice(0, 30);
      if (/原创|赞赏|声明/.test(t) || /js_original|js_reward|js_edit_ori|js_appmsg_editor_reward/.test(String(g.className))) {
        out.push('### ' + g.tagName + ' .' + String(g.className).slice(0, 90) + ' text=' + t);
        out.push(g.outerHTML.slice(0, 1200));
      }
    });
    return out;
  }).catch(function (e) { return ['ERR ' + e.message]; });
  html.forEach(function (x) { console.log(x); console.log('--------'); });
  await ctx.close();
})().catch(function (e) { console.error('FATAL', e.message); process.exit(1); });
