// 打开微信登录页 -> 循环检测登录状态并实时记录页面变化 -> 登录成功即保存状态退出
const { chromium } = require('playwright-core');
const fs = require('fs');

const EXE = 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe';
const PROFILE = 'C:/Users/Administrator/.workbuddy/wx-profile';
const QR = 'D:/测试公众号批量生成/wechat/二维码.png';
const STATE = 'C:/Users/Administrator/.workbuddy/wx_login_state.txt';

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: EXE, headless: true,
    viewport: { width: 1280, height: 800 },
    args: ['--no-sandbox'],
  });
  const page = await ctx.newPage();
  await page.goto('https://mp.weixin.qq.com/', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(5000);
  await page.screenshot({ path: QR });
  fs.writeFileSync(STATE, 'WAITING:' + Date.now());
  console.log('QR_READY');

  const start = Date.now();
  let loggedIn = false;
  let lastShot = Date.now();
  let lastUrl = page.url();
  while (Date.now() - start < 1200000) {  // 最多20分钟
    try {
      const url = page.url();
      const txt = await page.evaluate(() => document.body ? document.body.innerText : '');
      // 记录 URL 变化，便于判断中间状态
      if (url !== lastUrl) {
        console.log('URL_CHANGED -> ' + url);
        lastUrl = url;
      }
      // 登录成功的判定：后台首页/草稿/素材 或 URL 含 home/token
      if (/首页|内容与互动|草稿箱|图文消息|接口权限|素材库|群发/.test(txt) || /cgi-bin\/home|token=/.test(url)) {
        loggedIn = true;
        break;
      }
      // 中间态：账号选择页 / 正在登录
      if (/选择.*账号|正在登录|登录中/.test(txt)) {
        console.log('INTERMEDIATE: ' + txt.slice(0, 120).replace(/\n/g, ' '));
      }
    } catch (e) {
      // 页面跳转中，忽略
    }
    // 二维码每60秒重拍，保持新鲜
    if (Date.now() - lastShot > 60000) {
      try {
        await page.screenshot({ path: QR });
        lastShot = Date.now();
        fs.writeFileSync(STATE, 'WAITING:' + Date.now());
      } catch (e) {}
    }
    await page.waitForTimeout(3000);
  }

  console.log('LOGIN_OK=' + loggedIn);
  console.log('URL=' + page.url());
  let menu = '';
  try { menu = await page.evaluate(() => document.body ? document.body.innerText : ''); } catch (e) {}
  console.log('BODY_TEXT_START:\n' + menu.slice(0, 900));
  console.log('BODY_TEXT_END');
  fs.writeFileSync(STATE, (loggedIn ? 'OK' : 'TIMEOUT') + ':' + Date.now());
  await ctx.close();
})().catch(e => { console.error('ERROR:', e.message); process.exit(1); });
