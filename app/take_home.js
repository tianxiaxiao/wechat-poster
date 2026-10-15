// take_home.js — 截图首页给用户预览
const { chromium } = require('playwright-core');
(async () => {
  const browser = await chromium.launchPersistentContext('C:/Users/Administrator/.workbuddy/wx-profile', {
    executablePath: 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe',
    headless: true, viewport: { width: 900, height: 1100 },
  });
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:8765/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 'D:/测试公众号批量生成/app/home.png', fullPage: true });
  console.log('截图完成');
  await browser.close();
})();
