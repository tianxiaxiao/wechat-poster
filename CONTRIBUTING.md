# 贡献指南 · Contributing

感谢你有兴趣改进这个项目。请先读一下下面的约定，能让合并顺利很多。

## 开发环境

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r app/requirements.txt
cp app/.env.example app/.env      # 填入自己的测试凭证（切勿使用生产账号做实验）
cd app/wxcard && npm install && cd ../..
cd app && python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

## 提交前必须做的事

```bash
cd app
python tests/test_auth_flow.py      # 认证/权限/日志：38 项，必须全绿
python tests/test_ui_main_page.py   # 前端渲染断言：16 项，必须全绿
```

**改前端（CSS/HTML）时必须跑第二套**：它用无头浏览器读计算样式与元素位置，
能抓住"代码看着对、页面显示错"这类问题（本项目历史上踩过三次）。

## 代码约定

- 后端：Python 3.11+，标准库优先（`urllib`/`hmac`/`hashlib` 等），新增依赖请在 PR 里说明必要性
- 前端：单文件 `app/static/index.html`，Tailwind CDN；**品牌主色是红 `#E30012`**
  （实现方式是把 Tailwind 的 `blue` 色阶整体重映射 → **不要用 `blue-*` 表示真正的蓝色**，需要蓝用 `sky/indigo`）
- 前端**不要**改成每 2 秒整块重建 DOM：必须保留"内容级 diff（`statesEqual`）+ 卡片缓存"的机制，
  否则会闪屏、无法滚动、二维码无法扫码
- 卡片渲染相关改动：`app/wxcard/compare_all.js` 可与原 md2card 产物逐字节对比，
  有意偏离（如把 `<hr>` 换成 `<section>` 以避免微信客户端显示多余斜杠）请在注释里写清楚原因
- 提交信息：`<模块>: <做了什么>`，例如 `auth: 支持按 IP 与用户名双重登录节流`

## 安全红线（不接受违反的 PR）

- 不提交任何真实密钥、真实用户数据、真实文章；示例一律用占位符
- 不新增"接管他人账号""绕过平台风控""采集他人凭证"类功能
- 涉及对外发布的逻辑改动，必须保留**操作日志留痕**与**频率保护**（间隔不可缩短）

## Issue / PR 模板要点

- Bug：复现步骤 + 期望/实际 + 日志片段（**记得把密钥打码**）+ 环境（版本、部署方式）
- Feature：要解决的问题 + 你的使用场景（比"实现方案"更重要）
