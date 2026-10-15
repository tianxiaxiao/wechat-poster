# 润致公众号自动发布工具 · WeChat MP Auto Publisher

> 一个**自部署**的微信公众号内容流水线：输入主题 → AI 生成软文 → 富排版 → 发布到公众号。
> 支持官方 API 免扫码发布、每日定时无人值守、多人协作（登录/角色/操作日志）、企业微信通知，可在群晖 NAS + Cloudflare 隧道下运行。

**English**: A self-hosted pipeline for WeChat Official Account content: topic → AI-written article → rich card layout → publish via official API (no QR scan) or via editor automation. Includes daily scheduling, multi-user auth with roles, audit logs, WeCom notifications, and a Docker/群晖(Synology) deployment path.

---

## 目录

- [这是什么](#这是什么)
- [功能](#功能)
- [截图](#截图)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [两种发布方式](#两种发布方式)
- [部署到 NAS（群晖）](#部署到-nas群晖)
- [架构与代码结构](#架构与代码结构)
- [测试](#测试)
- [安全与合规（请务必阅读）](#安全与合规请务必阅读)
- [常见问题](#常见问题)
- [致谢与许可](#致谢与许可)

---

## 这是什么

给**自己的**微信公众号做内容批量生产的自托管工具。区别于第三方 SaaS：

- 所有数据（主题库、文章、密钥、日志）都在**你自己的机器/ NAS** 上，不经过任何第三方服务
- 发布走**微信官方 API**，不使用非官方接口、不接管他人账号
- 面向"长期、稳定、可追溯"的日常更新，而不是刷量

典型使用方式：每天 09:30 自动发布 20 篇围绕产品场景的软文，跑完推一条企业微信汇总（含每篇可点链接）。

## 功能

| 模块 | 说明 |
|---|---|
| 🧠 AI 生成 | 支持 DeepSeek / Ollama(本地) / 任意 OpenAI 兼容网关；产品介绍 + 主题 + 写作要求 → 软文（默认第一人称口语化、800–1200 字） |
| 🔁 内容去重 | bigram Jaccard 相似度比对历史文章（标题/正文），超阈值自动重写，避免同质化 |
| 🎨 卡片排版 | 移植 md2card「复制到公众号」管线：CSS 按选择器内联、`div→section`、渐变兜底等；4 个可选主题，随机分配 |
| 🚀 发布 | ① **API 免扫码**（`draft/add` → `freepublish/submit`）② **扫码发表**（Playwright 走编辑器，带上原创声明/赞赏） |
| ⏰ 定时 | 每日定时无人值守；分批发布规避频率限制；错过时间自动补跑；服务重启后不重复发 |
| 👥 多人协作 | 登录鉴权 + 三级角色（管理员/编辑/只读）、用户管理、强制改密 |
| 📜 操作日志 | 谁、何时、从哪个 IP、做了什么、结果如何，全部留痕，可筛选/导出 CSV |
| 📲 通知 | 企业微信群机器人：每日汇总一条（含链接）、硬故障即时告警（IP 白名单、频率限制） |
| 🐳 部署 | Docker 多阶段镜像（自带 Node 渲染）、群晖 Container Manager、Cloudflare 隧道公网访问 |

## 截图

> 建议自行截图补充到 `docs/screenshots/`（仓库默认不附带真实业务数据截图）。

- 登录页（品牌色 + 简洁表单）
- 主界面：产品/公众号选择、主题多选、定时开关
- 任务列表：每篇状态、文章链接、二维码
- 用户管理 / 操作日志

## 快速开始

### 方式一：本地直接跑（开发/试用）

```bash
git clone <your-repo-url>
cd <repo>

python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r app/requirements.txt

cp app/.env.example app/.env        # 填入 DEEPSEEK_API_KEY / WX_APPID / WX_SECRET
# 卡片排版需要 Node 与 jsdom：
cd app/wxcard && npm install && cd ../..

cd app
python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

打开 <http://127.0.0.1:8765> → 用管理员账号登录。

**首次启动会自动创建管理员**：
- 配了 `ADMIN_INIT_PASSWORD` → 用它登录
- 没配 → 随机生成强密码，写入 `app/data/管理员初始密码.txt` 并打印在启动日志里

### 方式二：Docker

```bash
cp docker/.env.example docker/.env      # 填密钥
cp docker/.env .env                     # compose 读项目根的 .env
mkdir -p data
docker compose up -d --build            # 或 docker-compose up -d --build
```

访问 <http://localhost:8765>。数据落在 `./data/`。

## 配置项

`app/.env`（本地运行）与项目根 `.env`（Docker 运行）字段相同：

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅* | DeepSeek 密钥（*用其它 provider 时不需要） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | | 默认 `https://api.deepseek.com/v1` / `deepseek-chat` |
| `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` | | 自建/第三方 OpenAI 兼容网关 |
| `WX_APPID` / `WX_SECRET` | ✅ | 公众号 AppID / AppSecret（「设置与开发 → 基本配置」） |
| `WX_AUTHOR` | | 默认作者名 |
| `WX_LOGIN_APPID` | | 扫码发表使用的账号 AppID，缺省取 `WX_APPID` |
| `ROBOT_WEBHOOK_URL` | | 企业微信群机器人 Webhook（不填=不通知） |
| `APP_SECRET_KEY` | | 会话签名密钥；不填自动生成并存库 |
| `ADMIN_USERNAME` / `ADMIN_INIT_PASSWORD` | | 初始管理员；密码留空则随机生成 |
| `SESSION_TTL_HOURS` / `COOKIE_SECURE` | | 会话有效期（默认 72h）/ Cookie Secure 策略（`auto`） |
| `POSTER_DB` | | SQLite 路径（默认 `app/data/poster.db`） |
| `TUNNEL_TOKEN` | | Cloudflare 隧道 token（仅用内置 cloudflared 容器时需要） |

> 产品介绍、主题库、公众号账号都可以直接在网页里维护，不必改配置。

## 两种发布方式

| | API 免扫码（推荐） | 扫码发表 |
|---|---|---|
| 实现 | `draft/add` 建草稿 → `freepublish/submit` | Playwright 驱动编辑器 → 弹安全二维码 → 人工扫码 → 提交 |
| 需不需要人 | **不需要**，可定时无人值守 | 需要人在旁边扫码 |
| 原创声明 / 赞赏 | ❌ 官方 API 不支持 | ✅ 可在编辑器里勾选 |
| 文章去向 | 「发表记录」（不主动推送给粉丝、不占群发次数） | 走发表/群发流程，主页可见 |

> 公众号 API 的**频率限制很严**（实测提交间隔需 30 分钟以上，否则易触发 `53501`）。本项目按批次 + 间隔控制节奏，**请勿修改间隔去"提速"**，那只会让账号被限流。

## 部署到 NAS（群晖）

完整步骤见 **[`docker/README-NAS.md`](docker/README-NAS.md)**，含：Container Manager / SSH 两种方式、国内网络构建（镜像源 + pip 源）、Cloudflare 隧道 + Access 双保险、数据迁移、忘记密码重置、常见报错对照表。

要点速览：

```bash
# 群晖（实测 docker-compose v1，需 sudo）
cd /volume1/docker/poster
mkdir -p data
sudo $(which docker-compose) up -d --build
```

## 架构与代码结构

```
app/
├── app.py            FastAPI 主入口：路由、发布编排、定时调度、登录中间件
├── auth.py           密码散列(PBKDF2)、会话 Cookie(HMAC 签名)、角色与登录节流
├── audit.py          操作日志写入与查询/导出
├── db.py             SQLAlchemy 模型：账号/产品/主题/文章/设置/用户/日志
├── llm.py            LLM 适配（DeepSeek / Ollama / OpenAI 兼容）
├── wechat_api.py     微信官方 API 封装（token/素材/草稿/发布，纯标准库）
├── wxcard.py         Node 渲染管线封装（失败自动降级旧版排版）
├── wxcard/render.js  md → 微信可用 HTML（按选择器内联 CSS、div→section 等）
├── publisher.py      扫码发布（spawn Playwright 子进程）
├── dedup.py          历史文章相似度去重
├── notify.py         企业微信通知（分片/短链/汇总）
├── static/           单文件前端（index.html / login.html / logo）
└── tests/            端到端测试（认证 38 项 + UI 渲染 16 项）
docker/               Dockerfile、compose、群晖部署文档
```

数据流：

```
主题+产品 ─→ LLM 生成 md ─→ 去重校验 ─→ wxcard 渲染 HTML ─→ 微信草稿 ─→ 发布
                                         │
                            定时器（每日 09:30）/ 手动触发
                                         │
                              操作日志 + 企业微信通知
```

## 测试

两套可复现的端到端测试（都用**临时数据库 + 独立端口**，不碰你的线上数据）：

```bash
cd app
python tests/test_auth_flow.py     # 登录/角色/用户管理/操作日志 —— 38 项
python tests/test_ui_main_page.py  # 前端渲染断言（无头浏览器）—— 16 项
```

`test_ui_main_page.py` 会检查"改完 CSS/HTML 后页面真的长对了"（读计算样式、量行高、验证元素位置），
这类断言比"看代码觉得对"可靠得多。

## 安全与合规（请务必阅读）

**这是一个能对外发布内容的工具，请认真对待：**

1. **密钥安全**：`.env`、`data/`、`wechat/config.json` 已在 `.gitignore`/`.dockerignore` 中排除。
   仓库内**不含任何真实凭证**，也请你在 fork/提交时确认没有把密钥带进去。
2. **公网部署必须加鉴权**：本项目自带登录 + 角色 + 操作日志，但更稳的做法是**再叠一层 Cloudflare Access**
   （邮箱白名单 / One-time PIN），或只在内网使用。**不要把 8765 端口直接映射到公网。**
3. **微信 IP 白名单**：API 发布需要在公众号后台配置服务器出口 IP，否则报 `40164`。
4. **平台规则**：请遵守微信公众平台运营规范。本工具面向**你自己账号**的日常内容更新；
   请勿用于批量垃圾内容、刷量、绕过平台风控等用途——那会导致账号被封，与工具无关。
5. **扫码发表的边界**：该模式通过浏览器自动化操作**你自己的**公众号后台（因为官方 API 不支持设置原创/赞赏）。
   平台界面变化可能导致失效；是否使用请自行评估风险。
6. **内容责任**：AI 生成内容需人工把关，尤其涉及健康/医疗宣称时请遵守广告法与平台规则。

## 常见问题

**Q: 一定要服务号吗？**
API 发布（`freepublish`）需要**已认证的服务号**权限。订阅号只能用扫码发表模式。

**Q: 每天能发多少篇？**
限制来自微信而非本工具。本项目按批次+间隔保守发布，请勿擅自缩短间隔。

**Q: 卡片排版变成旧样式了？**
说明容器/本机缺少 Node 或 jsdom。服务启动与每批开始前都有**排版自检**，失败会告警；
本地可 `cd app/wxcard && npm install` 修复。

**Q: 数据存在哪？**
SQLite（默认 `app/data/poster.db`）+ 文章正文 `app/data/articles/*.md`。备份这两个即可。

**Q: 支持多公众号吗？**
支持，在网页「📱 公众号」中添加多个账号后切换（AppSecret 仅管理员可见）。

## 致谢与许可

- 卡片排版管线移植并适配自 **[md2card](https://github.com/)「复制到公众号」** 的实现思路（CSS 内联、`div→section` 等），特此致谢。
- 本项目以 **MIT License** 开源，见 [LICENSE](LICENSE)。（如需更换为 Apache-2.0 / AGPL 等，请自行替换 LICENSE 文件并同步本节说明。）
- 免责声明：本软件按"现状"提供，作者不对因使用本软件导致的账号处罚、内容违规或数据丢失承担责任。
