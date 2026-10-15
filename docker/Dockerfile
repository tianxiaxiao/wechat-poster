# ============================================================
# 润致公众号自动发布工具 — NAS Docker 镜像
#   · API 自动发布 + 每日定时（免扫码、无人值守）
#   · 含登录鉴权 / 用户管理 / 操作日志
#   · 含 Node 运行时：卡片排版（md2card 公众号模式）在容器内也能渲染，
#     不会退化成旧版排版（2026-10-06 踩过静默降级的坑）
#
# 构建（在项目根目录）：
#   docker build -f docker/Dockerfile -t poster-api:1.1 .
# ============================================================

# 基础镜像可替换：国内拉不到 Docker Hub 时，用镜像源前缀重建即可（无需改 Docker 配置）
#   sudo $(which docker-compose) build \
#     --build-arg PY_BASE=docker.m.daocloud.io/library/python:3.13-slim \
#     --build-arg NODE_BASE=docker.m.daocloud.io/library/node:22-slim \
#     --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PY_BASE=python:3.13-slim
ARG NODE_BASE=node:22-slim
ARG PIP_INDEX=https://pypi.org/simple

# ---- 阶段 1：只为取 node 二进制（render.js 需要）----
FROM ${NODE_BASE} AS nodebin

# ---- 阶段 2：运行镜像 ----
FROM ${PY_BASE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    PIP_NO_CACHE_DIR=1 \
    WX_NODE=/usr/local/bin/node

# tzdata 必须：每日定时 09:30 依赖容器本地时区(北京时间)，默认 UTC 会错 8 小时
# libstdc++6 是 node 二进制的运行库依赖
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

# node + npm（卡片排版用；npm 仅在依赖缺失时才需要，见下面那步）
COPY --from=nodebin /usr/local/bin/node /usr/local/bin/node
COPY --from=nodebin /usr/local/bin/npm  /usr/local/bin/npm
COPY --from=nodebin /usr/local/lib/node_modules /usr/local/lib/node_modules

# Python 依赖（版本与本机开发环境一致）
# PIP_INDEX 可用 --build-arg 换成国内源（清华/阿里），默认官方 PyPI
ARG PIP_INDEX
COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install -i ${PIP_INDEX} -r /tmp/requirements.txt

# 应用代码（不含 data 历史库/密钥/测试产物，见**项目根**的 .dockerignore ——
# ⚠️ Docker 只读构建上下文根目录下的 .dockerignore，即项目根那份，不是 docker/ 这份）
WORKDIR /srv/poster
COPY app/ /srv/poster/app/

# 卡片排版依赖 jsdom：
#   · 仓库**不包含** node_modules（.gitignore 排除），但如果你本地已经装过并被一起拷进来，就直接复用（离线、最快）
#   · 从 git 克隆的新环境没有它 → 这里自动 npm 安装（默认用国内镜像，大陆网络更稳）
#   NPM_REGISTRY 可用 --build-arg 覆盖
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN cd /srv/poster/app/wxcard \
 && if [ -f node_modules/jsdom/package.json ]; then \
        echo "✓ 复用已存在的 node_modules（含 jsdom）"; \
    else \
        echo "→ 未发现 node_modules，使用 $NPM_REGISTRY 安装前端依赖"; \
        npm install --omit=dev --no-audit --no-fund --registry=$NPM_REGISTRY; \
    fi

# 自检：node 与 jsdom 是否就绪（构建期就能发现问题，而不是等到发布时才降级）
RUN node -e "const {JSDOM}=require('/srv/poster/app/wxcard/node_modules/jsdom');new JSDOM('<p>x</p>');console.log('node + jsdom OK')" \
 && python -c "import fastapi, sqlalchemy; print('python deps OK')"

WORKDIR /srv/poster/app
# 数据卷：SQLite 库 poster.db + 生成正文 + 日志 + 二维码 全部持久化在这里
VOLUME ["/srv/poster/app/data"]
EXPOSE 8765

# 健康检查（/health 为公开接口，不需要登录）
HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=5)"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8765"]
