#!/usr/bin/env bash
# Agent OS Web Channel 停止脚本
# 按项目目录隔离：只关闭当前项目 .ports 文件中记录的端口进程
# 支持 PID 验证，防止误杀其他项目的进程

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PORTS_FILE="$PROJECT_ROOT/.ports"
PROJECT_ID=$(echo -n "$PROJECT_ROOT" | md5sum | cut -c1-8)

echo "========================================"
echo "  Agent OS Web Channel 停止脚本"
echo "========================================"
echo ""
echo "项目目录: $PROJECT_ROOT"
echo "项目标识: $PROJECT_ID"
echo ""

FOUND=0
BACKEND_PORT=""
FRONTEND_PORT=""
STORED_BACKEND_PID=""
STORED_FRONTEND_PID=""
STORED_PROJECT_ROOT=""

# ========== 读取 .ports 文件 ==========
if [ ! -f "$PORTS_FILE" ]; then
    echo "[INFO] 未找到 .ports 文件，本项目没有运行中的实例"
    exit 0
fi

echo "[INFO] 从 .ports 文件读取端口信息..."
while IFS='=' read -r key value; do
    case "$key" in
        BACKEND_PORT) BACKEND_PORT="$value" ;;
        FRONTEND_PORT) FRONTEND_PORT="$value" ;;
        PROJECT_ROOT) STORED_PROJECT_ROOT="$value" ;;
        PROJECT_ID)
            if [ "$value" != "$PROJECT_ID" ]; then
                echo "[WARN] .ports 文件中的项目标识不匹配，可能已被其他项目覆盖"
            fi
            ;;
        BACKEND_PID) STORED_BACKEND_PID="$value" ;;
        FRONTEND_PID) STORED_FRONTEND_PID="$value" ;;
    esac
done < "$PORTS_FILE"

if [ -n "$STORED_PROJECT_ROOT" ] && [ "$PROJECT_ROOT" != "$STORED_PROJECT_ROOT" ]; then
    echo "[WARN] .ports 文件属于其他项目目录 [$STORED_PROJECT_ROOT]，拒绝操作"
    echo "[INFO] 如需强制停止，请手动删除 $PORTS_FILE"
    exit 1
fi

echo "[INFO] 后端端口: ${BACKEND_PORT:-未设置}"
echo "[INFO] 前端端口: ${FRONTEND_PORT:-未设置}"
[ -n "$STORED_BACKEND_PID" ] && echo "[INFO] 后端 PID: $STORED_BACKEND_PID"
[ -n "$STORED_FRONTEND_PID" ] && echo "[INFO] 前端 PID: $STORED_FRONTEND_PID"

# ========== 关闭后端进程（带 PID 验证） ==========
if [ -n "$BACKEND_PORT" ] && command -v lsof &>/dev/null; then
    PIDS=$(lsof -ti:$BACKEND_PORT 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        if [ -n "$STORED_BACKEND_PID" ]; then
            for pid in $PIDS; do
                if [ "$pid" = "$STORED_BACKEND_PID" ]; then
                    echo "[INFO] 关闭后端进程: $pid (端口 $BACKEND_PORT)"
                    kill -9 "$pid" 2>/dev/null || true
                    FOUND=1
                else
                    echo "[WARN] 端口 $BACKEND_PORT 上的进程已变更（存储PID=$STORED_BACKEND_PID，当前PID=$pid），跳过关闭以防误杀"
                fi
            done
        else
            echo "[INFO] 关闭后端进程: $PIDS (端口 $BACKEND_PORT)"
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            FOUND=1
        fi
    fi
fi

# ========== 关闭前端进程（带 PID 验证） ==========
if [ -n "$FRONTEND_PORT" ] && command -v lsof &>/dev/null; then
    PIDS=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        if [ -n "$STORED_FRONTEND_PID" ]; then
            for pid in $PIDS; do
                if [ "$pid" = "$STORED_FRONTEND_PID" ]; then
                    echo "[INFO] 关闭前端进程: $pid (端口 $FRONTEND_PORT)"
                    kill -9 "$pid" 2>/dev/null || true
                    FOUND=1
                else
                    echo "[WARN] 端口 $FRONTEND_PORT 上的进程已变更（存储PID=$STORED_FRONTEND_PID，当前PID=$pid），跳过关闭以防误杀"
                fi
            done
        else
            echo "[INFO] 关闭前端进程: $PIDS (端口 $FRONTEND_PORT)"
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            FOUND=1
        fi
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
