const { chromium } = require('playwright-core');
const fs = require('fs');
const EXE = 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe';
const PROFILE = 'C:/Users/Administrator/.workbuddy/wx-profile';
(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: EXE, headless: true,
    viewport: { width: 1280, height: 800 }, args: ['--no-sandbox'],
  });
  const page = await ctx.newPage();
  await page.goto('https://mp.weixin.qq.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(6000);
  const url = page.url();
  const txt = await page.evaluate(() => document.body ? document.body.innerText : '');
  const loggedIn = /首页|内容与互动|草稿箱|图文消息|接口权限|素材库/.test(txt) || /cgi-bin\/home|token=/.test(url);
  console.log('LOGGED_IN=' + loggedIn);
  console.log('URL=' + url);
  console.log('TEXT_START:/n' + txt.slice(0, 600));
  console.log('TEXT_END');
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/当前页面.png' });
  await ctx.close();
})().catch(e => { console.error('ERROR:', e.message); process.exit(1); });
