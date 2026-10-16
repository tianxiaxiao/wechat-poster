# bias-publish · 公众号批量发布

把「LLM 生成营销软文 → 微信后台编辑器自动发布 → 用户扫码」一条链路打包成 Web 应用。

## 功能
- Web UI：填主题/数量，点按钮启动
- 后端：DeepSeek 生成去 AI 化的多角度软文
- 发布：复用现有 `wechat/pub_one.js` 的 playwright 流程，自动填字段、选封面、声明原创、开赞赏、关闭群发、扫码
- 所有进度、二维码实时显示在浏览器

## 启动

```bash
# 一次性
python -m venv venv && venv\Scripts\pip install -r requirements.txt

# 复制环境变量并填 DEEPSEEK_API_KEY
cp .env.example .env

# 启动
python app.py
```

浏览器打开 http://127.0.0.1:8765

## 使用流程
1. UI 输入主题（如"偏头痛"），点"开始批量生成+发布"
2. 系统依次：DeepSeek 生成 N 篇 → 第 1 篇发表就绪 → 弹出二维码 → 用户扫码
3. 第 1 篇成功后自动进入第 2 篇，循环直到全部完成
4. 已发布文章会出现在公众号主页

## 依赖
- Python 3.10+
- DeepSeek API key（OpenAI 兼容）
- 微信服务号已认证、已授权（需要后台编辑器可写权限）
- 已安装 Chromium（在 `C:/Users/Administrator/.agent-browser/browsers/chrome-*/chrome.exe`）
- 已存在持久化扫码登录 session（在 `C:/Users/Administrator/.workbuddy/wx-profile`）

## 已知限制
- 同一公众号账号同一时间仅能跑一个 job（持久化 profile 复用）
- 二维码扫码等待最长 200 秒，超时需重试该篇
- 一个 job 内多篇串行，不能并行

## 原创 / 赞赏（打赏）——微信硬限制与当前支持范围
微信官方 API（`draft/add`）**不支持**设「原创声明」和「开启赞赏」，这两项只能由
微信编辑器 UI（playwright 自动化）勾选。因此本项目的行为是：

| 场景 | 原创/赞赏 | 说明 |
| --- | --- | --- |
| 扫码模式（`use_api` 不勾） | ✅ 生效 | 本来就走 playwright 编辑器 UI |
| API 自动发布 → 目标是「本机 playwright 登录号」 | ✅ 生效 | 自动改走 UI 两段式：存带原创/赞赏的草稿 → `freepublish/submit`（免扫码） |
| API 自动发布 → 目标是**非**本机登录号 | ❌ 不生效 | 微信硬限制；任务日志会告警，建议改用扫码模式或换到登录号 |

开关在发布区「✍️ 声明原创 / 💰 开启赞赏」（默认都开，可单独关）。
API 模式下若 UI 两段式失败（浏览器缺失/登录过期），会自动降级纯 API 继续发布（不影响出稿，仅不带原创/赞赏）。

⚠️ 代价：走 UI 两段式的那批文章是**纯文本正文**（与扫码模式一致），不带 API 模式的
卡片排版 HTML（编辑器注入富 HTML 不可靠）。介意排版的话可关掉原创/赞赏，让它们回落到纯 API 卡片排版。
