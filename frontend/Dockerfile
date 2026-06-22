# =============================================================================
# Agent OS Frontend — 多阶段构建
# 阶段 1: builder — 优先用宿主机构建的 dist/，否则容器内 npm ci + vite build
# 阶段 2: runtime — python:3.11-slim 托管 dist/（FastAPI + uvicorn 静态服务器）
#
# 构建策略（两种路径，自动选择）：
#   路径 A（推荐）：宿主机执行 `npm install && npm run build` 产出 frontend/dist/，
#                  Dockerfile 直接 COPY 它。避免容器内 node 在 WSL2 虚拟化 CPU 上的
#                  V8 SIGILL 问题（node:20-alpine 跑 npm ci 会 Illegal instruction）。
#   路径 B（回退）：宿主机无 dist 时，容器内 npm ci + vite build（需 CPU 支持 V8 指令）。
#
# 构建上下文 = 仓库根目录（compose 里 context: .，见 docker-compose.yml），
# 以便 COPY 根目录下的 packages/ 离线包和 frontend/dist（若已构建）。
#
# pip 依赖安装策略（两层兜底）：
#   1) 本地离线包优先（packages/wheels 非空时）
#   2) 远程多镜像链回退（阿里云 → 清华 → 官方）
#
# 注意：用纯 uvicorn（不带 [standard]），避免安装 uvloop / httptools 等 C 扩展。
# 这些 C 扩展在 WSL2 虚拟化 CPU 上会触发 SIGSEGV/SIGILL（退出码 139/132），
# 导致前端容器反复崩溃重启。纯 Python uvicorn + asyncio 事件循环更稳。
# =============================================================================

# ---------------------------------------------------------------------------
# 阶段 1: builder（优先宿主机 dist，否则容器内构建）
# ---------------------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /build

# 先 COPY 整个 frontend（含可能已构建的 dist/）
COPY frontend/ ./

# 路径 A: 宿主机已构建 dist/ → 直接保留，跳过容器内 npm（避免 V8 SIGILL）
# 路径 B: 无 dist/ → 容器内 npm ci + vite build（需要 CPU 支持 V8 指令集）
RUN if [ -d /build/dist ] && [ -f /build/dist/index.html ]; then \
        echo "[INFO] 检测到宿主机构建的 dist/，直接使用（跳过容器内 npm ci）"; \
        ls /build/dist | head -5; \
    else \
        echo "[INFO] 无宿主机 dist/，容器内构建前端..."; \
        npm ci --registry=https://registry.npmmirror.com \
        || npm install --registry=https://registry.npmmirror.com \
        || npm ci \
        || npm install; \
        npm run build; \
    fi

# ---------------------------------------------------------------------------
# 阶段 2: Python 运行时（保持原有静态文件服务器逻辑）
# ---------------------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

# 复制离线 Python wheels（若 packages/wheels/ 非空则优先离线安装）
COPY packages/wheels/ /opt/wheels/

# 安装 uvicorn + fastapi + httpx + websockets（轻量，无需编译）
# 离线 wheels 优先 → 阿里云 PyPI → 清华 tuna → 官方 PyPI，逐级回退
# 解决 Docker 内 SSL 连接 pypi.org 失败问题
RUN if [ "$(ls /opt/wheels/*.whl 2>/dev/null)" ]; then \
        echo "[INFO] 使用本地离线 wheels"; \
        pip install --no-cache-dir --no-index --find-links=/opt/wheels/ \
            uvicorn==0.38.0 \
            fastapi==0.121.3 \
            httpx==0.28.1 \
            websockets==16.0; \
    else \
        echo "[INFO] 无离线包，使用远程镜像（阿里云 → 清华 → 官方）"; \
        pip install --no-cache-dir \
            -i https://mirrors.aliyun.com/pypi/simple/ \
            --trusted-host mirrors.aliyun.com \
            uvicorn==0.38.0 \
            fastapi==0.121.3 \
            httpx==0.28.1 \
            websockets==16.0 \
        || pip install --no-cache-dir \
            -i https://pypi.tuna.tsinghua.edu.cn/simple/ \
            --trusted-host pypi.tuna.tsinghua.edu.cn \
            uvicorn==0.38.0 \
            fastapi==0.121.3 \
            httpx==0.28.1 \
            websockets==16.0 \
        || pip install --no-cache-dir \
            uvicorn==0.38.0 \
            fastapi==0.121.3 \
            httpx==0.28.1 \
            websockets==16.0; \
    fi

# 从 builder 阶段复制前端构建产物
COPY --from=builder /build/dist/ /app/dist/

# 复制静态文件服务脚本（构建上下文为根目录，server.py 在 frontend/ 下）
COPY frontend/server.py /app/server.py

# 暴露端口
EXPOSE 5188

# 启动服务
CMD ["python", "server.py"]
