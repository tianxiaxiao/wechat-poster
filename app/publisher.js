/**
 * publisher.js — 复用 pub_one.js 已验证的工作流（接受外部 args）
 * 流程完全照搬 pub_one.js：填字段 → 选封面 → 原创声明 → 赞赏开启 → 关闭群发 → 继续发表 → 弹二维码 → 等待扫码
 *
 * 调用方式：
 *   node publisher.js --title "..." --author "..." --body body.md
 *                    --cover cover.png --qrcode qr.png
 *                    --publish-id-out pid.txt --log log.txt
 */
const { chromium } = require('playwright-core');
const fs = require('fs');

// 顶层工具函数：解析 "data:image/png;base64,xxxx" → Buffer
function decodeBase64Png(dataUrl) {
  if (!dataUrl) return null;
  const m = String(dataUrl).match(/^data:([^;]+);base64,(.+)$/s);
  if (m) return { mime: m[1], buf: Buffer.from(m[2], 'base64') };
  if (/^[A-Za-z0-9+/=\s]+$/.test(String(dataUrl).slice(0, 100))) {
    return { mime: 'image/png', buf: Buffer.from(String(dataUrl), 'base64') };
  }
  return null;
}
// 从 PNG Buffer 读 IHDR 宽高
function pngDimensions(buf) {
  try {
    if (buf.length >= 24 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) {
      return { w: buf.readUInt32BE(16), h: buf.readUInt32BE(20) };
    }
  } catch (e) {}
  return { w: 0, h: 0 };
}

// 解析命令行参数（key value）
const args = {};
for (let i = 2; i < process.argv.length; i += 2) {
  if (process.argv[i].startsWith('--')) {
    args[process.argv[i].slice(2)] = process.argv[i + 1];
  }
}
const TITLE = args.title || '';
const AUTHOR = args.author || '';
const BODY_PATH = args.body;
const COVER_PATH = args.cover;
const QR_PATH = args.qrcode;
const PID_PATH = args['publish-id-out'];
const LOG_PATH = args.log;
const NOPUB = (args.nopub || '').toUpperCase() === 'NOPUB';
const SAVE_DRAFT_ONLY = (args['save-draft-only'] || '').toUpperCase() === 'TRUE';

// 日志：同时写文件 + 控制台
const logStream = fs.createWriteStream(LOG_PATH, { flags: 'w' });
const origLog = console.log, origErr = console.error;
function pipe(name, args) {
  const s = args.map(x => typeof x === 'string' ? x : JSON.stringify(x)).join(' ');
  logStream.write(s + '\n');
  if (name === 'log') origLog(s); else origErr(s);
}
console.log = (...a) => pipe('log', a);
console.error = (...a) => pipe('err', a);

// 编辑器 URL 在打开 home 页后获取 token 再补全（token 是登录后的会话 token）
// 换机可移植：浏览器可执行文件与登录态目录都动态探测（原来写死 C:/Users/Administrator/... 换用户名即失效）
//   · 可用环境变量覆盖：CHROME_EXE=浏览器路径  WX_PROFILE_DIR=登录态目录
const os = require('os');
const path = require('path');

function pickChromeExe() {
  const cands = [];
  if (process.env.CHROME_EXE) cands.push(process.env.CHROME_EXE);
  // WorkBuddy 自带的 agent-browser（版本号会变，故扫描目录）
  try {
    const base = path.join(os.homedir(), '.agent-browser', 'browsers');
    for (const d of fs.readdirSync(base)) {
      cands.push(path.join(base, d, 'chrome.exe'));
      cands.push(path.join(base, d, 'chrome'));
    }
  } catch (e) {}
  // 系统安装的 Chrome / Edge / Linux chrome
  cands.push(
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  );
  for (const p of cands) {
    try { if (p && fs.existsSync(p)) return p; } catch (e) {}
  }
  return '';
}
const EXE = pickChromeExe();
const PROFILE = process.env.WX_PROFILE_DIR || path.join(os.homedir(), '.workbuddy', 'wx-profile');
if (!EXE) console.error('⚠️ 未找到浏览器可执行文件（Chrome/Edge）。请安装 Chrome，或设置环境变量 CHROME_EXE 指向浏览器路径。');
console.log('使用浏览器:', EXE || '(未找到)');
console.log('登录态目录:', PROFILE);

async function fetchEditorUrl(page) {
  try {
    await page.goto('https://mp.weixin.qq.com/', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await page.waitForTimeout(3000);
    // 优先从当前 URL 提取（登录后首页会跳转到 ...home?...token=xxx）
    const curUrl = page.url();
    const m = curUrl.match(/token=(\d+)/);
    const token = m ? m[1] : null;
    console.log('抓 token: 当前URL =', curUrl, '| 提取 =', token);
    if (token) return `https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&token=${token}&lang=zh_CN`;
    // 兜底：从页面链接提取
    const tokenFromPage = await page.evaluate(() => {
      const a = Array.from(document.querySelectorAll('a[href*="token="]'));
      if (a.length) { const t = a[0].href.match(/token=(\d+)/); return t ? t[1] : null; }
      return null;
    });
    if (tokenFromPage) return `https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&token=${tokenFromPage}&lang=zh_CN`;
  } catch (e) { console.log('抓 token 失败:', e.message); }
  return 'https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&lang=zh_CN';
}

(async () => {
  // 把 markdown 正文清洗成微信可用的干净文本（微信编辑器不接受富 HTML，且会剥格式；
  // 这里仅去除 md 标记残留：**加粗**、*斜体*、# 标题、> 引用、- 列表、--- 分隔等，
  // 保留段落清晰，避免正文出现星号/符号乱码）
  function cleanMdForWechat(md) {
    return md
      .replace(/\*\*(.+?)\*\*/g, '$1')   // **加粗** → 加粗
      .replace(/\*([^*]+)\*/g, '$1')     // *斜体* → 斜体
      .replace(/^#{1,6}\s*/gm, '')       // # 标题前缀
      .replace(/^>\s*/gm, '')            // > 引用前缀
      .replace(/^\s*[-*+]\s+/gm, '')     // - 列表
      .replace(/^\s*\d+[.、]\s+/gm, '')  // 数字列表
      .replace(/^\s*[-*_]{3,}\s*$/gm, '') // --- 分隔线
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '') // 图片md（无图则去）
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1'); // 链接md → 文字
  }
  const rawMd = fs.readFileSync(BODY_PATH, 'utf8');
  // 按空行分段，每段清洗后作为 <p>；段内换行(如列表连续项)用 <br> 连接，忽略空段
  const BODY_PARAS = rawMd.split(/\n\s*\n/)
    .map(p => cleanMdForWechat(p).replace(/\n/g, '').trim())
    .filter(p => p.length > 0);
  const BODY = BODY_PARAS.map(p => '<p>' + p + '</p>').join('');

  // 启动带重试：频繁用同一 profile 时偶发 Chrome 启动崩溃/锁竞争，失败则清锁重试
  async function launchChrome() {
    let lastErr = null;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        // 清掉上次可能残留的 profile 锁（避免同 profile 竞态导致启动即崩）
        try {
          for (const f of ['SingletonLock', 'SingletonCookie', 'SingletonSocket', 'lockfile', 'DevToolsActivePort']) {
            const p = require('path').join(PROFILE, f);
            if (fs.existsSync(p)) fs.unlinkSync(p);
          }
        } catch (e) {}
        const ctx = await chromium.launchPersistentContext(PROFILE, {
          executablePath: EXE, headless: true,
          viewport: { width: 1440, height: 1100 }, args: ['--no-sandbox'],
        });
        return ctx;
      } catch (e) {
        lastErr = e;
        console.log('Chrome 启动失败 attempt=' + (attempt + 1) + ':' + String(e.message || e).slice(0, 90) + '，清锁后重试…');
        await new Promise(r => setTimeout(r, 3000));
      }
    }
    throw lastErr;
  }
  const ctx = await launchChrome();
  const page = await ctx.newPage();
  page.setDefaultTimeout(60000);

  const editUrl = await fetchEditorUrl(page);
  await page.goto(editUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  for (let i = 0; i < 20; i++) { await page.waitForTimeout(1500); if (await page.evaluate(() => /请在这里输入标题/.test(document.body.innerText))) break; }
  await page.waitForTimeout(1500);

  // ===== 1. 标题 / 作者 / 正文（与 pub_one.js 完全一致） =====
  // 微信正文是 ProseMirror 富文本，直接 pm.innerHTML 赋值不会被 PM 接受（会丢正文）。
  // 正确做法：先真实点击聚焦正文编辑器，再用 page.keyboard 输入（PM 只认 input/beforeinput 事件流）。
  // 填标题 / 作者（清空后填，避免残留）
  const fillHeader = async () => {
    await page.evaluate((o) => {
      const ts = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
      const ta = document.querySelector('textarea.js_title');
      if (ta) {
        ts.call(ta, '');          // 先清空防污染
        ts.call(ta, o.TITLE);
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        ta.dispatchEvent(new Event('change', { bubbles: true }));
        ta.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
        ta.blur();
      }
      const is = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      const ai = document.querySelector('input.js_author');
      if (ai) { is.call(ai, o.AUTHOR); ai.dispatchEvent(new Event('input', { bubbles: true })); }
    }, { TITLE, AUTHOR });
  };

  // 找到正文编辑器并可靠聚焦（真实点击到 PM 内容中部），返回是否聚焦成功
  const focusBodyEditor = async () => {
    const info = await page.evaluate(() => {
      const pms = Array.from(document.querySelectorAll('div.ProseMirror')).filter(p => p && p.offsetParent !== null);
      if (!pms.length) return null;
      let target = pms.find(p => /从这里开始写正文/.test(p.textContent || '')) || null;
      if (!target) {
        target = pms.slice().sort((a, b) => {
          const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
          return (rb.width * rb.height) - (ra.width * ra.height);
        })[0];
      }
      target.scrollIntoView({ block: 'center' });
      const r = target.getBoundingClientRect();
      return { x: Math.round(r.left + r.width / 2), y: Math.round(r.top + 30) };
    }).catch(() => null);
    if (!info) return false;
    // 真实鼠标点击正文区域，让 PM 获得焦点并定位光标
    await page.mouse.click(info.x, info.y);
    await page.waitForTimeout(400);
    // 校验焦点确实在正文 PM 内
    return await page.evaluate(() => {
      const ae = document.activeElement;
      return !!(ae && (ae.classList.contains('ProseMirror') || ae.closest('.ProseMirror')));
    }).catch(() => false);
  };

  // 正文输入（前提：已聚焦正文）。采用探针验证有效的"单次 insertText"方式，
  // 分段用 \n；微信 PM 对重复/大段程序化输入偶发拦截，故失败会重试几次。
  const fillBody = async () => {
    const fullText = BODY_PARAS.join('\n\n');
    let bodyLen = 0;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (attempt > 0) {
        // 重试前重新聚焦正文（点掉可能残留的占位/已输入内容）
        await page.evaluate(() => {
          const pms = Array.from(document.querySelectorAll('div.ProseMirror')).filter(p => p.offsetParent !== null);
          const pm = pms.find(p => /从这里开始写正文/.test(p.textContent || '')) || (pms.length ? pms.sort((a, b) => { const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect(); return (rb.width * rb.height) - (ra.width * ra.height); })[0] : null);
          if (pm) { pm.innerHTML = ''; pm.focus(); }
        }).catch(()=>{});
        await page.waitForTimeout(400);
        const r = await page.evaluate(() => {
          const pm = Array.from(document.querySelectorAll('div.ProseMirror')).find(p => p.offsetParent !== null && /从这里开始写正文/.test(p.textContent || '')) || null;
          const t = pm || Array.from(document.querySelectorAll('div.ProseMirror')).sort((a, b) => { const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect(); return (rb.width * rb.height) - (ra.width * ra.height); })[0];
          const rc = t.getBoundingClientRect(); return { x: Math.round(rc.left + rc.width / 2), y: Math.round(rc.top + 30) };
        }).catch(() => ({ x: 600, y: 300 }));
        await page.mouse.click(r.x, r.y);
        await page.waitForTimeout(500);
      }
      await page.keyboard.insertText(fullText);
      await page.waitForTimeout(1200);
      bodyLen = await page.evaluate(() => {
        const pm = Array.from(document.querySelectorAll('div.ProseMirror')).find(e => e && e.offsetParent !== null && (e.innerText || '').length > 0);
        return pm ? (pm.innerText || '').length : 0;
      }).catch(() => 0);
      console.log('步骤1: 正文输入 attempt=' + (attempt + 1) + ' 校验长度=' + bodyLen);
      if (bodyLen >= 100) break;
    }
    console.log('步骤1: 正文最终长度=' + bodyLen + (bodyLen < 50 ? '（⚠️ 过短，微信可能拦截了自动化输入）' : ''));
  };

  // ===== 1. 标题 / 作者 / 正文 =====
  await fillHeader();
  await fillBody();
  // 校验标题是否真实写入，未写入则仅重填标题（正文已填，不再重复输入）
  let titleMatched = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    const titleNow = await page.evaluate(() => document.querySelector('textarea.js_title')?.value || '');
    if (titleNow === TITLE) { titleMatched = true; break; }
    console.log('步骤1#第' + (attempt + 1) + '次: 标题当前值=' + JSON.stringify(titleNow.slice(0, 30)) + '… 期望=' + JSON.stringify(TITLE) + '，重填标题…');
    await fillHeader();
    await page.waitForTimeout(1000);
  }
  if (!titleMatched) console.log('步骤1: 警告 - 标题仍未匹配（可能页面异常）');
  else console.log('步骤1: 标题/作者/正文 完成');

  // ===== 2. 封面（与 pub_one.js 完全一致） =====
  await page.evaluate(() => {
    const el = document.querySelector('a.js_imagedialog') || Array.from(document.querySelectorAll('a,span,div')).find(e => /从图片库选择/.test(e.textContent) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (el) el.click();
  });
  await page.waitForTimeout(4500);
  const thumbs = await page.locator('i.weui-desktop-img-picker__img-thumb').all();
  console.log('步骤2: 素材库缩略图数量 =', thumbs.length);
  if (thumbs.length > 0) {
    const pickIdx = Math.floor(Math.random() * thumbs.length);
    await thumbs[pickIdx].click();
    console.log('步骤2: 已选中素材库缩略图 #' + pickIdx);
    await page.waitForTimeout(1500);
    const step = await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e => e.textContent.trim() === '下一步' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      if (btns.length) { btns[btns.length - 1].click(); return 'NEXT_CLICKED'; }
      return 'NO_NEXT';
    });
    console.log('步骤2: 下一步', step);
    await page.waitForTimeout(2500);
    // 在"编辑封面"裁剪弹窗内精确找"确认"（避免点中页面其它"确认"按钮）
    const coverConfirm = await page.evaluate(() => {
      const dlg = Array.from(document.querySelectorAll('.weui-desktop-dialog, .weui-desktop-mask, .weui-desktop-mask, .weui-desktop-dialog, div')).find(e => /编辑封面|服务号列表|1:1/.test(e.textContent) && e.childElementCount > 5 && e.getBoundingClientRect().height > 300);
      const scope = dlg || document;
      const btns = Array.from(scope.querySelectorAll('a,button,span')).filter(e => /^确认$|确认使用|确认提交/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      if (btns.length) { btns[btns.length - 1].click(); return 'COVER_CONFIRMED:' + btns.length; }
      return 'NO_COVER_CONFIRM';
    });
    console.log('步骤2: 封面确认', coverConfirm);
    await page.waitForTimeout(3000);
  }

  // ===== 3. 原创声明（与 pub_one.js 完全一致） =====
  await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.appmsg-editor__setting-group')).find(e => /原创/.test(e.textContent) && e.childElementCount < 10);
    if (el) el.click();
  });
  await page.waitForTimeout(1500);
  const original = await page.evaluate((o) => {
    const dlg = Array.from(document.querySelectorAll('.weui-desktop-dialog, .weui-desktop-mask, div')).find(e => /声明类型|快捷转载/.test(e.textContent) && /我已阅读并同意/.test(e.textContent) && e.getBoundingClientRect().height > 200);
    const scope = dlg || document;
    const authorInput = Array.from(scope.querySelectorAll('input[type=text],input:not([type])')).find(e => /请输入作者|作者/.test(e.placeholder || '') || /js_author/.test(e.className));
    let authorFilled = false;
    if (authorInput) {
      const is = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      is.call(authorInput, o.AUTHOR);
      authorInput.dispatchEvent(new Event('input', { bubbles: true }));
      authorFilled = true;
    }
    let quickOff = false;
    const qsw = Array.from(scope.querySelectorAll('.weui-desktop-switch')).find(s => /已开启|快捷转载|所有账号均可转载/.test((s.parentElement && s.parentElement.textContent) || ''));
    if (qsw) {
      const qcb = qsw.querySelector('input[type=checkbox]');
      if (qcb && qcb.checked) { qcb.click(); quickOff = true; }
      else if (!qcb) { qsw.click(); quickOff = true; }
    }
    let agreeCb = null;
    const agreeLabel = Array.from(scope.querySelectorAll('label')).find(l => /我已阅读并同意/.test(l.textContent));
    if (agreeLabel) agreeCb = agreeLabel.querySelector('input[type=checkbox]');
    if (agreeCb && !agreeCb.checked) agreeCb.click();
    else if (!agreeCb) {
      const cb = Array.from(scope.querySelectorAll('input[type=checkbox]')).find(c => /我已阅读并同意/.test((c.parentElement && c.parentElement.textContent) || ''));
      if (cb && !cb.checked) cb.click();
    }
    return new Promise((resolve) => {
      setTimeout(() => {
        const btns = Array.from(scope.querySelectorAll('a,button,span')).filter(e => e.textContent.trim() === '确定' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
        if (btns.length) btns[btns.length - 1].click();
        resolve({ authorFilled, quickOff, agreeCb: !!agreeCb, confirmClicked: btns.length > 0 });
      }, 500);
    });
  }, { AUTHOR });
  console.log('步骤3: 原创弹窗处理', JSON.stringify(original));
  await page.waitForTimeout(1500);

  // ===== 4. 赞赏（与 pub_one.js 完全一致） =====
  const rewardOpen = await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.appmsg-editor__setting-group')).find(e => /赞赏/.test(e.textContent) && e.childElementCount < 10);
    if (el) el.click();
    return !!el;
  });
  await page.waitForTimeout(1500);
  let rewardState = 'NOT_AVAILABLE';
  if (rewardOpen) {
    const switched = await page.evaluate(() => {
      const switchWrap = document.querySelector('.setting-group__switch.js_reward_open');
      const cb = switchWrap && switchWrap.querySelector('input[type=checkbox]');
      if (cb) { if (!cb.checked) { cb.click(); return true; } return 'ALREADY_ON'; }
      else if (switchWrap) { switchWrap.click(); return true; }
      return false;
    });
    await page.waitForTimeout(2000);
    const rewardConfirm = await page.evaluate(() => {
      const d = Array.from(document.querySelectorAll('.weui-desktop-dialog')).find(e => /赞赏类型|赞赏作者|赞赏账户/.test(e.innerText) && e.getBoundingClientRect().height > 100);
      if (!d) return { dlgFound: false };
      const scope = d;
      const authorRadio = scope.querySelector('input[type=radio][value="1"], weui-desktop-form__radio[value="1"]') || Array.from(scope.querySelectorAll('input[type=radio]')).find(e => e.value === '1');
      if (authorRadio) authorRadio.click();
      const agreeLabel = Array.from(scope.querySelectorAll('label')).find(l => /我已阅读并同意/.test(l.textContent));
      const agreeCb = agreeLabel ? agreeLabel.querySelector('input[type=checkbox]') : null;
      if (agreeCb && !agreeCb.checked) agreeCb.click();
      else if (!agreeCb) {
        const cb = Array.from(scope.querySelectorAll('input[type=checkbox]')).find(c => /我已阅读并同意/.test((c.parentElement && c.parentElement.textContent) || ''));
        if (cb && !cb.checked) cb.click();
      }
      return { dlgFound: true, authorRadio: !!authorRadio, agreeCbFound: !!agreeCb };
    });
    await page.waitForTimeout(800);
    const confirmBtn = await page.evaluate(() => {
      const d = Array.from(document.querySelectorAll('.weui-desktop-dialog')).find(e => /赞赏类型|赞赏作者|赞赏账户/.test(e.innerText) && e.getBoundingClientRect().height > 100);
      const scope = d || document;
      const btns = Array.from(scope.querySelectorAll('a,button,span')).filter(e => e.textContent.trim() === '确定' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      if (btns.length) { btns[btns.length - 1].click(); return 'REWARD_CONFIRMED'; }
      return 'NO_REWARD_CONFIRM';
    });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: QR_PATH.replace('.png','_reward.png') });
    const finalTip = await page.evaluate(() => (document.querySelector('.js_reward_setting_tips') || {}).textContent || '');
    rewardState = { switched, rewardConfirm, confirmBtn, finalTip };
  }
  console.log('步骤4: 赞赏状态', JSON.stringify(rewardState));
  await page.waitForTimeout(1500);

  if (NOPUB) {
    console.log('NOPUB: 仅验证，跳过发表');
    fs.writeFileSync(PID_PATH, '');
    await ctx.close();
    logStream.end();
    return;
  }

  if (SAVE_DRAFT_ONLY) {
    // 点"保存为草稿"按钮（带原创+赞赏），不点发表、不弹码
    console.log('SAVE_DRAFT_ONLY: 走保存草稿流程（不发表、不扫码）');
    const saved = await page.evaluate(() => {
      const btn = Array.from(document.querySelectorAll('a,button,span'))
        .find(e => /保存为草稿|保存草稿/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0 && !e.disabled);
      if (btn) { btn.click(); return 'CLICKED:' + btn.tagName; }
      return 'NO_BTN';
    });
    console.log('SAVE_DRAFT_ONLY: 点保存按钮结果', saved);
    await page.waitForTimeout(3000);
    // 检测是否保存成功（弹"已保存"提示或URL含draft/草稿标记）
    const tip = await page.evaluate(() => {
      const t = document.body.innerText;
      return {
        savedHint: /已保存|保存成功|草稿已保存/.test(t),
        hasDraftUrl: /draft/.test(location.href),
      };
    }).catch(() => ({}));
    console.log('SAVE_DRAFT_ONLY: 状态', JSON.stringify(tip));
    // dump 保存后原创/赞赏两行状态（判定原创是否真声明成功）
    try {
      const st = await page.evaluate(() => {
        const groups = Array.from(document.querySelectorAll('.appmsg-editor__setting-group, .setting-group'));
        const pick = (re) => {
          const g = groups.find(e => re.test(e.textContent || '') && e.getBoundingClientRect().height > 0);
          return g ? (g.textContent || '').replace(/\s+/g, ' ').slice(0, 90) : '(not found)';
        };
        return { original: pick(/^原创|原创声明|声明原创/), reward: pick(/赞赏/) };
      });
      console.log('SAVE_DRAFT_ONLY: 原创/赞赏行状态 -> ' + JSON.stringify(st));
      await page.screenshot({ path: QR_PATH.replace('.png', '_draft_state.png') });
    } catch (e) { console.log('SAVE_DRAFT_ONLY: dump状态失败', e.message.slice(0, 80)); }
    // 写一个标识，Python 端会再调 wechat_api.find_draft_media_id_by_title
    fs.writeFileSync(PID_PATH, 'SAVED');
    await ctx.close();
    logStream.end();
    return;
  }

  // ===== 5. 发表（与 pub_one.js 完全一致） =====
  const pubClick = await page.evaluate(() => {
    const btn = document.querySelector('button.mass_send') || Array.from(document.querySelectorAll('a,button,span')).find(e => e.textContent.trim() === '发表' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (btn) { btn.click(); return 'PUB_CLICKED'; }
    return 'NO_PUB_BTN';
  });
  console.log('步骤5: 发表按钮', pubClick);
  await page.waitForTimeout(3000);
  const confirmPub = await page.evaluate(() => {
    const hasPublishDialog = /群发通知|分组通知|定时发表/.test(document.body.innerText);
    if (hasPublishDialog) {
      const rows = Array.from(document.querySelectorAll('div,li,label,section')).filter(e => /群发通知/.test(e.textContent) && e.querySelector('input[type=checkbox], [class*=switch]') && e.getBoundingClientRect().height > 0 && e.getBoundingClientRect().height < 160);
      let closed = false;
      for (const row of rows) {
        const cb = row.querySelector('input[type=checkbox]');
        if (cb && cb.checked) { cb.click(); closed = true; break; }
      }
      if (!closed) {
        for (const row of rows) {
          const sw = row.querySelector('label.weui-desktop-switch, [class*=switch]');
          if (sw) { sw.click(); closed = true; break; }
        }
      }
      if (closed) return 'GROUP_OFF:' + rows.length;
      return 'GROUP_SWITCH_NOT_FOUND rows=' + rows.length;
    }
    return 'NO_PUBLISH_DIALOG';
  });
  console.log('步骤5b: 发表弹窗处理', confirmPub);
  await page.waitForTimeout(2000);
  const publishBtn = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e => /^发表$/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (btns.length) { btns[btns.length - 1].click(); return 'PUBLISH_CONFIRMED'; }
    return 'NO_PUBLISH_BTN';
  });
  console.log('步骤5c: 点发表确认', publishBtn);
  await page.waitForTimeout(3500);
  const continueBtn = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e => /^继续发表$/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (btns.length) { btns[btns.length - 1].click(); return 'CONTINUE_CLICKED'; }
    return 'NO_CONTINUE_BTN';
  });
  console.log('步骤5d: 继续发表', continueBtn);

  // ===== 6. 等待二维码弹窗出现，并设法定位/保存二维码图 =====
  console.log('步骤6: 等待微信验证弹窗...');
  let qrReady = false;
  let qrProbe = '';
  const tqr0 = Date.now();
  while (Date.now() - tqr0 < 30000) {
    const probe = await page.evaluate(() => {
      const txt = document.body.innerText;
      // 弹窗里若有 canvas，尺寸 > 60 的即为二维码
      const canvases = Array.from(document.querySelectorAll('canvas')).filter(c => c.width >= 60 && c.height >= 60);
      // 若有 img 且 src 是 data/base64 或很大，可能是二维码
      const bigImg = Array.from(document.querySelectorAll('img')).filter(i => (i.src || '').startsWith('data:') && i.naturalWidth > 0);
      return {
        dlgFound: /微信验证|扫码|扫一扫|请使用微信扫描/.test(txt),
        canvasCount: canvases.length,
        canvasSize: canvases.length ? (canvases[0].width + 'x' + canvases[0].height) : '0',
        dataImg: bigImg.length,
        anyImg: Array.from(document.querySelectorAll('img')).length,
        anyIframe: Array.from(document.querySelectorAll('iframe')).length,
      };
    });
    qrProbe = JSON.stringify(probe);
    if (probe.dlgFound && (probe.canvasCount > 0 || probe.dataImg > 0 || probe.anyIframe > 0)) { qrReady = true; break; }
    await page.waitForTimeout(1000);
  }
  console.log('步骤6: 微信验证弹窗检测', qrReady, qrProbe);

  // 二维码在 iframe 里（微信验证码常跨域 iframe）。这里只做探测日志，
  // 真正的"干净二维码提取"由步骤7 的 base64 解码完成（读 data:image，无需跨 frame 截图坐标）。
  // 二维码异步渲染：步骤6 等到 dlgFound 即开始轮询提取，最长 30s，找到正方形 + 边长>=120 即采用。
  let qr = null;
  let bufFallback = null;   // svg/bg 等非 http 候选，作为超时兜底
  const tQr = Date.now();
  const QR_TIMEOUT = 40000; // 给足 safeqrcode 渲染时间
  while (Date.now() - tQr < QR_TIMEOUT) {
    qr = await findAndExtractQR();
    if (!qr) { console.log('步骤7: 轮询 round 未找到候选'); await page.waitForTimeout(1500); continue; }
    // 只有真正的 safeqrcode(http) 才立即采用；其余(客服码/svg/bg)均只作兜底，继续等真二维码
    if (qr.pendingHttp && /safeqrcode/i.test(qr.pendingHttp)) {
      console.log('步骤7: 找到真二维码(safeqrcode) url=' + qr.pendingHttp.slice(0, 80) + ' score=' + qr.score);
      break;
    }
    // svg/bg/非safeqrcode-http 候选：记作兜底(优先分高的)，继续轮询等 safeqrcode
    let fbScore = qr.buf ? (qr.score || 0) : (qr.score || 0);
    if (fbScore >= 100 && (!bufFallback || fbScore > bufFallback.score)) {
      bufFallback = qr;
      console.log('步骤7: 记录兜底 kind=' + qr.kind + ' score=' + (qr.score||'-') + '，继续等待真二维码…');
    }
    await page.waitForTimeout(1500);
  }
  // 循环结束：拿到 safeqrcode 用之；否则用兜底(可能是图标/客服码，尽力而为)
  if (!(qr && qr.pendingHttp && /safeqrcode/i.test(qr.pendingHttp))) {
    if (bufFallback) { qr = bufFallback; console.log('步骤7: ' + QR_TIMEOUT/1000 + 's 内未见 safeqrcode，采用兜底 kind=' + bufFallback.kind + ' score=' + (bufFallback.score||'-')); }
    else { qr = null; }
  }
  // http 候选兜底 fetch
  if (qr && qr.pendingHttp && !qr.buf) {
    try {
      console.log('步骤7: http fetch ' + qr.pendingHttp);
      const resp = await page.request.get(qr.pendingHttp);
      if (resp.ok()) {
        const buf = await resp.body();
        const ct = (resp.headers() && (resp.headers()['content-type'] || resp.headers()['Content-Type'])) || 'image/png';
        qr.buf = buf; qr.mime = ct; qr.bytes = buf.length; qr.kind = 'http'; qr.score = 260;
        console.log('步骤7: http fetch 成功 bytes=' + qr.buf.length + ' mime=' + ct);
      } else { console.log('步骤7: http fetch 状态码=' + resp.status()); }
    } catch (e) { console.log('步骤7: http fetch 失败', e.message.slice(0, 80)); }
  }

  // ===== 7. 扫码等待（与 pub_one.js 完全一致） =====
  const scanState = await page.evaluate(() => {
    const txt = document.body.innerText;
    return {
      hasQR: /扫码|扫一扫|二维码|请使用微信扫描/.test(txt),
      hasSuccess: /发表成功|已群发|发送成功|发布成功/.test(txt),
      hasFail: /图片不能为空|请设置封面|校验失败|操作失败/.test(txt),
      hasGroupConfirm: /群发|发送给全部/.test(txt),
      url: location.href
    };
  });
  console.log('步骤6: 扫码检测', JSON.stringify(scanState));

  // ============================================================
  // 主二维码图截取（V3 - base64 解码方案）
  // 微信验证二维码藏在 cross-origin iframe 里，getBoundingClientRect 在主页面坐标系拿不到
  // （返回全 0），page.screenshot({clip}) 也无法跨 frame 截图。
  // 最可靠办法：直接读取二维码元素的 data:image base64（img.src 或 canvas.toDataURL），
  // 在 Node 端用 Buffer 解码写盘 → 得到的就是干净的二维码原始像素，无压缩、无变形。
  // ============================================================
  let mainQRShot = false;

  // 遍历主页面 + 所有 iframe，找到真正的微信验证二维码并读其 base64。
  // 候选来源三种：img[src^=data:image]、canvas.toDataURL()、任意元素 background-image: url(data:image...)。
  // 选最像二维码的：解码后 PNG 用 header 读宽高，宽高比 ~1:1 + 边长 ≥ 120 的优先。
  // cross-origin iframe 里 naturalWidth/bbox 常为 0 或 0,0,不能依赖。
  async function findAndExtractQR() {
    const frames = page.frames();
    const httpUrls = []; // [{url, kind, frameId, scoreHint}]
    let best = null;
    let allInfo = [];
    for (const fr of frames) {
      let candidates = [];
      let httpCandidates = [];
      try {
        const result = await fr.evaluate(() => {
          const out = [];
          const httpOut = [];
          const seen = new Set();
          // 1) img[src^="data:image"]
          for (const im of document.querySelectorAll('img')) {
            const s = (im.src || '');
            if (s.startsWith('data:image')) {
              out.push({ kind: 'img', dataUrl: s, w: im.naturalWidth || 0, h: im.naturalHeight || 0 });
            } else if (s && /^https?:/.test(s)) {
              const r = im.getBoundingClientRect();
              // 真正的微信验证二维码 URL 形如 .../safe/safeqrcode?ticket=... ，
              // 只有它才是要扫的安全码。普通 "xxxqrcode.jpg"(如客服码) 不能当作强候选。
              const isQRUrl = /\/safe\/safeqrcode|safeqrcode\?/i.test(s) || /safeqrcode/i.test(s);
              if (isQRUrl) {
                httpOut.push({ kind: 'img', url: s, w: Math.round(r.width) || 280, h: Math.round(r.height) || 280, strong: true });
              } else {
                // 普通 http 图：过滤小图标
                if (r.width >= 100 && r.width <= 600 && r.height >= 100 && r.height <= 600) {
                  const ratio = Math.min(r.width, r.height) / Math.max(r.width, r.height);
                  if (ratio >= 0.7) httpOut.push({ kind: 'img', url: s, w: Math.round(r.width), h: Math.round(r.height) });
                }
              }
            }
          }
          // 2) background-image: url("data:...") 或 url("https://...")
          for (const el of document.querySelectorAll('*')) {
            const bg = getComputedStyle(el).backgroundImage;
            if (!bg || bg === 'none') continue;
            const mData = bg.match(/url\(["']?(data:image\/[^;]+;base64,[A-Za-z0-9+/=]+)["']?\)/);
            if (mData && !seen.has(mData[1])) {
              seen.add(mData[1]);
              const r = el.getBoundingClientRect();
              out.push({ kind: 'bg', dataUrl: mData[1], w: Math.round(r.width) || 0, h: Math.round(r.height) || 0 });
              continue;
            }
            const mHttp = bg.match(/url\(["']?(https?:\/\/[^"')]+)["']?\)/);
            if (mHttp) {
              const r = el.getBoundingClientRect();
              if (r.width >= 100 && r.width <= 600 && r.height >= 100 && r.height <= 600) {
                httpOut.push({ kind: 'bg', url: mHttp[1], w: Math.round(r.width), h: Math.round(r.height) });
              }
            }
          }
          // 3) canvas
          for (const c of document.querySelectorAll('canvas')) {
            const w = c.width || 0, h = c.height || 0;
            if (w >= 80 && h >= 80) {
              try { out.push({ kind: 'canvas', dataUrl: c.toDataURL('image/png'), w, h }); } catch (e) {}
            }
          }
          return { data: out, http: httpOut };
        }).catch(() => ({ data: [], http: [] }));
        candidates = result.data || [];
        httpCandidates = result.http || [];
      } catch (e) { candidates = []; httpCandidates = []; }

      // 处理 dataURL 候选（之前的评分逻辑）
      for (const cand of candidates) {
        if (!cand || !cand.dataUrl) continue;
        const dec = decodeBase64Png(cand.dataUrl);
        if (!dec || !dec.buf || dec.buf.length < 200) continue;
        const dims = pngDimensions(dec.buf);
        const dw = dims.w, dh = dims.h;
        allInfo.push(`(${cand.kind} ${dec.mime||'?'} ${dw}x${dh} ${dec.buf.length}B)`);
        const isImg = /^image\//.test(dec.mime || '');
        if (!isImg) continue;
        let score = 0;
        const isSvg = /svg/i.test(dec.mime || '');
        if (isSvg) {
          if (dec.buf.length >= 1000 && dec.buf.length <= 30000) score += 200;
          if (dec.buf.length >= 3000 && dec.buf.length <= 15000) score += 80;
        } else if (dw > 0 && dh > 0) {
          const ratio = Math.min(dw, dh) / Math.max(dw, dh);
          if (ratio >= 0.9 && dw >= 120 && dh >= 120) score += 200;
          else if (ratio >= 0.7 && dw >= 60 && dh >= 60) score += 50;
          if (dec.buf.length >= 800 && dec.buf.length <= 20000) score += 30;
        }
        if (dec.buf.length > 60000) score = Math.max(0, score - 100);
        if (score >= 100 && (!best || score > best.score)) {
          best = { buf: dec.buf, mime: dec.mime, bytes: dec.buf.length, kind: cand.kind, w: dw, h: dh, score };
        }
      }

      // 处理 http URL 候选（评分：标准 280x280 + URL 含 safeqrcode 高分）
      for (const cand of httpCandidates) {
        if (!cand || !cand.url) continue;
        let score = 0;
        // 只有 /safe/safeqrcode 才是真正的微信验证二维码，给决定性高分
        const isQR = /safeqrcode/i.test(cand.url);
        if (isQR) {
          score = 1000 + (cand.strong ? 500 : 0);
        } else if (cand.w >= 200 && cand.w <= 350 && Math.abs(cand.w - cand.h) < 20) {
          score = 250;
        } else if (cand.w >= 100 && cand.w <= 600 && Math.abs(cand.w - cand.h) < 50) {
          score = 80;
        }
        if (score >= 100 && (!best || score > best.score)) {
          // 把 http 候选放进 best（buf 待 fetch 拉取），避免被低分 svg 抢占
          best = { pendingHttp: cand.url, mime: 'image/png', bytes: 0, kind: 'http-pending', w: cand.w, h: cand.h, score };
        }
      }
    }
    if (allInfo.length) console.log('步骤7: data候选', allInfo.slice(0, 12).join(' '));
    if (httpUrls.length) console.log('步骤7: http候选', httpUrls.map(u => `(${u.kind} ${u.w}x${u.h} ${u.url.slice(0,60)})`).join(' '));
    return best;
  }

  // pngDimensions 已定义在脚本顶部

  // ============================================================
  // 二维码在 iframe 里（异步渲染）。步骤7 轮询提取（最多 30s），找到合适的即采用
  // ============================================================

  if (qr && qr.buf) {
    const out = QR_PATH.endsWith('.png') ? QR_PATH : QR_PATH + '.png';
    fs.writeFileSync(out, qr.buf);
    mainQRShot = true;
    console.log('步骤7: 二维码提取成功，kind=' + qr.kind + ' size=' + qr.w + 'x' + qr.h + ' bytes=' + qr.buf.length + ' score=' + (qr.score||'-') + ' -> ' + out);
  } else {
    console.log('步骤7: 未提取到二维码，走全屏截图兜底');
    await page.screenshot({ path: QR_PATH });
    console.log('步骤7: 兜底截全屏到 QR_PATH');
  }

  let scannedOk = false;
  let savedDraftOnly = false;   // 扫码确认后微信仅"保存为草稿"、未真正发表
  if (scanState.hasQR) {
    console.log('WAIT_SCAN: 请用户扫码');
    let success = false;
    const t0 = Date.now();
    // 关键：微信扫码确认后会跳到 home 首页（/cgi-bin/home），首页文案不含 success 字眼
    // 强信号 = 二维码消失 + URL 跳离扫码/编辑器页
    let qrGoneSince = 0;  // 第一次发现 hasQR=false 的时间
    while (Date.now() - t0 < 200000) {
      await page.waitForTimeout(4000);
      let s;
      try {
        s = await page.evaluate(() => {
          const txt = document.body.innerText || '';
          return {
            hasSuccess: /发表成功|已群发|发送成功|发布成功|已发送/.test(txt),
            hasQR: /扫码|扫一扫|二维码|请使用微信扫描/.test(txt),
            url: location.href,
            txt: txt.slice(0, 3000),
          };
        });
      } catch (e) {
        console.log('扫码等待 evaluate 失败(可能页面跳转):', e.message.slice(0, 80));
        try { await page.waitForLoadState('domcontentloaded', { timeout: 8000 }); } catch (_) {}
        continue;
      }
      console.log('扫码巡检:', JSON.stringify(s));
      // 强信号判定：URL 已离开扫码/编辑器页才可信。
      // ⚠️ 关键：微信在 appmsg_edit（编辑器页）内也会出现"发表成功/已发送"等文案
      //（例如扫码确认后仅"保存为草稿"成功），若在编辑器页内直接判成功，会把
      // "只存了草稿、并未真正发布"的文章误报为 PUBLISH_OK（历史踩坑：用户主页看不见）。
      const inScanishPage = /scan|verify|qrcode|appmsg_edit|appmsg\?t=media/i.test(s.url);
      if (s.hasSuccess && !inScanishPage) { success = true; break; }
      // 识别"仅保存为草稿未发表"：扫码确认后 15s 仍停留在编辑器页(appmsg_edit)、
      // 页面无"发表成功"类文案，且二维码已消失或出现"保存成功/已保存"类草稿文案
      // → 明确判定为草稿状态（不误报成功、也不干等到超时）。
      if (Date.now() - t0 > 15000 && inScanishPage && !s.hasSuccess
          && (s.hasQR === false || /保存成功|已保存|保存为草稿|存为草稿|已存为草稿/.test(s.txt || ''))) {
        console.log('DRAFT_ONLY_DETECTED: 扫码确认后仅保存为草稿，未真正发表（URL=' + s.url + '）');
        savedDraftOnly = true;
        break;
      }
      if (!s.hasQR) {
        if (qrGoneSince === 0) qrGoneSince = Date.now();
        // 二维码消失超过 6 秒且不再回扫码/编辑器页 → 视为发表成功（微信跳到 home 是常态）
        const elapsed = Date.now() - qrGoneSince;
        const onHomeLike = /home|appmsg\?.*list|category|main/i.test(s.url);
        if (elapsed > 6000 && onHomeLike) {
          console.log('SCAN_SUCCESS by URL 跳转（' + s.url + ', 二维码消失 ' + Math.round(elapsed/1000) + 's）');
          success = true;
          break;
        }
        // 兜底：二维码消失超过 15 秒且不在扫码页 → 也算成功（避免极端场景死等）
        if (elapsed > 15000 && !inScanishPage) {
          console.log('SCAN_SUCCESS by 兜底（QR 消失 ' + Math.round(elapsed/1000) + 's, URL=' + s.url + '）');
          success = true;
          break;
        }
      } else {
        qrGoneSince = 0;  // 二维码又回来了（页面被刷），重置计时
      }
    }
    console.log(success ? 'SCAN_SUCCESS' : 'SCAN_TIMEOUT');
    scannedOk = success;
  } else if (scanState.hasSuccess && !/scan|verify|qrcode|appmsg_edit|appmsg\?t=media/i.test(scanState.url || '')) {
    scannedOk = true;
    console.log('SCAN_INSTANT_SUCCESS');
  } else if (scanState.hasFail) {
    console.log('FAIL: ' + JSON.stringify(scanState));
  } else {
    console.log('未知状态: ' + JSON.stringify(scanState));
  }

  // 无论扫码结果如何，扫完码后再次截图 + 写入 pid（成功时尝试用 wechat API 兜底拿 article_id）
  await page.screenshot({ path: QR_PATH.replace('.png','_results.png') });

  // SCAN_SUCCESS 后立刻给浏览器窗口加一个明显的反馈（用户切回桌面时立刻能看到）
  if (scannedOk) {
    try {
      await page.evaluate((title) => {
        // 1. 改 tab 标题 + 浏览器窗口标题（任务栏可见）
        document.title = '✅ 已发布：' + title;
        // 2. 在页面上加一个超大的绿色横幅（headless 下也写入 DOM，截图 _results.png 可看到）
        const banner = document.createElement('div');
        banner.id = '__publish_ok_banner__';
        banner.style.cssText = 'position:fixed;top:0;left:0;right:0;background:#16a34a;color:#fff;text-align:center;padding:18px;font:bold 22px sans-serif;z-index:2147483647;box-shadow:0 4px 16px rgba(0,0,0,0.3);';
        banner.innerHTML = '✅ 已发布：' + title + ' <span style="font-weight:400;font-size:14px;opacity:0.85;margin-left:12px;">—— 8 秒扫码识别成功，下次你扫完码切回窗口就能看到这个反馈</span>';
        document.body.appendChild(banner);
        // 3. 响一声提示音
        try {
          const AC = window.AudioContext || window.webkitAudioContext;
          if (AC) {
            const ctx = new AC();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.frequency.value = 880; osc.type = 'sine';
            gain.gain.value = 0.3;
            osc.connect(gain); gain.connect(ctx.destination);
            osc.start(); setTimeout(() => { osc.stop(); ctx.close(); }, 250);
          }
        } catch (_) {}
      }, TITLE);
      await page.waitForTimeout(500);
      await page.screenshot({ path: QR_PATH.replace('.png', '_done.png') });
      console.log('已发布横幅 + 标题 + 提示音 已写入');
    } catch (e) { console.log('写入反馈失败（不影响发布结果）:', e.message.slice(0, 100)); }
    // 通过 freepublish/batchget 兜底获取最新发表的文章 ID（可选）
    try {
      const axiosScript = `process.title = 'pid-probe'; ...`; // 此处省略 XMLHttpRequest 复杂调用，留空
    } catch (e) {}
    fs.writeFileSync(PID_PATH, 'PUBLISH_OK'); // 显式标记成功，让 Python 侧不去检查具体 id
  } else if (savedDraftOnly) {
    fs.writeFileSync(PID_PATH, 'SAVED_DRAFT'); // 扫码确认后仅存草稿（未真正发表）
  } else {
    fs.writeFileSync(PID_PATH, '');
  }

  await ctx.close();
  logStream.end();
})().catch(e => { console.error('ERROR:', e.stack || e.message); try { fs.writeFileSync(PID_PATH, ''); } catch(_) {}; logStream.end(); process.exit(1); });
