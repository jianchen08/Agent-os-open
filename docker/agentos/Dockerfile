# =============================================================================
# AgentOS Container Runtime Image
# 用于隔离任务执行的自定义镜像，基于 python:3.11-slim 预装工具链
# =============================================================================

FROM python:3.11-slim

LABEL maintainer="Agent OS Team" \
      description="AgentOS 隔离任务运行时镜像 — 预装常用工具链" \
      version="1.0.0"

# 安装系统级工具
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        curl \
        wget \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# 创建 python -> python3 软链接（确保 python 命令可用）
RUN ln -sf /usr/local/bin/python3 /usr/local/bin/python

# 安装常用 Python 包
# 镜像链回退：阿里云 → 清华 tuna → 官方 PyPI
RUN pip install --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        --trusted-host mirrors.aliyun.com \
        "pyyaml>=6.0" \
        "pytest>=8.0" \
        "ruff>=0.4.0" \
        "pip>=24.0" \
        "jsonschema>=4.0" \
        "pydantic>=2.0" \
        "rich>=13.0" \
        "httpx>=0.27.0" \
    || pip install --no-cache-dir \
        -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
        --trusted-host pypi.tuna.tsinghua.edu.cn \
        "pyyaml>=6.0" \
        "pytest>=8.0" \
        "ruff>=0.4.0" \
        "pip>=24.0" \
        "jsonschema>=4.0" \
        "pydantic>=2.0" \
        "rich>=13.0" \
        "httpx>=0.27.0" \
    || pip install --no-cache-dir \
        "pyyaml>=6.0" \
        "pytest>=8.0" \
        "ruff>=0.4.0" \
        "pip>=24.0" \
        "jsonschema>=4.0" \
        "pydantic>=2.0" \
        "rich>=13.0" \
        "httpx>=0.27.0"

# 设置工作目录
WORKDIR /workspace

# 健康检查
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD python -c "print('ok')" || exit 1

# 默认命令
CMD ["python"]
