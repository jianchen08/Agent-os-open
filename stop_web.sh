#!/usr/bin/env bash
# Agent OS Web Channel 停止脚本
# 按项目目录隔离：只关闭当前项目 .ports 文件中记录的端口进程

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PORTS_FILE="$PROJECT_ROOT/.ports"

echo "========================================"
echo "  Agent OS Web Channel 停止脚本"
echo "========================================"
echo ""
echo "项目目录: $PROJECT_ROOT"
echo ""

FOUND=0
BACKEND_PORT=""
FRONTEND_PORT=""

# ========== 读取 .ports 文件 ==========
if [ -f "$PORTS_FILE" ]; then
    echo "[INFO] 从 .ports 文件读取端口信息..."
    source "$PORTS_FILE"
    echo "[INFO] 后端端口: ${BACKEND_PORT:-未设置}"
    echo "[INFO] 前端端口: ${FRONTEND_PORT:-未设置}"
else
    echo "[INFO] 未找到 .ports 文件，使用默认端口..."
    BACKEND_PORT=8888
    FRONTEND_PORT=5188
fi

# ========== 关闭后端进程 ==========
if [ -n "$BACKEND_PORT" ]; then
    if command -v lsof &>/dev/null; then
        PIDS=$(lsof -ti:$BACKEND_PORT 2>/dev/null || true)
        if [ -n "$PIDS" ]; then
            echo "[INFO] 关闭后端进程: $PIDS (端口 $BACKEND_PORT)"
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            FOUND=1
        fi
    fi
fi

# ========== 关闭前端进程 ==========
if [ -n "$FRONTEND_PORT" ]; then
    if command -v lsof &>/dev/null; then
        PIDS=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
        if [ -n "$PIDS" ]; then
            echo "[INFO] 关闭前端进程: $PIDS (端口 $FRONTEND_PORT)"
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            FOUND=1
        fi
    fi
fi

# 备用: 通过 fuser 查找
if command -v fuser &>/dev/null; then
    if [ "$FOUND" -eq 0 ]; then
        [ -n "$BACKEND_PORT" ] && fuser -k $BACKEND_PORT/tcp 2>/dev/null && FOUND=1 || true
        [ -n "$FRONTEND_PORT" ] && fuser -k $FRONTEND_PORT/tcp 2>/dev/null && FOUND=1 || true
    fi
fi

sleep 1

# ========== 清理 .ports 文件 ==========
rm -f "$PORTS_FILE"

# ========== 结果 ==========
echo ""
if [ "$FOUND" -eq 0 ]; then
    echo "[INFO] 没有发现运行中的 Agent OS 服务"
else
    echo "[OK] Agent OS 服务已停止"
fi
