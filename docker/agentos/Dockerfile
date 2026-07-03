# =============================================================================
# AgentOS Container Runtime Image
# 用于隔离任务执行的自定义镜像，基于 python:3.11-slim 预装工具链
# =============================================================================

FROM python:3.11-slim

LABEL maintainer="Agent OS Team" \
      description="AgentOS 隔离任务运行时镜像 — 预装常用工具链" \
      version="1.0.0"

# 安装系统级工具
# - 基础工具链: git/curl/wget/build-essential/gcc
# - 进程/网络诊断(system-issue #2): 容器精简到没有 ps/netstat/ss，
#   agent 无法做 unix 诊断。procps(ps/pmap/top)、net-tools(netstat/route)、
#   iproute2(ss/ip) 三个 debian 标准包补齐，体积小(~5MB)。
# - Node.js(system-issue #3): 容器内端到端测试常需 npx playwright，
#   预装 nodejs+npm 让前端 e2e 可在容器内运行。
# - chromium 系统依赖(system-issue #3): playwright install --with-deps 只装 python 侧，
#   浏览器运行的 .so 依赖(libnss3/libatk/libgbm 等)在此一并装掉。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        wget \
        build-essential \
        gcc \
        procps \
        net-tools \
        iproute2 \
        nodejs \
        npm \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpangocairo-1.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# 创建 python -> python3 软链接（确保 python 命令可用）
RUN ln -sf /usr/local/bin/python3 /usr/local/bin/python

# 安装常用 Python 包
# 镜像链回退：阿里云 → 清华 tuna → 官方 PyPI
#
# 包构成：
# 1. 原有基础包：pyyaml/pytest/ruff/pip/jsonschema/pydantic/rich/httpx
# 2. 项目运行时依赖(system-issue #1)：pyproject.toml 声明 + 项目实际 import。
#    agent 在容器里跑项目代码/起 server 时，这些依赖必须就位，否则每次
#    都要 pip install 重装。补：aiohttp/watchdog/litellm/simpleeval/python-lsp-server
#    （pyproject 声明）+ fastapi/uvicorn/starlette（src/channels 大量 import，
#    属隐式运行时依赖）。
# 3. 异步测试：pytest-asyncio（pyproject dev 依赖，agent 跑测试常用）。
# 4. 浏览器自动化(system-issue #3)：playwright。chromium 浏览器本体在下一层装。
ARG PY_MIRROR="https://mirrors.aliyun.com/pypi/simple/"
ARG PY_TRUSTED="mirrors.aliyun.com"
RUN pip install --no-cache-dir \
        -i ${PY_MIRROR} --trusted-host ${PY_TRUSTED} \
        "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.21" "ruff>=0.4.0" "pip>=24.0" \
        "jsonschema>=4.0" "pydantic>=2.0" "rich>=13.0" "httpx>=0.27.0" \
        "aiohttp>=3.9" "watchdog>=3.0" "litellm>=1.0" "simpleeval>=1.0" \
        "python-lsp-server>=1.14.0" \
        "fastapi>=0.110" "uvicorn>=0.27" "starlette>=0.36" \
        "playwright>=1.40" \
    || pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.21" "ruff>=0.4.0" "pip>=24.0" \
        "jsonschema>=4.0" "pydantic>=2.0" "rich>=13.0" "httpx>=0.27.0" \
        "aiohttp>=3.9" "watchdog>=3.0" "litellm>=1.0" "simpleeval>=1.0" \
        "python-lsp-server>=1.14.0" \
        "fastapi>=0.110" "uvicorn>=0.27" "starlette>=0.36" \
        "playwright>=1.40" \
    || pip install --no-cache-dir \
        "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.21" "ruff>=0.4.0" "pip>=24.0" \
        "jsonschema>=4.0" "pydantic>=2.0" "rich>=13.0" "httpx>=0.27.0" \
        "aiohttp>=3.9" "watchdog>=3.0" "litellm>=1.0" "simpleeval>=1.0" \
        "python-lsp-server>=1.14.0" \
        "fastapi>=0.110" "uvicorn>=0.27" "starlette>=0.36" \
        "playwright>=1.40"

# 安装 Playwright 浏览器(system-issue #3)
# 让 agent 在容器内直接跑 e2e 测试（npx playwright / python -m pytest）。
# server + 浏览器同处容器，共用 127.0.0.1，根除「容器内 server 对宿主浏览器不可达」。
# 不带 --with-deps（系统依赖已在 apt 层装掉）。
#
# 下载源说明：
# - playwright 浏览器走官方 cdn.playwright.dev。新版(1.61+)用 Chrome-for-Testing，
#   国内镜像(npmmirror)未同步该结构，故只能用官方源。
# - 官方源国内较慢(~100KB/s, ~177MB)，故加 --force 重试 + 给足超时。
# - 若需走代理/镜像，build 时传 --build-arg PW_HOST=... 覆盖。
# - 失败仅 warning，不阻断镜像构建（断网时仍能得到可用镜像，#1/#2 不受影响，
#   容器内 e2e 暂不可用，可后续 docker exec 单独补装）。
ARG PW_HOST=""
RUN if [ -n "${PW_HOST}" ]; then export PLAYWRIGHT_DOWNLOAD_HOST="${PW_HOST}"; fi \
    && (python -m playwright install chromium \
        || python -m playwright install chromium \
        || echo "[warn] playwright chromium 下载失败，镜像仍可用但容器内 e2e 不可用")

# 安装 Playwright headless shell(system-issue #3 续)
# playwright 1.49+ 默认 headless 模式用 chrome-headless-shell 而非完整 chrome，
# `install chromium` 虽含 shell 但下载中途失败会被上面的 `|| echo` 吞掉，导致
# 镜像里只有完整 chrome、缺 shell——默认 e2e 代码（launch() 不传参）会报
# "Executable doesn't exist at chromium_headless_shell-..."。
# 这里独立装一次 shell，确保即使完整 chrome 那层失败也能补齐 shell；
# --only-shell 避免重复下载完整 chrome。
RUN if [ -n "${PW_HOST}" ]; then export PLAYWRIGHT_DOWNLOAD_HOST="${PW_HOST}"; fi \
    && (python -m playwright install --only-shell chromium \
        || python -m playwright install --only-shell chromium \
        || echo "[warn] playwright chromium-headless-shell 下载失败，默认 headless e2e 将不可用")

# 设置工作目录
WORKDIR /workspace

# 健康检查
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "print('ok')" || exit 1

# 默认命令
CMD ["python"]
