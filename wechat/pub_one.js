// 参数化发表脚本：填字段 + 上传封面 + 原创 + 赞赏 + 发表(等扫码)
// 用法: node pub_one.js <articleIndex> <coverPath>
// articleIndex: 0/1/2 对应 config.json articles
const { chromium } = require('playwright-core');
const EXE = 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe';
const PROFILE = 'C:/Users/Administrator/.workbuddy/wx-profile';
const cfg = require('./config.json');

const idx = parseInt(process.argv[2] || '0', 10);
const NOPUB = (process.argv[3] || '').toUpperCase() === 'NOPUB';
const coverPath = process.argv[4] || ('D:/测试公众号批量生成/wechat/covers/cover_' + (idx + 1) + '.png');
const art = cfg.articles[idx];

const fs = require('fs');
const BODY = fs.readFileSync(art.content_md, 'utf8').split('\n\n').map(p => '<p>' + p.trim() + '</p>').join('');
const TITLE = art.title, AUTHOR = art.author;

(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: EXE, headless: true,
    viewport: { width: 1440, height: 1100 }, args: ['--no-sandbox'],
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(60000);

  // 打开新建文章编辑器
  const url = 'https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&token=1916614841&lang=zh_CN';
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  for (let i = 0; i < 20; i++) { await page.waitForTimeout(1500); if (await page.evaluate(() => /请在这里输入标题/.test(document.body.innerText))) break; }
  await page.waitForTimeout(1500);

  // ===== 1. 标题 / 作者 / 正文 =====
  await page.evaluate((o) => {
    const ts = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
    const ta = document.querySelector('textarea.js_title');
    ts.call(ta, o.TITLE);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.dispatchEvent(new Event('change', { bubbles: true }));
    ta.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true }));
    ta.blur();
    const is = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    const ai = document.querySelector('input.js_author'); is.call(ai, o.AUTHOR); ai.dispatchEvent(new Event('input', { bubbles: true }));
    const pm = Array.from(document.querySelectorAll('div.ProseMirror')).find(e => /从这里开始写正文/.test(e.textContent)) || document.querySelector('div.ProseMirror');
    if (pm) { pm.innerHTML = o.BODY; pm.dispatchEvent(new Event('input', { bubbles: true })); }
  }, { TITLE, AUTHOR, BODY });
  await page.waitForTimeout(2000);
  // 验证标题是否写入
  const titleNow = await page.evaluate(() => document.querySelector('textarea.js_title')?.value || document.querySelector('.js_title')?.value || '');
  console.log('步骤1: 标题当前值 =', JSON.stringify(titleNow), '| 期望 =', JSON.stringify(TITLE));
  console.log('步骤1: 标题/作者/正文 完成');

  // ===== 2. 封面：从素材库随机选一张图 =====
  await page.evaluate(() => {
    const el = document.querySelector('a.js_imagedialog') || Array.from(document.querySelectorAll('a,span,div')).find(e => /从图片库选择/.test(e.textContent) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (el) el.click();
  });
  await page.waitForTimeout(4500);
  // 选一张缩略图（素材库20张，随机挑一个）
  const thumbs = await page.$$('i.weui-desktop-img-picker__img-thumb');
  console.log('步骤2: 素材库缩略图数量 =', thumbs.length);
  if (thumbs.length > 0) {
    const pickIdx = Math.floor(Math.random() * thumbs.length);
    await thumbs[pickIdx].click();
    console.log('步骤2: 已选中素材库缩略图 #' + pickIdx);
    await page.waitForTimeout(1500);
    // 点"下一步"确认
    const step = await page.evaluate(() => {
      const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e => e.textContent.trim() === '下一步' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      if (btns.length) { btns[btns.length - 1].click(); return 'NEXT_CLICKED'; }
      return 'NO_NEXT';
    });
    console.log('步骤2: 下一步', step);
    // 进入"编辑封面"裁剪预览，点"确认"完成封面提交
    await page.waitForTimeout(2500);
    const coverConfirm = await page.evaluate(() => {
      // 在"编辑封面"弹窗内找"确认"
      const dlg = Array.from(document.querySelectorAll('.weui-desktop-dialog, .weui-desktop-mask, div')).find(e => /编辑封面|服务号列表/.test(e.textContent) && e.childElementCount > 5 && e.getBoundingClientRect().height > 300);
      const scope = dlg || document;
      const btns = Array.from(scope.querySelectorAll('a,button,span')).filter(e => /^确认$|确认使用|确认提交/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      if (btns.length) { btns[btns.length - 1].click(); return 'COVER_CONFIRMED:' + btns.length; }
      return 'NO_COVER_CONFIRM';
    });
    console.log('步骤2: 封面确认', coverConfirm);
    await page.waitForTimeout(3000);
    await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_cover_done.png' });
  } else {
    console.log('步骤2: 素材库无缩略图，改用本地上传');
  }
  await page.waitForTimeout(3500);
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_cover_upload.png' });

  // ===== 3. 原创声明 =====
  await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.appmsg-editor__setting-group')).find(e => /原创/.test(e.textContent) && e.childElementCount < 10);
    if (el) el.click();
  });
  await page.waitForTimeout(1500);
  // 在"原创"弹窗内：填入作者 + 关闭快捷转载 + 勾协议 + 确定
  const original = await page.evaluate((o) => {
    const dlg = Array.from(document.querySelectorAll('.weui-desktop-dialog, .weui-desktop-mask, div')).find(e => /声明类型|快捷转载/.test(e.textContent) && /我已阅读并同意/.test(e.textContent) && e.getBoundingClientRect().height > 200);
    const scope = dlg || document;
    // 填入原创弹窗里的"作者"输入框
    const authorInput = Array.from(scope.querySelectorAll('input[type=text],input:not([type])')).find(e => /请输入作者|作者/.test(e.placeholder || '') || /js_author/.test(e.className));
    let authorFilled = false;
    if (authorInput) {
      const is = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      is.call(authorInput, o.AUTHOR);
      authorInput.dispatchEvent(new Event('input', { bubbles: true }));
      authorFilled = true;
    }
    // 关闭"快捷转载"开关
    let quickOff = false;
    const qsw = Array.from(scope.querySelectorAll('.weui-desktop-switch')).find(s => /已开启|快捷转载|所有账号均可转载/.test((s.parentElement && s.parentElement.textContent) || ''));
    if (qsw) {
      const qcb = qsw.querySelector('input[type=checkbox]');
      if (qcb && qcb.checked) { qcb.click(); quickOff = true; }
      else if (!qcb) { qsw.click(); quickOff = true; }
    }
    // 勾选协议（要点 checkbox 本身，不是 label 文本）
    let agreeCb = null;
    const agreeLabel = Array.from(scope.querySelectorAll('label')).find(l => /我已阅读并同意/.test(l.textContent));
    if (agreeLabel) agreeCb = agreeLabel.querySelector('input[type=checkbox]');
    if (agreeCb && !agreeCb.checked) agreeCb.click();
    else if (!agreeCb) {
      const cb = Array.from(scope.querySelectorAll('input[type=checkbox]')).find(c => /我已阅读并同意/.test((c.parentElement && c.parentElement.textContent) || ''));
      if (cb && !cb.checked) cb.click();
    }
    // 点确定
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

  // ===== 4. 赞赏（勾选开关本身，并处理弹窗） =====
  const rewardOpen = await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.appmsg-editor__setting-group')).find(e => /赞赏/.test(e.textContent) && e.childElementCount < 10);
    if (el) el.click();
    return !!el;
  });
  await page.waitForTimeout(1500);
  let rewardState = 'NOT_AVAILABLE';
  if (rewardOpen) {
    const switched = await page.evaluate(() => {
      // 切换"赞赏"开关（setting-group__switch.js_reward_open 包裹 checkbox）
      const switchWrap = document.querySelector('.setting-group__switch.js_reward_open');
      const cb = switchWrap && switchWrap.querySelector('input[type=checkbox]');
      if (cb) {
        if (!cb.checked) { cb.click(); return true; }
        return 'ALREADY_ON';
      } else if (switchWrap) {
        switchWrap.click(); return true;
      }
      return false;
    });
    await page.waitForTimeout(2000);
    // 赞赏协议弹窗：选"赞赏作者"radio + 勾选协议checkbox + 点确定
    const rewardConfirm = await page.evaluate(() => {
      const d = Array.from(document.querySelectorAll('.weui-desktop-dialog')).find(e => /赞赏类型|赞赏作者|赞赏账户/.test(e.innerText) && e.getBoundingClientRect().height > 100);
      if (!d) return { dlgFound: false };
      const scope = d;
      // 1) 选中"赞赏作者"radio (value=1)
      const authorRadio = scope.querySelector('input[type=radio][value="1"], weui-desktop-form__radio[value="1"]') || Array.from(scope.querySelectorAll('input[type=radio]')).find(e => e.value === '1');
      if (authorRadio) authorRadio.click();
      // 2) 勾选协议最前面的 checkbox（"我已阅读并同意《微信公众平台赞赏功能使用协议》"）
      const agreeLabel = Array.from(scope.querySelectorAll('label')).find(l => /我已阅读并同意/.test(l.textContent));
      const agreeCb = agreeLabel ? agreeLabel.querySelector('input[type=checkbox]') : null;
      if (agreeCb && !agreeCb.checked) agreeCb.click();
      else if (!agreeCb) {
        // 兜底：直接找协议checkbox
        const cb = Array.from(scope.querySelectorAll('input[type=checkbox]')).find(c => /我已阅读并同意/.test((c.parentElement && c.parentElement.textContent) || ''));
        if (cb && !cb.checked) cb.click();
      }
      return { dlgFound: true, authorRadio: !!authorRadio, agreeCbFound: !!agreeCb };
    });
    await page.waitForTimeout(800);
    // 点弹窗内"确定"
    const confirmBtn = await page.evaluate(() => {
      const d = Array.from(document.querySelectorAll('.weui-desktop-dialog')).find(e => /赞赏类型|赞赏作者|赞赏账户/.test(e.innerText) && e.getBoundingClientRect().height > 100);
      const scope = d || document;
      const btns = Array.from(scope.querySelectorAll('a,button,span')).filter(e => e.textContent.trim() === '确定' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
      if (btns.length) { btns[btns.length - 1].click(); return 'REWARD_CONFIRMED'; }
      return 'NO_REWARD_CONFIRM';
    });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_reward_done.png' });
    const finalTip = await page.evaluate(() => (document.querySelector('.js_reward_setting_tips') || {}).textContent || '');
    rewardState = { switched, rewardConfirm, confirmBtn, finalTip };
  }
  console.log('步骤4: 赞赏状态', JSON.stringify(rewardState));
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_ready.png' });

  // ===== 5. 发表 =====
  if (NOPUB) {
    console.log('NOPUB模式: 仅验证字段与封面，不点发表');
    await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_nopub_final.png' });
    await ctx.close();
    return;
  }
  const pubClick = await page.evaluate(() => {
    const btn = document.querySelector('button.mass_send') || Array.from(document.querySelectorAll('a,button,span')).find(e => e.textContent.trim() === '发表' && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (btn) { btn.click(); return 'PUB_CLICKED'; }
    return 'NO_PUB_BTN';
  });
  console.log('步骤5: 发表按钮', pubClick);
  await page.waitForTimeout(3000);
  // 若有"发表"确认弹窗（含群发通知开关），先关闭群发，再点发表
  const confirmPub = await page.evaluate(() => {
    const hasPublishDialog = /群发通知|分组通知|定时发表/.test(document.body.innerText);
    if (hasPublishDialog) {
      // 找到"群发通知"那一行，关闭其中的checkbox
      const rows = Array.from(document.querySelectorAll('div,li,label,section')).filter(e => /群发通知/.test(e.textContent) && e.querySelector('input[type=checkbox], [class*=switch]') && e.getBoundingClientRect().height > 0 && e.getBoundingClientRect().height < 160);
      let closed = false;
      for (const row of rows) {
        const cb = row.querySelector('input[type=checkbox]');
        if (cb && cb.checked) {
          cb.click(); closed = true; break;
        }
      }
      if (!closed) {
        // 兜底：找群发通知行里的label开关点击
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
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_pub_dialog.png' });
  // 点"发表"确认按钮
  const publishBtn = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e => /^发表$/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (btns.length) { btns[btns.length - 1].click(); return 'PUBLISH_CONFIRMED'; }
    return 'NO_PUBLISH_BTN';
  });
  console.log('步骤5c: 点发表确认', publishBtn);
  await page.waitForTimeout(3500);
  // 点"继续发表"（未开启群发通知的最终确认）
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_pub_dialog2.png' });
  const continueBtn = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e => /^继续发表$/.test(e.textContent.trim()) && e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0);
    if (btns.length) { btns[btns.length - 1].click(); return 'CONTINUE_CLICKED'; }
    return 'NO_CONTINUE_BTN';
  });
  console.log('步骤5d: 继续发表', continueBtn);
  await page.waitForTimeout(4000);

  // ===== 6. 检测扫码二维码并等待用户扫码 =====
  const scanState = await page.evaluate(() => {
    const txt = document.body.innerText;
    return {
      hasQR: /扫码|扫一扫|二维码|请使用微信扫描/.test(txt),
      hasSuccess: /发表成功|已群发|发送成功|发布成功/.test(txt),
      hasFail: /图片不能为空|请设置封面|校验失败|操作失败/.test(txt),
      hasGroupConfirm: /群发|发送给全部/.test(txt)
    };
  });
  console.log('步骤6: 扫码检测', JSON.stringify(scanState));

  // 截图二维码
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_scan.png' });

  if (scanState.hasQR) {
    console.log('WAIT_SCAN: 请用户扫码');
    // 轮询等待真正发表成功（必须出现明确成功文案，不能仅凭二维码消失判定）
    let success = false;
    const t0 = Date.now();
    while (Date.now() - t0 < 200000) {
      await page.waitForTimeout(5000);
      const s = await page.evaluate(() => {
        const txt = document.body.innerText;
        return {
          hasSuccess: /发表成功|已群发|发送成功|发布成功|已发送/.test(txt),
          hasQR: /扫码|扫一扫|二维码/.test(txt),
          hasDialog: /发表|群发|确认/.test(txt)
        };
      });
      if (s.hasSuccess) { success = true; break; }
      // 二维码消失还不能判定成功：再检查页面是否明确进入成功态或已无发表相关弹窗
      if (!s.hasQR) {
        // 再等6秒，若仍出现"发表成功/已发送"才算成功；否则继续等待真实成功文案
        await page.waitForTimeout(6000);
        const s2 = await page.evaluate(() => {
          const txt = document.body.innerText;
          return { hasSuccess: /发表成功|已群发|发送成功|已发送/.test(txt) };
        });
        if (s2.hasSuccess) { success = true; break; }
        // 若二维码消失但没有任何成功提示字眼，视为"还在处理"继续轮询，不误判
      }
    }
    console.log(success ? 'SCAN_SUCCESS' : 'SCAN_TIMEOUT');
    await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/p' + idx + '_results.png' });
  } else if (scanState.hasFail) {
    console.log('FAIL: ' + JSON.stringify(scanState));
  } else if (scanState.hasSuccess) {
    console.log('结果: 发表成功（未弹码）');
  } else {
    console.log('未知状态: ' + JSON.stringify(scanState));
  }

  await ctx.close();
})().catch(e => { console.error('ERROR:', e.stack || e.message); process.exit(1); });