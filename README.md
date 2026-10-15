# 润致公众号自动发布工具 · WeChat MP Auto Publisher

> 一个**自部署**的微信公众号内容流水线：输入主题 → AI 生成软文 → 富排版 → 发布到公众号。
> 支持官方 API 免扫码发布、每日定时无人值守、多人协作（登录/角色/操作日志）、企业微信通知，可在群晖 NAS + Cloudflare 隧道下运行。

**English**: A self-hosted pipeline for WeChat Official Account content: topic → AI-written article → rich card layout → publish via official API (no QR scan) or via editor automation. Includes daily scheduling, multi-user auth with roles, audit logs, WeCom notifications, and a Docker/群晖(Synology) deployment path.

---

## 目录

- [这是什么](#这是什么)
- [功能](#功能)
- [状态自检与出口 IP](#状态自检与出口-ip)
- [截图](#截图)
- [快速开始](#快速开始)
- [长期后台运行（进程守护）](#长期后台运行进程守护)
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
| 🩺 状态自检 | 标题栏一枚徽章说清「能不能生成 + 能不能发布」；配置页一键直达各平台取 Key；公众号弹层显示出口 IP 与白名单路径 |
| 🐳 部署 | Docker 多阶段镜像（自带 Node 渲染）、群晖 Container Manager、Cloudflare 隧道公网访问 |

---

## 状态自检与出口 IP

发布失败通常只有三个原因：**模型连不上、Key 没配对、出口 IP 不在白名单**。与其等定时任务跑完、
再去看日志猜，不如在页面上直接看到结论。

### 1）标题栏状态徽章

模型标签后面那枚小胶囊就是结论，点一下重新体检。

| 显示 | 含义 | 该去哪处理 |
|---|---|---|
| 🟢 生成与发布都正常 | 模型连通 + 公众号凭证与白名单都通过 | 不用管 |
| 🟡 生成正常 · 发布未验证 | 模型通了，但还没验证过公众号侧 | 点一下做完整体检 |
| 🟠 出口 IP 变了 / 不在白名单 | 凭证没问题，但微信看到的 IP 不在白名单里（报 `40164`） | 微信开发者平台 → 公众号/服务号 → 基础信息 → API IP白名单 |
| 🔴 模型连不上 / 公众号凭证异常 / 缺 API Key | 配置本身有问题 | 见下面两条 |
| ⚪ 未体检 · 点击检测 | 还没检测过，或配置刚改过 | 点一下 |

几点说明：

- **体检不会发布任何文章**。它只做两件事：向模型发一次最小请求、取一次 `access_token`（不建草稿、不发表）。
- 打开页面时先读缓存（秒回），点徽章才真探测；改了模型/Key 之后徽章会自动回到「⚪ 已保存 · 点击体检」，
  不会拿旧结论糊弄你。
- 在「⚙️ 模型设置」里点「🔌 测试连通」，结果会同步给徽章，不用再测一遍。

### 2）取 Key 直达

「⚙️ 模型设置」的 API Key 输入框下面有个 **🔑 去 ×× 获取 Key**：选了哪个来源，就直达哪个平台的
Key 管理页（DeepSeek / 硅基流动 / 阿里云百炼 / 智谱 / 月之暗面 / 火山方舟 / OpenAI）。
`openai_compat` 会按你填的 `base_url` 主机名自动判断；认不出来的来源**不显示链接**（不乱跳），
只提示去你的网关后台取。旁边还有 👁 显示 / 📋 复制，方便核对粘贴有没有缺字符。

### 3）出口 IP 与白名单

「📱 公众号账号管理」顶部有一条 **🌐 发布出口 IP**，显示本机/服务器**真实**的公网出口
（直连国内源探测，不走代理），旁边可以一键复制、一键打开微信开发者平台。

- 「🏠 打开微信开发者平台」固定在这一排，IP 框是固定宽度（不会因为「查询中…」变短而让按钮左右跳）。
- 下面两行分别是**白名单填写路径**和**上次验证通过的 IP**；两者不一致会标黄提醒。
- 家用宽带出口**会漂移**，建议直接填网段（如 `106.85.0.0/16`）省得隔三差五失效。
- 探测用的是**直连**，避免代理节点污染结果——微信看到的和你这里看到的必须是同一个 IP。

> 相关接口：`GET /api/system/status`（`refresh=1` 触发真探测）、`GET /api/system/public-ip`。

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

## 长期后台运行（进程守护）

工具本体是个前台进程（`uvicorn`）——终端一关就没了。想让它「关掉终端还在跑、挂了自动起来、开机自启」，
**按你的部署方式选一条即可，不要全都上**：

| 部署方式 | 怎么常驻 / 守护 | 需要本项目的 `scripts/guard.py` 吗 |
|---|---|---|
| **Docker（含群晖 NAS）** | compose 已配 `restart: unless-stopped`，镜像自带 `HEALTHCHECK` | ❌ 不需要 |
| **Linux** | systemd 单元，`Restart=always` 本身就是守护 → [`deploy/linux/wechat-poster.service`](deploy/linux/wechat-poster.service) | ❌ 不需要 |
| **macOS** | launchd，`KeepAlive` 本身就是守护 → [`deploy/macos/com.wechatposter.plist`](deploy/macos/com.wechatposter.plist) | ❌ 不需要 |
| **Windows** | 系统没有内置服务管理器 → 用本项目提供的守护脚本 + 「启动」文件夹 | ✅ 需要 |

### Windows

```powershell
# 先看一眼：它打算用哪个解释器、什么命令、日志写哪里（不会启动任何东西）
python scripts\guard.py --dry-run

# 跑一次：端口没监听就拉起服务，然后退出
python scripts\guard.py --once

# 常驻守护：默认每 300 秒检查一次，服务掉了自动拉起（--interval 60 可改成每分钟）
python scripts\guard.py --supervise

# 装成开机自启（在当前用户的「启动」文件夹里放一个快捷方式；加 -Uninstall 撤销）
powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1

# ⚠️ 若项目里没有 .venv\，请把「已装好依赖的解释器」显式指定给它，
#    否则守护会用系统 PATH 里的 pythonw 去起服务，很可能因缺 uvicorn 而反复启动失败：
powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 `
           -Python "C:\你的venv\Scripts\python.exe"
```

- 日志：`logs/guard.log`（守护自身）、`logs/server.log`（服务输出），**都是 UTF-8**。
- 脚本内**不含任何硬编码路径**：项目根、Python 解释器、日志目录全部按脚本位置自动推导；
  解释器优先取项目内 `.venv` / `venv`，也可用环境变量 `POSTER_PYTHON` 指定。
- 也可以用 Windows「任务计划程序」建一个「登录时触发」的任务，操作填
  `python <你的路径>\scripts\guard.py --supervise`。
- ⚠️ **别用 .bat 承载含中文的路径**：cmd.exe 按本地代码页读取 .bat，中文路径容易被解成乱码。
  上面的 `.ps1` 生成的是 `.lnk` 快捷方式（路径以 UTF-16 存储），不受影响。

### Linux / macOS

按模板注释里那几步装上即可 —— 平台自带的服务管理器通常比脚本更可靠
（崩溃、OOM、宿主重启都能拉起，日志也能用系统自带工具查看）。

```bash
# Linux（systemd）—— 先把 .service 里的路径改成你的
sudo cp deploy/linux/wechat-poster.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now wechat-poster
systemctl status wechat-poster && journalctl -u wechat-poster -f

# macOS（launchd）—— 先把 .plist 里的路径改成你的
cp deploy/macos/com.wechatposter.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.wechatposter.plist
```

想临时调试就直接前台跑 `python -m uvicorn app:app --host 127.0.0.1 --port 8765`（改完代码记得重启）。

## 配置项

`app/.env`（本地运行）与项目根 `.env`（Docker 运行）字段相同：

| 变量 | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅* | DeepSeek 密钥（*用其它 provider 时不需要） |
| `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | | 默认 `https://api.deepseek.com/v1` / `deepseek-chat` |
| `OPENAI_COMPAT_BASE_URL` / `OPENAI_COMPAT_API_KEY` | | 自建/第三方 OpenAI 兼容网关 |
| `WX_APPID` / `WX_SECRET` | ✅ | 公众号 AppID / AppSecret（**微信开发者平台 → 公众号/服务号 → 基础信息** 获取） |
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

**NAS 上不需要额外的进程守护**：compose 里的 `restart: unless-stopped` +
镜像自带的 `HEALTHCHECK` 已经覆盖「容器退出 / 宿主重启后自动拉起」。
装完确认一下就行：

```bash
sudo docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' wechat-poster   # 期望 unless-stopped
sudo docker inspect -f '{{.State.Health.Status}}' wechat-poster             # 期望 healthy
# 想实测守护是否真的生效（无损：数据在 ./data 绑定目录里）——杀掉它，几秒后应自己起来
sudo docker kill wechat-poster && sleep 8 && sudo docker ps --filter name=wechat-poster
```

> ⚠️ 唯一需要额外手段的情况是进程**假死**（端口还在监听、接口却不响应）：
> `HEALTHCHECK` 只会在 `docker ps` 里标成 `(unhealthy)`，**普通 docker/compose 不会据此重启容器**。
> 真要自动化，可以另跑一个 [`willfarrell/autoheal`](https://hub.docker.com/r/willfarrell/autoheal)
> 容器订阅 unhealthy 事件来重启 —— 一般场景没必要。

## 架构与代码结构

```
app/
├── app.py            FastAPI 主入口：路由、发布编排、定时调度、登录中间件、状态自检/出口 IP
├── auth.py           密码散列(PBKDF2)、会话 Cookie(HMAC 签名)、角色与登录节流
├── audit.py          操作日志写入与查询/导出
├── db.py             SQLAlchemy 模型：账号/产品/主题/文章/设置/用户/日志
├── llm.py            LLM 适配（DeepSeek / Ollama / OpenAI 兼容）
├── wechat_api.py     微信官方 API 封装（token/素材/草稿/发布，纯标准库）+ 无副作用凭证探测
├── wxcard.py         Node 渲染管线封装（失败自动降级旧版排版）
├── wxcard/render.js  md → 微信可用 HTML（按选择器内联 CSS、div→section 等）
├── publisher.py      扫码发布（spawn Playwright 子进程）
├── dedup.py          历史文章相似度去重
├── notify.py         企业微信通知（分片/短链/汇总）
├── static/           单文件前端（index.html / login.html / logo）
└── tests/            端到端测试（临时库 + 独立端口；含浏览器「读计算样式」的渲染断言）
scripts/              guard.py —— Windows 用的轻量进程守护（纯标准库、路径自动推导）
deploy/               常驻运行模板：systemd（Linux）/ launchd（macOS）/ 启动文件夹（Windows）
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

所有测试都用**临时数据库 + 独立端口**，不碰你的线上数据，也不会真的发布文章或调用真实大模型
（需要网络的那几项用「进程内假服务」代替）。每个脚本跑完会打印 `通过 X / 共 Y`。

```bash
cd app
# —— 后端 / 接口（纯 Python）——
python tests/test_auth_flow.py             # 登录、角色、用户管理、操作日志
python tests/test_multitenant.py           # 多租户：账号隔离、每用户设置隔离、越权防护
python tests/test_llm_key_policy.py        # 多用户 Key 策略：必须用自己的 Key / 管理员授权共享
python tests/test_llm_test_conn.py         # 「测试连通」接口（内含进程内假 LLM 服务）
python tests/test_local_llm_one_by_one.py  # 本地/自建模型逐篇生成（打桩计数，验证真实调用次数）
python tests/test_owner_isolation.py       # 产品库 / 主题库归属隔离
python tests/test_topic_product_bind.py    # 主题按产品过滤 + 批量归类 + 旧库迁移
python tests/test_notify_theme.py          # 企业微信通知里的排版主题 + 旧库补列
python tests/test_schedule_admin.py        # 管理员「定时总览 / 代管各用户定时」
python tests/test_system_status.py         # 状态自检与出口 IP（含「体检不会发文」安全断言）
python tests/test_guard_script.py          # 进程守护脚本（打桩，不会真的拉起服务）

# —— 前端渲染断言（无头 Chrome，读计算样式 / 量尺寸）——
# 首次需要：cd wechat && npm install      （提供 playwright-core；本机需装 Chrome）
node tests/render_check_key_policy.js      # Key 可编辑 / 缺 Key 警告 / 授权开关
node tests/render_check_llm_test.js        # 「测试连通」按钮位置与状态颜色
node tests/render_check_topic_product.js   # 主题随产品切换 + 排版尺寸
node tests/render_check_users.js           # 用户管理每行按钮是否真的点得到
node tests/render_check_schedule_admin.js  # 定时总览面板
node tests/render_check_status_ip.js       # 状态徽章颜色/位置、取 Key 直达链接、出口 IP 条
```

渲染断言检查的是「改完 CSS/HTML 后页面**真的**长对了」：读计算样式、量行高与横向溢出、
验证元素是否真在可视区内（例如「保存」按钮有没有被挤出屏幕）。
这类断言比"看代码觉得对"可靠得多——它们抓到过好几个「接口返回正常、页面却点不到」的问题。

另外 `tests/inspect_*.js` 是两个**排查工具**：造长/短数据 → 打开对应弹层 → 截图到 `.tmp_layout/`
并打印每一行的实际尺寸，排版出问题时先用它"看到"，再动手改。

## 安全与合规（请务必阅读）

**这是一个能对外发布内容的工具，请认真对待：**

1. **密钥安全**：`.env`、`data/`、`wechat/config.json` 已在 `.gitignore`/`.dockerignore` 中排除。
   仓库内**不含任何真实凭证**，也请你在 fork/提交时确认没有把密钥带进去。
2. **公网部署必须加鉴权**：本项目自带登录 + 角色 + 操作日志，但更稳的做法是**再叠一层 Cloudflare Access**
   （邮箱白名单 / One-time PIN），或只在内网使用。**不要把 8765 端口直接映射到公网。**
3. **微信 IP 白名单**：API 发布需要先配置服务器出口 IP，否则报 `40164`。
   路径是 **微信开发者平台 → 公众号/服务号 → 基础信息 → API IP白名单**
   （AppID / AppSecret 也在同一页的「开发密钥」里）。
   ⚠️ 公众号的「开发接口管理」已从公众平台迁移到微信开发者平台
   （<https://developers.weixin.qq.com/platform/>），旧路径「公众平台 → 设置与开发 → 安全中心」已废弃。
   页面上的「🏠 打开微信开发者平台」按钮直达控制台页
   <https://developers.weixin.qq.com/console/index?tab1=business&tab2=dev>。
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

**Q: 怎么知道今天到底能不能发出去？**
看标题栏那枚徽章，点一下做一次体检（不会发文章）。红色/橙色都会直接告诉你是模型的问题、
Key 的问题还是 IP 白名单的问题，见 [状态自检与出口 IP](#状态自检与出口-ip)。

**Q: 卡片排版变成旧样式了？**
说明容器/本机缺少 Node 或 jsdom。服务启动与每批开始前都有**排版自检**，失败会告警；
本地可 `cd app/wxcard && npm install` 修复。

**Q: 数据存在哪？**
SQLite（默认 `app/data/poster.db`）+ 文章正文 `app/data/articles/*.md`。备份这两个即可。

**Q: 支持多公众号吗？**
支持，在网页「📱 公众号」中添加多个账号后切换（AppSecret 仅管理员可见）。

## 致谢与许可

- 感谢百秘一梳（重庆）生物科技有限公司邓总、张总和杨总的支持以完成这个工具！他们提供给我的百秘一梳中药桃木梳的效果确实效果好，熬夜头晕、头痛不舒服梳两下马上缓解，特此感谢！
- 卡片排版管线移植并适配自 **[md2card](https://github.com/)「复制到公众号」** 的实现思路（CSS 内联、`div→section` 等），特此致谢。
- 本项目以 **MIT License** 开源，见 [LICENSE](LICENSE)。（如需更换为 Apache-2.0 / AGPL 等，请自行替换 LICENSE 文件并同步本节说明。）
- 免责声明：本软件按"现状"提供，作者不对因使用本软件导致的账号处罚、内容违规或数据丢失承担责任。
