// 测试：打开微信登录页，确认二维码能否渲染并截图
const { chromium } = require('playwright-core');

const EXE = 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe';
const PROFILE = 'C:/Users/Administrator/.workbuddy/wx-profile';

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: EXE,
    headless: true,
    viewport: { width: 1280, height: 800 },
    args: ['--no-sandbox'],
  });
  const page = await ctx.newPage();
  await page.goto('https://mp.weixin.qq.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(5000);
  const url = page.url();
  const title = await page.title();
  // 检查二维码相关元素
  const imgInfo = await page.evaluate(() =>
    Array.from(document.querySelectorAll('img')).map(i => ({
      src: (i.src || '').slice(0, 90),
      w: i.naturalWidth, h: i.naturalHeight,
    })).slice(0, 5)
  );
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/二维码_测试.png' });
  console.log('URL:', url);
  console.log('TITLE:', title);
  console.log('IMG:', JSON.stringify(imgInfo));
  console.log('已截图');
  await ctx.close();
})().catch(e => { console.error('ERROR:', e.message); process.exit(1); });
