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
