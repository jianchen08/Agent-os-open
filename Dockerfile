# =============================================================================
# Agent OS Dockerfile — 多阶段构建
# 阶段 1: builder  — 安装依赖
# 阶段 2: runtime  — 最小化运行时镜像
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1: 构建阶段 — 安装 Python 依赖
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

WORKDIR /build

# 安装系统级构建工具（编译 C 扩展所需）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# 先复制依赖声明文件，利用 Docker 缓存层加速构建
COPY pyproject.toml ./

# 安装项目依赖到独立目录（便于第二阶段精确复制）
RUN pip install --no-cache-dir --prefix=/install \
        --find-links=/install \
        . 2>/dev/null \
    || pip install --no-cache-dir --prefix=/install \
        pyyaml>=6.0 rich>=13.0 aiohttp>=3.9 watchdog>=3.0 \
        litellm>=1.0 pydantic>=2.0 jsonschema>=4.0 \
        uvicorn[standard]>=0.24.0 fastapi>=0.104.0

# ---------------------------------------------------------------------------
# 阶段 2: 运行阶段 — 最小化镜像
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

LABEL maintainer="Agent OS Team" \
      description="Agent OS — 插件化管道架构 AI Agent 平台" \
      version="1.0.0"

# 安装运行时必需的系统工具（nc 用于健康检查等待）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        netcat-openbsd \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# 从 builder 阶段复制已安装的 Python 包
COPY --from=builder /install /usr/local

# 设置工作目录
WORKDIR /app

# 复制项目代码
COPY src/ ./src/
COPY config/ ./config/
COPY conftest.py ./

# 复制启动脚本
COPY app_factory.py ./stream_handler.py ./ws_handler.py ./static_files.py ./
COPY run.py ./
COPY docker-entrypoint.sh ./

# 确保入口脚本可执行
RUN chmod +x docker-entrypoint.sh

# 创建数据目录并设置权限
RUN mkdir -p /app/data/logs /app/data/sessions /app/data/memory /app/data/workspace \
    && chown -R appuser:appuser /app

# 切换到非 root 用户
USER appuser

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    APP_ENV=production \
    LOG_LEVEL=INFO \
    API_PORT=8000 \
    API_HOST=0.0.0.0 \
    DATA_DIR=/app/data \
    REDIS_HOST=redis \
    REDIS_PORT=6379

# 暴露 API 服务端口
EXPOSE 8000

# 数据卷挂载点
VOLUME ["/app/data", "/app/config"]

# 健康检查：每 30s 检查一次 /health 端点
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:${API_PORT}/health || exit 1

# 入口点：先执行初始化脚本，再启动服务
ENTRYPOINT ["./docker-entrypoint.sh"]

# 默认命令：启动 API 服务器
CMD ["python", "app_factory.py"]
