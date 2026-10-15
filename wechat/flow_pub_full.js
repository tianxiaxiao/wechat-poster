const { chromium } = require('playwright-core');
const EXE = 'C:/Users/Administrator/.agent-browser/browsers/chrome-152.0.7977.82/chrome.exe';
const PROFILE = 'C:/Users/Administrator/.workbuddy/wx-profile';
const TITLE = '偏头痛这毛病，没疼过的人不懂';
const AUTHOR = '百秘一梳';
const PARAS = [
'说真的，偏头痛这毛病，没疼过的人根本理解不了。',
'不是普通那种"有点晕"。是太阳穴像被人用指节一下一下地顶，疼起来连光都嫌亮，屏幕亮度调到最低，眼睛半眯着，什么活儿都干不了，只想找个没人的地方躺平。',
'我这毛病是前几年落下的。工作节奏一起来，咖啡当水喝，晚上手机刷到一两点，白天对着电脑一坐就是十个小时。刚开始只是后脑勺发紧，我没当回事，后来就发展成动不动疼上一整天。',
'药也吃过，布洛芬家里常备。但老吃不是办法，胃先受不了。',
'有次回老家，我妈看我按着太阳穴窝在沙发上，也没多问，就翻出她用了好多年的一把木梳子，让我"梳梳头"。',
'我当时还想笑，觉得这就是老一辈那种"多喝热水"式的关心。架不住她念叨，就顺着发际线从前到后梳了几下。',
'你别说，还真有点不一样。梳齿贴着头皮慢慢刮过去，太阳穴、后颈那一片，像被捋顺了一样，紧绷感松下来一点。',
'后来我才知道，那把梳子不是普通木梳，叫百秘一梳。桃木做的，里头封了何首乌、当归、川芎这些本草。梳头的时候，手温和头皮的体温会把药油一点点化开，渗进头皮。川芎这味药，懂点中医的都熟，治头痛的老方子里基本都有它。',
'现在它成了我办公桌上最不起眼、但天天用的东西。下午三四点头开始发沉，我就拿起来，从前额梳到后颈，来回几下，就当给自己"松一松发条"。',
'我不敢说它治好了我的偏头痛——那太玄了，也不诚实。真疼得厉害，我照样吃药、照样关灯躺下。但它确实帮我在那种"快要疼起来"的关口，把那股劲儿缓了下来。',
'就冲这个，我觉得值。',
'头疼别硬扛。每天留几分钟，认真梳梳头，真的比想象中有用。'
];
const BODY = PARAS.map(p=>'<p>'+p+'</p>').join('');
(async () => {
  const ctx = await chromium.launchPersistentContext(PROFILE, {
    executablePath: EXE, headless: true,
    viewport: { width: 1440, height: 1100 }, args: ['--no-sandbox'],
  });
  const page = await ctx.newPage();
  page.setDefaultTimeout(45000);
  const url = 'https://mp.weixin.qq.com/cgi-bin/appmsg?t=media/appmsg_edit_v2&action=edit&isNew=1&type=77&token=1916614841&lang=zh_CN';
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 60000 });
  for (let i=0;i<20;i++){ await page.waitForTimeout(1500); if (await page.evaluate(()=>/请在这里输入标题/.test(document.body.innerText))) break; }
  await page.waitForTimeout(1500);
  // 1. 标题/作者/正文
  await page.evaluate((o) => {
    const ts = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;
    const ta=document.querySelector('textarea.js_title'); ts.call(ta,o.TITLE); ta.dispatchEvent(new Event('input',{bubbles:true}));
    const is = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
    const ai=document.querySelector('input.js_author'); is.call(ai,o.AUTHOR); ai.dispatchEvent(new Event('input',{bubbles:true}));
    const pm = Array.from(document.querySelectorAll('div.ProseMirror')).find(e=>/从这里开始写正文/.test(e.textContent))||document.querySelector('div.ProseMirror');
    if (pm) { pm.innerHTML = o.BODY; pm.dispatchEvent(new Event('input',{bubbles:true})); }
  }, {TITLE, AUTHOR, BODY});
  await page.waitForTimeout(1500);
  // 2. 原创
  await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.appmsg-editor__setting-group')).find(e=>/原创/.test(e.textContent) && e.childElementCount<10);
    if (el) el.click();
  });
  await page.waitForTimeout(1500);
  // 勾协议
  const agree = await page.evaluate(() => {
    const label = Array.from(document.querySelectorAll('.weui-desktop-dialog label, label, a')).find(e=>/我已阅读并同意/.test(e.textContent));
    if (label) { label.click(); return 'AGREE_CLICKED'; }
    return 'NO_AGREE';
  });
  console.log('协议:', agree);
  await page.waitForTimeout(800);
  // 点确定
  const confirm = await page.evaluate(() => {
    const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e=>e.textContent.trim()==='确定');
    const vis = btns.find(e=>e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0);
    if (vis) { vis.click(); return 'CONFIRM_CLICKED'; }
    return 'NO_CONFIRM count=' + btns.length;
  });
  console.log('原创确定:', confirm);
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/s8_original_done.png' });

  // 3. 赞赏
  await page.evaluate(() => {
    const el = Array.from(document.querySelectorAll('.appmsg-editor__setting-group')).find(e=>/赞赏/.test(e.textContent) && e.childElementCount<10);
    if (el) el.click();
  });
  await page.waitForTimeout(1500);
  const rw = await page.evaluate(() => {
    const txt = document.body.innerText;
    const hasAgree = /我已阅读并同意|赞赏协议/.test(txt);
    const agree = Array.from(document.querySelectorAll('label,a,span')).find(e=>/我已阅读并同意|同意/.test(e.textContent));
    if (agree) { agree.click(); }
    const btns = Array.from(document.querySelectorAll('a,button,span')).filter(e=>e.textContent.trim()==='确定');
    const vis = btns.find(e=>e.getBoundingClientRect().width>0 && e.getBoundingClientRect().height>0);
    if (vis) vis.click();
    return { hasAgree, done: true };
  });
  console.log('赞赏:', JSON.stringify(rw));
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/s9_reward_done.png' });

  // 4. 点发表
  await page.evaluate(() => {
    const btn = document.querySelector('button.mass_send') || Array.from(document.querySelectorAll('a,button,span')).find(e=>e.textContent.trim()==='发表' && e.getBoundingClientRect().width>0);
    if (btn) btn.click();
  });
  await page.waitForTimeout(3000);
  await page.screenshot({ path: 'D:/测试公众号批量生成/wechat/s10_after_pub.png' });
  const state = await page.evaluate(() => {
    const txt = document.body.innerText;
    return { hasQR: /扫码|扫一扫|二维码/.test(txt), hasPublishFail: /图片不能为空|请设置封面|校验失败/.test(txt), url: location.href.slice(0,90) };
  });
  console.log('发表后状态:', JSON.stringify(state));
  await ctx.close();
})().catch(e => { console.error('ERROR:', e.message); process.exit(1); });
