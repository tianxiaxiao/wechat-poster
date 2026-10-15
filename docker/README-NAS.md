# 润致公众号自动发布工具 · 群晖 NAS 部署指南（含登录鉴权与 Cloudflare 隧道）

**镜像能力**：AI 生成正文 → 富排版 → 发布到公众号「发表记录」（API 免扫码、可无人值守定时）。

**本版本新增（2026-10-14）**：
- 🔐 **登录鉴权**：必须登录才能使用；三种角色（管理员 / 编辑 / 只读）；密码 PBKDF2 加盐散列
- 👥 **用户管理**：管理员可增删用户、改角色、停用、重置密码；AppSecret 仅管理员可见
- 📜 **操作日志**：谁在什么时候、从哪个 IP、做了什么，全部留痕，可筛选、可导出 CSV
- 🌐 **Cloudflare 隧道**：不用公网 IP、不用开端口，手机随时可访问
- 🛡 配套加固：登录失败限流（同 IP 连错 8 次锁 5 分钟）、Cookie HttpOnly/Secure、只读账号禁止一切写操作

> ⚠️ 精简镜像**不含扫码发表**（原创+赞赏勾选）能力——扫码依赖本机已登录微信的浏览器会话。日常发文 / 定时任务走 API 路径不受影响。

---

## 零、TL;DR（你的 NAS 已实测，照抄即可）

```bash
# 1) 上传项目到 /volume1/docker/poster/
#    根目录需要：docker-compose.yml、.dockerignore、.env
#    （含 app/、docker/；docker/ 里要有 .env 或 .env.example）
# 2) 把密钥放到【项目根】的 .env（compose 直接读它，v1 也强制要它在项目根）
cd /volume1/docker/poster
sudo cp docker/.env .env          # 已有密钥文件时最省事；没有就照 docker/.env.example 填
sudo vi .env                      # 检查 DEEPSEEK_API_KEY / WX_APPID / WX_SECRET 是否到位

# 3) 建数据目录（compose v1 不会自动创建绑定挂载的宿主目录，必须先建）
mkdir -p /volume1/docker/poster/data

# 4) 构建并启动（带横杠的 docker-compose + sudo + 绝对路径）
sudo $(which docker-compose) up -d --build

# 5) 看日志 / 找初始管理员密码
sudo $(which docker-compose) logs -f poster
```

> 首次构建要拉基础镜像 + 装依赖（5~15 分钟）。**大陆网络常见报错**（Docker Hub 超时 / 502 / pip 超时）
> 与解法见**第十一节**。

> 群晖实测：**没有** `docker compose`（v2 子命令），**有** `docker-compose` 1.28.5；
> 且 `docker` 必须加 `sudo`（否则 `permission denied /var/run/docker.sock`）。详见第三节方式二。
>
> ⚠️ **密钥文件只放一处：项目根 `.env`**。compose v1 会强制在项目根找 `.env`（缺了报
> `ERROR: Couldn't find env file: /volume1/docker/poster/.env`），而 compose 里 service 的 `env_file`
> 也指向同一个文件 —— 一个文件到底，避免"两个 .env 谁生效"的歧义。

> 🚑 **传错文件了？**（例如把本机整个 `app/` 覆盖到 NAS 的 `app/` 上）→ 见文末
> **附录 A：误操作急救**。**先按 A.2 做只读诊断，别急着删任何东西**——多数情况下生产数据毫发无伤。

---

## 一、把文件传到 NAS（照这个清单准备）

NAS 上建目录 `/volume1/docker/poster/`，**最终结构如下**（★ = 必须，其余可省）：

```
/volume1/docker/poster/                 ← 项目根（Container Manager「项目」路径就选这里）
├── ★ docker-compose.yml                    群晖图形界面/命令行用（build context 在本文件夹内）
├── ★ .dockerignore                         必须放这里！否则密钥和数据库会被打进镜像
├── ★ .env                                  ★★ 密钥文件（compose 直接读它；`cp docker/.env .env` 即可生成）
├── ★ app/                                  程序代码
│   ├── app.py  auth.py  audit.py  db.py  llm.py  notify.py  wechat_api.py  wxcard.py …
│   ├── wxcard/                             卡片排版（含 node_modules 里的 jsdom，别漏传！）
│   ├── static/                             index.html + login.html + logo.png
│   └── data/                               ★ 首次启动会自建；迁移历史数据时把 poster.db 放这里
├── ★ docker/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── .env                                ★★ 你的密钥（部署时 `cp` 一份到项目根即可）—— 别漏！
│   ├── .env.example
│   ├── docker-compose.yml                  命令行版（context 是上一级，供 SSH 用）
│   └── README-NAS.md
└── ○ docker-compose.cloudflare.yml         仅当「想让本仓库一并管隧道」才需要；
                                            你已有自己的隧道 → **不用传，也别启动**
```

> ⚠️ **别把 `docker/docker-compose.yml` 拷到项目根用**：那份的 `context: ..` 指向项目文件夹之外，
> 群晖会判「文件格式无效」，命令行下也会把 build 上下文指到上一层。项目根要用**根目录那份** compose。

**上传方式**（群晖 File Station 直接拖也行）：

```bash
# 在 Windows 上（把整个项目传上去；data/ 与 .env 里的密钥注意保密）
scp -r D:\测试公众号批量生成\app yourname@192.168.1.100:/volume1/docker/poster/
scp -r D:\测试公众号批量生成\docker yourname@192.168.1.100:/volume1/docker/poster/
scp D:\测试公众号批量生成\docker-compose.yml D:\测试公众号批量生成\.dockerignore D:\测试公众号批量生成\.env yourname@192.168.1.100:/volume1/docker/poster/
```

**最容易漏的四样**：① `docker/.env`（没它无密钥；**群晖 File Station 默认隐藏点开头的文件，要先在「设置」里勾上「显示隐藏文件」**）；
② `docker-compose.yml`；③ `.dockerignore`；④ 根目录的 `.env`（内容可为空，缺了 v1 会报 `Couldn't find env file`）。
另外 `app/wxcard/node_modules` 也别漏（漏了文章会退回旧版排版）。

> 两个 compose 的区别：**根目录那份**给群晖图形界面用（build context 在项目文件夹内，且带 `version` 键），
> **docker/ 目录那份**给 SSH 命令行用。内容等价，任选其一，不要同时起。

## 二、填好 .env（含初始管理员密码）

把 `docker/.env.example` 复制成 `docker/.env`，至少填这几项：

```ini
DEEPSEEK_API_KEY=sk-你的key
WX_APPID=wx-your-appid-here
WX_SECRET=你的appsecret
ROBOT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key   # 可选，跑完通知

# ==== 登录相关（强烈建议填）====
APP_SECRET_KEY=随便一串32位以上随机字符      # 会话签名密钥；不填会自动生成并存在数据库里
ADMIN_USERNAME=admin
ADMIN_INIT_PASSWORD=换成你自己的强密码123     # 首次启动用它创建管理员；不填则随机生成
```

> `ADMIN_INIT_PASSWORD` 只在**首次启动、数据库还没有任何用户时**生效。
> 若留空：系统会随机生成密码，写入数据目录 `管理员初始密码.txt`，并在日志里打印：
> `sudo docker logs wechat-poster | grep 初始密码`

## 三、构建并启动（三种方式，按你的群晖情况选一种）

### 方式一（最稳，推荐）：SSH 命令行，不用 compose ★

**群晖很多机型的 `docker` CLI 没有 `compose` 子命令**（`docker compose up -d` 会报
`unknown shorthand flag: 'd' in -d`），这条路完全绕开它：

```bash
cd /volume1/docker/poster

# 0) 确认密钥文件在（没有就从模板复制后编辑）
ls docker/.env || cp docker/.env.example docker/.env && vi docker/.env

# 1) 构建镜像（首次约 5-15 分钟，取决于 NAS 性能）
sudo docker build -f docker/Dockerfile -t poster-api:1.1 .

# 2) 建数据目录
mkdir -p /volume1/docker/poster/data

# 3) 起容器
sudo docker run -d --name wechat-poster --restart unless-stopped \
  -p 8765:8765 \
  -e TZ=Asia/Shanghai \
  -e POSTER_DB=/srv/poster/app/data/poster.db \
  -e COOKIE_SECURE=auto \
  --env-file docker/.env \
  -v /volume1/docker/poster/data:/srv/poster/app/data \
  poster-api:1.1

# 4) 看日志（首次启动会打印管理员账号，或提示密码文件位置）
sudo docker logs -f wechat-poster
```

**先自检一下你的 docker 支持哪种写法**（把结果发我更快定位）：

```bash
docker version --format '{{.Server.Version}}'       # Docker 引擎版本
docker compose version   2>/dev/null || echo "无 docker compose 子命令"
docker-compose version   2>/dev/null || echo "无 docker-compose"
```

### 方式二：SSH + docker-compose（你这台群晖实测可用 ✓）

**⚠️ 群晖的两种写法别搞混**（2026-10-15 在 NAS 上实测）：

| 命令 | 结果 |
|---|---|
| `docker compose version` | ❌ 无此子命令（群晖的 docker CLI 不带 v2 插件）→ 所以 `docker compose up -d` 报 `unknown shorthand flag: 'd'` |
| `docker-compose version` | ✅ 有：**docker-compose 1.28.5**（带横杠的老版） |
| `docker version`（不加 sudo） | ❌ `permission denied` → **必须用 sudo**（或把用户加入 docker 组） |

所以正确命令是（**带横杠 + sudo + 绝对路径**，避开 sudo 的 PATH 限制）：

```bash
cd /volume1/docker/poster            # 项目根，不是 docker/ 子目录
sudo $(which docker-compose) up -d --build

# 后续常用
sudo $(which docker-compose) ps
sudo $(which docker-compose) logs -f poster
sudo $(which docker-compose) restart poster
sudo $(which docker-compose) down          # 停止（数据目录保留）
```

> 若 `sudo docker-compose` 报 `command not found`，就是因为 sudo 的 PATH 不含 `/usr/local/bin`，
> 用上面的 `sudo $(which docker-compose)` 即可（shell 先把路径展开再交给 sudo）。

### 方式三：群晖「Container Manager」图形界面（项目）

1. 打开 **Container Manager → 项目 → 新增**
2. 项目名称：`poster`；**路径选 `/volume1/docker/poster`（项目根，不要选 docker/ 子目录）**
3. 来源选「使用现有的 docker-compose.yml」（它会自动选中根目录那份）
4. 点「下一步 → 完成」等待构建
5. 构建完成后容器自动运行（`restart: unless-stopped`，NAS 重启也会自动拉起）

> 若图形界面仍提示**「文件格式无效」**：多半是路径选到了 `docker/` 子目录（那份的 build context 指向上一级，会被判定越界）。
> 请改选项目根；或直接用上面的**方式一**。

## 四、登录与验证

浏览器打开：`http://<NAS的IP>:8765` → 会自动跳到登录页。

| 检查项 | 预期 |
|---|---|
| 未登录访问 | 自动跳转 `/login`（接口返回 401） |
| 登录 | 用 `admin` + `ADMIN_INIT_PASSWORD`；首次登录会提示改密码 |
| 登录后 | 看到「润致公众号自动发布工具」表单，右上角显示当前用户与「退出」 |
| 管理员额外可见 | 「👥 用户管理」「📜 操作日志」两个按钮 |
| 健康检查 | 浏览器打开 `http://<NAS的IP>:8765/health` 返回 `{"ok":true,...}` |

**第一次进来必做**：右上角「🔑 改密码」改掉初始密码。

## 五、用户与权限（重点）

| 角色 | 能做什么 | 不能做什么 |
|---|---|---|
| **管理员 admin** | 全部功能：生成、发布、定时、用户管理、操作日志、公众号 AppSecret | — |
| **编辑 editor** | 生成与发布、改定时、维护主题/产品 | 用户管理、操作日志、查看 AppSecret |
| **只读 viewer** | 只浏览任务列表与日志 | 一切写操作（按钮置灰，接口也会 403） |

给别人开账号：**用户管理 → 新增用户**（用户名 3-32 位字母数字），密码至少 8 位。
安全底线（代码里已强制）：**不能删除/停用自己**，**必须保留至少一名启用状态的管理员**。

## 六、公网访问：Cloudflare 隧道

### 6.0 你已经有隧道？（config.yaml 方式 —— **推荐走这条，无需改动**）

如果你已经在本机/其他机器上跑着 cloudflared，且 `config.yaml` 里有类似：

```yaml
ingress:
  - hostname: poster.example.com
    service: http://192.168.1.100:8765      # ← 指向本工具所在机器:端口
  - service: http_status:404                  # 兜底规则必须在最后
```

那么**什么都不用改**（也不要用本仓库的 `docker-compose.cloudflare.yml`，否则会多起一个连接）：

1. 确认 `192.168.1.100` 就是**运行本容器的那台机器**、且 8765 已发布到宿主机：
   ```bash
   # 在被指向的那台机器上：
   ip addr | grep 192.168.1          # 确认本机 IP
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/health   # 应为 200
   # 在跑 cloudflared 的机器上：
   curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.100:8765/health   # 跨机也要能通
   ```
2. 校验隧道配置（改完 config.yaml 后务必执行，能提前发现缩进/语法问题）：
   ```bash
   cloudflared tunnel ingress validate
   cloudflared tunnel ingress rule https://poster.example.com     # 看命中的是不是 gzh 那条规则
   ```
3. **YAML 缩进要严格**（粘贴到聊天里常被吃掉前导空格，务必对一下文件本体）：
   ```yaml
   tunnel: daae3ed7-....
   credentials-file: /home/nonroot/.cloudflared/daae3ed7-....json
   ingress:
     - hostname: poster.example.com
       service: http://192.168.1.100:8765
       originRequest:                       # 该主机的可选高级项，缩进在 hostname 之下
         headers:
           Authorization: "Bearer xxx"
     - service: http_status:404             # 必须最后一条
   ```
   `service` / `originRequest` 必须**缩进到 `hostname` 同级/子级**；若与 `hostname` 顶格对齐，cloudflared 会启动失败。
4. 重启隧道让配置生效：`sudo systemctl restart cloudflared`（或你实际的进程管理方式）。
5. **强烈建议加一层 Cloudflare Access**（见 6.3）——因为这是个能对外发布文章的入口，只有站内密码一道关卡偏薄。

### 6.1 没有现成隧道：在 Cloudflare 后台建一条

1. 登录 **Cloudflare Zero Trust**（one.dash.cloudflare.com）→ **Networks → Tunnels → Create a tunnel**
2. 类型选 **Cloudflared**，起个名字（如 `poster-nas`）
3. 环境选 **Docker**，页面会给出一串命令，形如：
   ```
   docker run cloudflare/cloudflared:latest tunnel --no-autoupdate run --token eyJhIjoi...
   ```
   **只复制 `--token` 后面那串**，填进 `docker/.env`：
   ```ini
   TUNNEL_TOKEN=eyJhIjoi...
   ```
4. 同一页面往下 **Public Hostname** 添加一条：
   - Subdomain/Domain：如 `poster.你的域名.com`
   - Service：**`http://poster:8765`**（容器名 + 端口；也可填 `http://<NAS-IP>:8765`）

### 6.2 在 NAS 上启动隧道

**有 docker-compose 的（你的机器属于这种）：**

```bash
cd /volume1/docker/poster
sudo $(which docker-compose) -f docker-compose.yml -f docker-compose.cloudflare.yml up -d
```

**没有 compose（只有 docker build/run 的，用这条）：**

```bash
cd /volume1/docker/poster
# 方式一：token 通过环境变量传入（推荐，变量名必须叫 TUNNEL_TOKEN）
sudo docker run -d --name poster-cloudflared --restart unless-stopped \
  --env-file docker/.env \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run

# 方式二（等价，把 token 直接写在命令里）：
sudo docker run -d --name poster-cloudflared --restart unless-stopped \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run --token 你的token
```

> ⚠️ 这种（cloudflared 与本体分别在两个容器里、不在同一 compose 网络）情况下，
> Cloudflare 后台 Public Hostname 的 **Service 要填 `http://<NAS的IP>:8765`**，不能填 `http://poster:8765`。
> 用 compose 叠加层启动时才是 `http://poster:8765`。

之后访问 `https://poster.你的域名.com` 即可（手机 4G/5G 也能打开）。

### 6.3 强烈建议再加一层 Cloudflare Access（双保险）

**Zero Trust → Access → Applications → Add an application → Self-hosted**：

- Application domain 填 `poster.你的域名.com`
- 策略（Policy）设为 **Allow** + 邮箱白名单（只允许你自己的邮箱），或选 **One-time PIN**

这样即使有人知道网址，也要先过 Cloudflare 身份校验，再面对站内账号密码 —— **两层**。

### 6.4 三个必须知道的注意事项

| 事项 | 说明 |
|---|---|
| **微信 IP 白名单** | 发布是 NAS **主动出网**调微信接口，微信看到的是你**家里宽带的公网 IP**（与原 Windows 机同一出口时无需改动）。若换到别的网络/机房，需去公众号后台加新 IP，否则报 40164。容器日志会打印被拒 IP |
| **不要暴露 8765 到公网** | 只走隧道即可；路由器上不要做 8765 端口映射。若只想隧道访问，可把 `docker-compose.yml` 里 `ports:` 两行注释掉 |
| **HTTPS 与 Cookie** | 经隧道访问时 `COOKIE_SECURE=auto` 会自动给会话 Cookie 加 Secure。**混用 http 内网地址与 https 域名会各需登录一次**（属正常） |

## 七、数据迁移与本机历史

- 数据（数据库、生成文章、日志、二维码）都在 **NAS 可见目录 `/volume1/docker/poster/data/`**（用 compose 的命名卷则在 Docker 卷里），**删容器不丢数据**。
- **迁移本机历史**（把现在 Windows 上的数据搬到 NAS）：
  ```bash
  # 1) 先停容器
  sudo docker stop wechat-poster
  # 2) 把本机 app/data/poster.db（以及可选的 articles/）传到 NAS 的 data 目录
  #    用 File Station 直接拖进去即可，最终是：
  #    /volume1/docker/poster/data/poster.db
  #    /volume1/docker/poster/data/articles/*.md
  sudo docker start wechat-poster
  ```
  > 迁移后**用户表也会一起带过去**（含本机已改过的密码）；若不带，容器首次启动会自动创建管理员。
- **备份**：群晖「Hyper Backup」定期备份代码+配置目录，或直接备份 Docker 卷目录。

## 八、运维常用命令

```bash
sudo docker logs -f wechat-poster              # 看日志（方式一部署的容器名）
sudo docker logs wechat-poster | grep 初始密码   # 找初始管理员密码
sudo docker restart wechat-poster              # 重启
# 更新代码后重建（方式一）：
cd /volume1/docker/poster && sudo docker build -f docker/Dockerfile -t poster-api:1.1 . \
  && sudo docker rm -f wechat-poster && sudo docker run -d --name wechat-poster \
     --restart unless-stopped -p 8765:8765 -e TZ=Asia/Shanghai \
     -e POSTER_DB=/srv/poster/app/data/poster.db -e COOKIE_SECURE=auto \
     --env-file docker/.env \
     -v /volume1/docker/poster/data:/srv/poster/app/data poster-api:1.1

# 用 compose 的话：
docker compose logs -f poster
docker compose down            # 停止（数据卷/目录保留）
```

**操作日志**：网页 →「📜 操作日志」，可按关键字/级别筛选、翻页、导出 CSV（Excel 打开不乱码）。
落库表名 `audit_logs`。

## 九、端口修改 / 多公众号

- 改前端端口：编辑 `docker-compose.yml`，把 `"8765:8765"` 左侧改成想要的值（如 `"18765:8765"`），重新 `up -d`
- 网页右上「📱 公众号」可继续添加/切换公众号（API 模式支持多号）；**AppSecret 只有管理员能看到**

## 十、ARM 机型

镜像基于 `python:3.13-slim`（官方多架构），AMD64 / ARM64 均可直接构建。群晖 ARM 机型（如 DS223j）也能用，构建会自动拉对应架构。

## 十一、国内网络：拉不到基础镜像 / pip 超时（群晖 + 大陆宽带的必读）

典型报错：

```
Step 1/15 : FROM node:22-slim AS nodebin
Get "https://registry-1.docker.io/v2/": net/http: request canceled while waiting for connection
(Client.Timeout exceeded while awaiting headers)
ERROR: Service 'poster' failed to build
```

这是**网络到 Docker Hub 不通**，不是配置错误。三种解法，按推荐顺序：

### 解法 1：给 Docker 配国内镜像源（一次配好，长期有效）★

**图形界面**：Container Manager → **注册表 / 设置** → 镜像站点（Registry Mirror）→ 新增，填 2-3 个：

```
https://docker.1ms.run
https://docker.m.daocloud.io
https://docker.xuanyuan.me
```

**SSH（更可靠，UI 有时不写进配置）**：

```bash
# DSM 7.1+ 配置文件（Container Manager）
sudo cat /var/packages/ContainerManager/etc/dockerd.json     # 先看现有内容，别整份覆盖！

# 编辑时只增加 registry-mirrors 这一项，例如最终形如：
# {
#   "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io"],
#   ... 原有配置保持不变 ...
# }

# 写完必须重启 Docker 套件（不会热加载）：
sudo synoservice --restart docker 2>/dev/null || sudo synosystemctl restart docker
# 或：套件中心 → Container Manager → 停止 → 启动

# 验证（看到地址即生效）：
sudo docker info | grep -A 3 "Registry Mirrors"
```

> 配 2-3 个源可自动 fallback。镜像源失效/更换很常见，**优先用有口碑的**（上面几个是 2026 年多篇实测推荐的），
> 别用来路不明的"野鸡源"（有植入恶意镜像的风险）。

### 解法 2：手动拉取 + 打回官方标签（不改配置，绕过 DNS/代理问题）

**镜像源经常"拉得动这个、拉不动那个"**（典型：node 拉成功、python 报 `502 Bad Gateway`）。
先探一下哪些源活着，再用活的源逐个拉：

```bash
# ① 探测各镜像源是否可达
#    判读：401 = 正常（仓库要求鉴权，说明服务是活的，能拉镜像）
#          403/302 = 不确定，可以试着拉一下
#          000 / 超时 = 不通，跳过
for M in docker.1ms.run docker.xuanyuan.me docker.m.daocloud.io hub.rat.dev docker.ms; do
  printf "%-30s " "$M"
  curl -s -o /dev/null -w "%{http_code}\n" -m 8 "https://$M/v2/" 2>/dev/null || echo "超时"
done

# ② 用活着的源把基础镜像拉下来（python 那个 502 就换下一个源试）
for M in docker.1ms.run docker.xuanyuan.me docker.m.daocloud.io hub.rat.dev docker.ms; do
  echo "== 尝试 $M =="
  sudo docker pull $M/library/python:3.13-slim && { OK=$M; echo "✅ python 来自 $OK"; break; }
done
sudo docker pull docker.m.daocloud.io/library/node:22-slim     # node 之前已成功

# ③ 把拉下来的镜像打回「官方名字」，这样 Dockerfile 默认值直接命中本地镜像，不再访问仓库
sudo docker tag $OK/library/python:3.13-slim python:3.13-slim
sudo docker tag docker.m.daocloud.io/library/node:22-slim node:22-slim
sudo docker images | grep -E "python|node"      # 核对两个标签都在

# ④ 正常构建（无需再传 --build-arg，因为本地已有 python:3.13-slim / node:22-slim）
cd /volume1/docker/poster
sudo $(which docker-compose) build
sudo $(which docker-compose) up -d
```

> 502 常是**暂时性**的：换个源、或过几分钟重试同一个源，往往就过了。

### 解法 3：构建时用参数直接指定镜像源地址（本仓库已支持）

```bash
cd /volume1/docker/poster
sudo $(which docker-compose) build \
  --build-arg PY_BASE=docker.m.daocloud.io/library/python:3.13-slim \
  --build-arg NODE_BASE=docker.m.daocloud.io/library/node:22-slim \
  --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
  --build-arg NPM_REGISTRY=https://registry.npmmirror.com
sudo $(which docker-compose) up -d
```

> 第三个参数是**给 pip 换国内源**：大陆网络装 Python 依赖（openai/sqlalchemy 等，约 30MB）也常超时，
> 报错形如 `ReadTimeoutError ... pypi.org`。清华源 `https://pypi.tuna.tsinghua.edu.cn/simple`（或阿里源
> `https://mirrors.aliyun.com/pypi/simple`）都在 packagist 白名单内，可放心用。

### 顺带：构建上下文 132MB 偏大？

构建时若显示 `Sending build context to Docker daemon 132.7MB`，通常是两种情况：

1. `docker/` 里那个 58MB 的旧镜像包 `poster-api-1.0.tar.gz` 被算进去了 →
   确认项目根的 `.dockerignore` 是**最新版**（应有 `docker/poster-api-*.tar.gz` 这一行）；
   确认后仍偏大，可把该 tar.gz 移到别处（旧版本镜像，留着也没用）。
2. `.dockerignore` 是旧版（**把 `**/node_modules` 排除了**）→ 这会导致容器里**卡片排版失效**、文章退回旧版样式。
   请务必用仓库里**最新**的 `.dockerignore`（约 1.4KB，含「别整体排除 node_modules」的说明）。



| 现象 | 处理 |
|---|---|
| **`Bind mount failed: '/volume1/docker/poster/data' does not exists`** | compose v1 不会自动建宿主目录 → `mkdir -p /volume1/docker/poster/data` 后再 `up -d` |
| **`Get "https://registry-1.docker.io/v2/": ... Client.Timeout`** | 到 Docker Hub 不通 → 见**第十一节**（配镜像源 / 手动 pull+tag / `--build-arg`） |
| **`received unexpected HTTP status: 502 Bad Gateway`**（FROM 某一行） | 该镜像源对这一层抽风 → **换个源重试**（第十一节解法 2 的探测脚本），拉下来的镜像 `docker tag` 回官方名即可复用 |
| **pip 装依赖超时（`pypi.org` ReadTimeout）** | 用 `--build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple` 重新 build |
| **`ERROR: Couldn't find env file: /volume1/docker/poster/.env`** | 项目根缺 `.env`。一条命令解决：`cd /volume1/docker/poster && sudo cp docker/.env .env`。**若加了 `--env-file` 也无效**，说明 NAS 上这份 compose 的 `env_file` 指向的就是 `./.env`（很可能是把 `docker/docker-compose.yml` 拷到了项目根）→ 请重新上传**项目根**那份 compose（`context: .`、`env_file: ./.env`） |
| **`unable to prepare context: ... lstat /volume1/docker/docker: no such file`** | 同上：用的是 `docker/` 里那份 compose（`context: ..`）。就地改：`sudo sed -i 's|context: \.\.|context: .|' docker-compose.yml`，或上传项目根那份 |
| **`unknown shorthand flag: 'd' in -d`** | 这台群晖没有 v2 的 `docker compose` 子命令 → 改用**带横杠**的 `sudo $(which docker-compose) up -d --build`（本项目已在 NAS 实测通过 docker-compose 1.28.5） |
| **`permission denied ... /var/run/docker.sock`** | `docker` 必须用 `sudo`（群晖默认没把普通用户加进 docker 组）；或执行 `sudo synogroup --add docker 你的用户名` 后重新登录 |
| **`sudo docker-compose: command not found`** | sudo 的 PATH 不含 `/usr/local/bin` → 用 `sudo $(which docker-compose) ...` |
| **Container Manager 建项目报「文件格式无效」** | ① 路径选到了 `docker/` 子目录 → 改选**项目根** `/volume1/docker/poster`（根目录那份 compose 的 build context 在文件夹内、且带 `version` 键）；② 确认根目录那份 `docker-compose.yml` 已上传 |
| 打不开页面 | `sudo docker ps` 看容器是否在跑；`sudo docker logs wechat-poster` 看报错 |
| 一直跳登录页 / 登录后立刻退出 | 浏览器禁用了 Cookie；或混用 http/https 访问（各需登录一次） |
| 忘记管理员密码 | 见本节末「重置管理员密码」 |
| 发布报 40164 | 微信 IP 白名单：把日志里提示的 IP 加进公众号后台（约 10 分钟生效） |
| 发布报 401/token 无效 | 检查 `docker/.env` 里 `WX_APPID`/`WX_SECRET` |
| 生成失败 | 检查 `DEEPSEEK_API_KEY` 是否有效、是否有额度 |
| 定时没触发 | 容器需在运行；时间为北京时间 09:30；服务当天晚启动会在 20:00 前自动补跑一次 |
| 想马上验证定时链路 | 页面把时间改成 2 分钟后，看任务列表是否自动出现新任务 |
| 排版变成旧样式 | 说明卡片渲染不可用（一般缺 node）——启动时与每批前都会自检并推送告警，按告警处理 |

**重置管理员密码（忘记密码时）**：

```bash
# ⚠️ 本项目默认用的是【绑定挂载】（./data → /srv/poster/app/data），不是命名卷。
#    所以这里直接挂宿主数据目录；若你当初改成了命名卷，把左值换回 poster_data 即可。
sudo docker stop wechat-poster
sudo docker run --rm -v /volume1/docker/poster/data:/data python:3.13-slim python - <<'PY'
import sqlite3, hashlib, secrets
db = "/data/poster.db"; salt = secrets.token_hex(16)
dk = hashlib.pbkdf2_hmac("sha256", "你的新密码至少8位".encode(), salt.encode(), 200000)
h = f"pbkdf2_sha256$200000${salt}${dk.hex()}"
c = sqlite3.connect(db)
c.execute("update users set password_hash=?, must_change_pw=0 where role='admin'", (h,))
c.commit(); print("已重置管理员密码")
PY
sudo docker start wechat-poster
```

---

## 附录 A：误操作急救 —— 把 `app/` 目录传错 / 覆盖了 NAS 怎么办

> 本节由一次**真实误操作**总结：把本机整个 `app/` 目录覆盖到 NAS 的 `app/` 上（连同本机的 `app/data/`、`app/.env`）。
> 实测结论是**生产数据零损失**。流程记下来，下次照跑即可。**先诊断，后动手。**

### A.1 先看懂：哪些会受影响，哪些不会

| 东西 | 真正来源 | 覆盖宿主 `app/` 会怎样 |
|---|---|---|
| 容器跑的**代码** | **镜像内** `/srv/poster/app`（不是宿主目录） | 无影响；但**不 rebuild 就不会生效** |
| **数据库/文章/日志/二维码** | bind mount 的 `/volume1/docker/poster/data/` | **碰不到**（数据不在 `app/` 下） |
| 容器**环境变量与密钥** | **项目根** `.env`（compose `env_file: ./.env`） | 无影响 |

关键认知：**宿主 `app/` 目录只是"构建原料"（build context）**，容器根本不从它读代码。
另外项目根 `.dockerignore` 已排除 `**/.env` 与 `app/data`，所以本机的 `app/.env`、`app/data`
**即使被拷到 NAS 也进不了镜像**，rebuild 不会污染容器 —— 但要记得事后清理宿主上的残留。

### A.2 三步只读诊断（先别删任何东西）

```bash
cd /volume1/docker/poster

# ①【最关键】容器到底把宿主哪个目录挂到了 /srv/poster/app/data
sudo docker inspect -f '{{range .Mounts}}{{.Source}} ==> {{.Destination}}{{"\n"}}{{end}}' wechat-poster

# ② 两个 data 目录现状（生产 vs 残留）
ls -la data/
ls -la app/data/ 2>/dev/null

# ③ 让容器自己报生产库家底（最终裁判）
sudo docker exec wechat-poster python -c "import sqlite3;c=sqlite3.connect('/srv/poster/app/data/poster.db');print('用户数',c.execute('select count(*) from users').fetchone()[0]);print('文章数',c.execute('select count(*) from articles').fetchone()[0])"
```

### A.3 判读标准

- ① 显示 `.../poster/data ==> /srv/poster/app/data` → ✅ **生产库安全**，虚惊一场。
- ③ 用户数/文章数与你 NAS 的历史量级相符 → ✅ 数据库完好。
- ② 若 `app/data/poster.db` 的体积/时间与本机开发库一致 → 是本机数据被拷进来了，
  **未被挂载、无害**，可清理。

> 参考指纹（本机开发库，仅供比对，不是生产数据）：
> `3,698,688 字节 / md5 前16 位 2d3cf741bda7fece`。

### A.4 确认无恙后的收尾

```bash
cd /volume1/docker/poster

# 1) 备份真·生产库
sudo cp data/poster.db data/poster.db.bak-$(date +%Y%m%d-%H%M)

# 2) 清掉拷过来、不该留在宿主上的残留
#    ⚠️ 前提：A.2① 已确认挂载源是 ./data；若挂载源恰是 app/data，千万不要删，走 A.5
sudo rm -f  app/.env        # 含本机路径（D:/…），留着日后在 NAS 直接 python app.py 会踩坑
sudo rm -rf app/data        # 本机库 + 一堆 .pid / 调试图片，纯残留

# 3) 重建，让新代码真正生效（覆盖 app/ 后不 rebuild，容器里跑的仍是旧代码）
sudo $(which docker-compose) up -d --build
sudo $(which docker-compose) logs -f --tail=100
```

### A.5 最坏情况：挂载源就是 `app/data`，且已被覆盖

**先停手，别再写任何东西**，按优先级抢救：

1. **容器还没重启** → 旧 inode 可能仍被进程持有，可从句柄拷出：
   ```bash
   sudo ls -l /proc/$(sudo docker inspect -f '{{.State.Pid}}' wechat-poster)/fd | grep poster.db
   # 找到 fd 号 N 后：
   sudo cp /proc/<PID>/fd/<N> /volume1/docker/poster/data/poster.db.recovered
   ```
2. **群晖回收站**：File Station → 该共享文件夹 → `#recycle`。
3. **Hyper Backup / Snapshot Replication** 的历史版本恢复。

即使最坏：**文章本体都在微信后台**，丢的只是「账号 + 发布记录 + 定时设置」，重建即可，不必恐慌。

### A.6 事故后必做的一次核对（确认没有重复发布）

服务晚启动/重建后，调度器会因"当天还没触发过"而在 **20:00 前自动补跑一次**。
用下面这段确认今天的发布量与定时状态，避免重复发文：

```bash
sudo docker exec wechat-poster python -c "
import sqlite3
c=sqlite3.connect('/srv/poster/app/data/poster.db')
cols=[r[1] for r in c.execute('pragma table_info(articles)')]
t='created_at' if 'created_at' in cols else cols[-1]
print('按天发布量:')
for r in c.execute(f'select substr({t},1,10) d, count(*) from articles group by d order by d desc limit 7'): print(' ', r[0], r[1], '篇')
print('settings:')
for k,v in c.execute('select key,value from settings'): print(' ', k, '=', (v or '')[:150])
"
```

- `daily_schedule.last_run_date` **已是今天** → 不会重复发（正常）。
- 若容器日志出现 `触发每日批次` 而今天明明已经发过 → 立刻 `sudo $(which docker-compose) stop`。

### A.7 两条通用规矩

1. **永远不要把本机的 `app/data/` 传到 NAS**——那是本机开发库，会误导后续排查。只传代码。
2. **覆盖宿主 `app/` 后必须 rebuild**，否则新代码不生效（容器跑的是镜像里的旧代码）。
