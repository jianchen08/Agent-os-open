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

# 安装 Playwright Chromium 浏览器(system-issue #3)
# 让 agent 在容器内直接跑 e2e 测试（npx playwright / python -m pytest）。
# server + 浏览器同处容器，共用 127.0.0.1，根除「容器内 server 对宿主浏览器不可达」。
# 系统依赖已在 apt 层装掉（libnss3/libatk/libgbm 等），无需 --with-deps。
#
# ================== 为什么不用 `playwright install` ==================
# playwright 1.61+ 改用 Chrome-for-Testing，下载 URL 会 307 重定向到
# storage.googleapis.com（chrome-for-testing-public bucket）。国内访问 googleapis
# 被限速到 ~60KB/s（177MB 要近 1 小时），实测 20 秒仅下载 0.6% 即超时 → install 卡死。
# PLAYWRIGHT_DOWNLOAD_HOST 环境变量对 1.61 的 cft 路径【无效】：它只改 host 不改路径，
# 而 npmmirror 的 cft 结构与官方不同（npmmirror 未按 playwright 旧版 builds/ 结构同步），
# 指过去全是 404。故彻底放弃 playwright install，改为直接从 npmmirror 下载 CFT zip。
#
# ================== 验证过的可用镜像源 ==================
# npmmirror 镜像了 Google chrome-for-testing-public，路径：
#   https://cdn.npmmirror.com/binaries/chrome-for-testing/<ver>/linux64/chrome-linux64.zip
#   https://cdn.npmmirror.com/binaries/chrome-for-testing/<ver>/linux64/chrome-headless-shell-linux64.zip
# 实测速度 ~24MB/s（177MB 仅需 7.5s），完整文件齐全。CFT_MIRROR ARG 允许覆盖。
#
# ================== 目录结构（playwright 1.61 期望）==================
# 由 driver/package/browsers.json (revision 1228) + EXECUTABLE_PATHS 决定：
#   $PLAYWRIGHT_BROWSERS_PATH/chromium-1228/chrome-linux64/chrome
#   $PLAYWRIGHT_BROWSERS_PATH/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell
# revision/browserVersion 从已安装的 playwright 的 browsers.json 动态读取，
# 避免硬编码版本号导致 playwright 升级后目录名失配。
ARG PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# ENV 硬编码而非 ARG 展开（${PLAYWRIGHT_BROWSERS_PATH}）：早期镜像曾出现运行容器里
# $PLAYWRIGHT_BROWSERS_PATH 为空串（疑似旧构建或 ARG/ENV 展开坑）。硬编码消除歧义。
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# CFT 镜像源。官方源 https://storage.googleapis.com/chrome-for-testing-public 国内不可用。
# 需换源时 build 传 --build-arg CFT_MIRROR=...。
ARG CFT_MIRROR=https://cdn.npmmirror.com/binaries/chrome-for-testing

# 下载 + 解压 Chromium 完整浏览器与 headless shell。
# 关键原则（吸取历史教训）：
# 1) 失败必 fail —— 不用 || echo 兜底。下载/解压/校验任一失败 → RUN 失败 →
#    docker build 非 0 退出 → _ensure_image raise → 熔断器接管，问题当场暴露，
#    绝不再出现"镜像成功但浏览器为空"。
# 2) 版本号动态读取 —— 从 browsers.json 取 chromium 的 revision 与 browserVersion，
#    playwright 升级后本层自动适配，无需改 Dockerfile。
# 3) BuildKit cache mount 复用已下载 zip，二次构建跳过网络下载。
# 注：镜像内无 unzip，用 python zipfile 解压（python 在 pip 层已装）。
RUN --mount=type=cache,target=/tmp/pw-downloads,sharing=locked \
    set -eu \
    && mkdir -p "${PLAYWRIGHT_BROWSERS_PATH}" /tmp/pw-downloads \
    && BJ=/usr/local/lib/python3.11/site-packages/playwright/driver/package/browsers.json \
    && REV=$(python -c "import json;print(next(b['revision'] for b in json.load(open('$BJ'))['browsers'] if b['name']=='chromium'))") \
    && VER=$(python -c "import json;print(next(b['browserVersion'] for b in json.load(open('$BJ'))['browsers'] if b['name']=='chromium'))") \
    && echo "[pw] chromium revision=$REV browserVersion=$VER" \
    && CHROME_ZIP=/tmp/pw-downloads/chrome-${VER}.zip \
    && SHELL_ZIP=/tmp/pw-downloads/shell-${VER}.zip \
    && curl -fsSL -o "${CHROME_ZIP}" "${CFT_MIRROR}/${VER}/linux64/chrome-linux64.zip" \
    && curl -fsSL -o "${SHELL_ZIP}" "${CFT_MIRROR}/${VER}/linux64/chrome-headless-shell-linux64.zip" \
    && mkdir -p "${PLAYWRIGHT_BROWSERS_PATH}/chromium-${REV}" \
    && python -c "import zipfile;zipfile.ZipFile('${CHROME_ZIP}').extractall('${PLAYWRIGHT_BROWSERS_PATH}/chromium-${REV}')" \
    && mkdir -p "${PLAYWRIGHT_BROWSERS_PATH}/chromium_headless_shell-${REV}" \
    && python -c "import zipfile;zipfile.ZipFile('${SHELL_ZIP}').extractall('${PLAYWRIGHT_BROWSERS_PATH}/chromium_headless_shell-${REV}')" \
    && chmod +x "${PLAYWRIGHT_BROWSERS_PATH}/chromium-${REV}/chrome-linux64/chrome" \
              "${PLAYWRIGHT_BROWSERS_PATH}/chromium_headless_shell-${REV}/chrome-headless-shell-linux64/chrome-headless-shell" \
    && echo "[info] installed browsers:" && ls -1 "${PLAYWRIGHT_BROWSERS_PATH}"

# 下载校验断言（双保险）：两个浏览器可执行文件必须真实存在才放行。
# 彻底杜绝"镜像成功但浏览器为空"。test -x 要求文件存在且可执行。
# revision 同样动态读取，与上一层保持一致，playwright 升级后不会误判。
RUN REV=$(python -c "import json;print(next(b['revision'] for b in json.load(open('/usr/local/lib/python3.11/site-packages/playwright/driver/package/browsers.json'))['browsers'] if b['name']=='chromium'))") \
    && test -x "${PLAYWRIGHT_BROWSERS_PATH}/chromium-${REV}/chrome-linux64/chrome" \
    && test -x "${PLAYWRIGHT_BROWSERS_PATH}/chromium_headless_shell-${REV}/chrome-headless-shell-linux64/chrome-headless-shell" \
    && echo "[ok] playwright chromium browsers present at ${PLAYWRIGHT_BROWSERS_PATH} (revision ${REV})"

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
# 间隔放宽到 180s（原 60s）：每次 healthcheck 都是一次 docker exec = runc exec 短命
# 进程，runc 1.3.x 在 WSL2 + cgroup v2 下对此有已知 race（短命进程卡死/状态脱节）。
# 60s 间隔会把 race 概率最大化。真正的容器活性探测由 IsolationManager._find_existing
# _container_sync 的 exec_run(["true"]) 探针承担，healthcheck 仅作 docker 元数据标记，
# 代码不依赖它触发任何逻辑，故放宽安全。见 moby#51845 / runc 1.3.x CHANGELOG。
HEALTHCHECK --interval=180s --timeout=5s --retries=3 \
    CMD python -c "print('ok')" || exit 1

# 默认命令
CMD ["python"]
