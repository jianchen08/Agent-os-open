#!/usr/bin/env bash
# Agent OS Web Channel 停止脚本
# 关闭后端 FastAPI 服务器和前端 Vite 开发服务器

set -e

BACKEND_PORT=8888
FRONTEND_PORT=5188

echo "========================================"
echo "  Agent OS Web Channel 停止脚本"
echo "========================================"
echo ""

FOUND=0

# ========== 按端口关闭 ==========
echo "[INFO] 查找 Agent OS 服务进程..."

# 关闭后端进程
if command -v lsof &>/dev/null; then
    PIDS=$(lsof -ti:$BACKEND_PORT 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "[INFO] 关闭后端进程: $PIDS (端口 $BACKEND_PORT)"
        echo "$PIDS" | xargs kill -9 2>/dev/null || true
        FOUND=1
    fi
fi

# 关闭前端进程
if command -v lsof &>/dev/null; then
    PIDS=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        echo "[INFO] 关闭前端进程: $PIDS (端口 $FRONTEND_PORT)"
        echo "$PIDS" | xargs kill -9 2>/dev/null || true
        FOUND=1
    fi
fi

# 备用: 通过 fuser 查找
if command -v fuser &>/dev/null; then
    if [ "$FOUND" -eq 0 ]; then
        fuser -k $BACKEND_PORT/tcp 2>/dev/null && FOUND=1 || true
        fuser -k $FRONTEND_PORT/tcp 2>/dev/null && FOUND=1 || true
    fi
fi

sleep 1

# ========== 结果 ==========
echo ""
if [ "$FOUND" -eq 0 ]; then
    echo "[INFO] 没有发现运行中的 Agent OS 服务"
else
    echo "[OK] Agent OS 服务已停止"
fi
