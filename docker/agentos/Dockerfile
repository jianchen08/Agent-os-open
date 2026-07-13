# syntax=docker/dockerfile:1
# BuildKit 前端：--mount=type=cache 需要此指令 + DOCKER_BUILDKIT=1 才生效。
# 构建时务必带 DOCKER_BUILDKIT=1（install.sh 与 docker_provider._ensure_image 已带）。
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
# apt 缓存挂载：复用宿主已下载的 .deb，二次构建跳过下载（需 BuildKit）。
# 注意：不再 rm -rf /var/lib/apt/lists/*，否则会清空 cache 挂载反而失效；
# 改用禁用 docker-clean 配置，让 apt 复用缓存的索引和包。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
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
        fonts-liberation

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
# 注意：pip 层故意不加 --mount=type=cache。实测 BuildKit 的 cache 挂载到
# /root/.cache/pip 会导致 pip 26.x 在 python:3.11-slim 里 Segmentation fault
# （解析依赖阶段崩溃，16s 即 exit 139）。去掉 cache mount 后正常构建成功。
# 缓存增益主要来自 apt 层（体积大、下载慢），pip 层牺牲缓存换稳定性。
#
# 不把 "pip>=24.0" 放进 install 列表。pip 24.0 自带的 packaging 库有 bug
# （_parse_letter_version 在解析某些版本号时变 None → TypeError: 'NoneType' object
# is not callable），在解析"自身要不要升级"时直接崩溃，三个镜像源全失败。
# 改为：先单独 python -m pip install --upgrade pip 升级到最新（修复 bug），
# 再装其余依赖。
RUN \
    (python -m pip install --upgrade pip -i ${PY_MIRROR} --trusted-host ${PY_TRUSTED} \
        || python -m pip install --upgrade pip \
        -i https://pypi.tuna.tsinghua.edu.cn/simple/ --trusted-host pypi.tuna.tsinghua.edu.cn \
        || python -m pip install --upgrade pip) \
    && pip install --no-cache-dir \
        -i ${PY_MIRROR} --trusted-host ${PY_TRUSTED} \
        "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.21" "ruff>=0.4.0" \
        "jsonschema>=4.0" "pydantic>=2.0" "rich>=13.0" "httpx>=0.27.0" \
        "aiohttp>=3.9" "watchdog>=3.0" "litellm>=1.0" "simpleeval>=1.0" \
        "python-lsp-server>=1.14.0" \
        "fastapi>=0.110" "uvicorn>=0.27" "starlette>=0.36" \
        "playwright>=1.40" "pandas>=2.0" \
    || pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.21" "ruff>=0.4.0" \
        "jsonschema>=4.0" "pydantic>=2.0" "rich>=13.0" "httpx>=0.27.0" \
        "aiohttp>=3.9" "watchdog>=3.0" "litellm>=1.0" "simpleeval>=1.0" \
        "python-lsp-server>=1.14.0" \
        "fastapi>=0.110" "uvicorn>=0.27" "starlette>=0.36" \
        "playwright>=1.40" "pandas>=2.0" \
    || pip install --no-cache-dir \
        "pyyaml>=6.0" "pytest>=8.0" "pytest-asyncio>=0.21" "ruff>=0.4.0" \
        "jsonschema>=4.0" "pydantic>=2.0" "rich>=13.0" "httpx>=0.27.0" \
        "aiohttp>=3.9" "watchdog>=3.0" "litellm>=1.0" "simpleeval>=1.0" \
        "python-lsp-server>=1.14.0" \
        "fastapi>=0.110" "uvicorn>=0.27" "starlette>=0.36" \
        "playwright>=1.40" "pandas>=2.0"

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
# playwright 缓存挂载：复用宿主已下载的浏览器 zip，二次构建跳过下载（需 BuildKit）。
# 保持原有 || echo 容错：下载失败仅 warning，不阻断镜像构建。
RUN --mount=type=cache,target=/root/.cache/ms-playwright \
    if [ -n "${PW_HOST}" ]; then export PLAYWRIGHT_DOWNLOAD_HOST="${PW_HOST}"; fi \
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
RUN --mount=type=cache,target=/root/.cache/ms-playwright \
    if [ -n "${PW_HOST}" ]; then export PLAYWRIGHT_DOWNLOAD_HOST="${PW_HOST}"; fi \
    && (python -m playwright install --only-shell chromium \
        || python -m playwright install --only-shell chromium \
        || echo "[warn] playwright chromium-headless-shell 下载失败，默认 headless e2e 将不可用")

# Rust 工具链（≥1.85，jsonschema-rs MSRV 要求）
# 0.2 内核用 Rust 编写，隔离子任务（cargo build/check/clippy/fmt）必须可用。
# - 先补装 pkg-config（apt 层漏装，Rust 编译时链接系统库如 openssl 需要它）
# - 用 rustup 官方脚本安装，RUST_VERSION 锁定 1.85.0
# - --profile minimal 只装 rustc+cargo，再单独 add rustfmt+clippy
# - RUSTUP_HOME / CARGO_HOME 放 /usr/local 下，对所有用户可见
ARG RUST_VERSION=1.85.0
ENV RUSTUP_HOME=/usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:${PATH}
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        pkg-config \
    && curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
        --default-toolchain ${RUST_VERSION} \
        --profile minimal \
    && rustup component add rustfmt clippy \
    && rustc --version \
    && cargo --version

# 设置工作目录
WORKDIR /workspace

# 健康检查
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "print('ok')" || exit 1

# 默认命令
CMD ["python"]
