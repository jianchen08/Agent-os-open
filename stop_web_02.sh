#!/usr/bin/env bash
# Lingxi AgentOS 0.2 停止脚本
# 读取 .ports_02 文件，关闭内核和前端进程
# 支持 PID 验证，防止误杀其他项目的进程

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
PORTS_FILE="$PROJECT_ROOT/.ports_02"
PROJECT_ID=$(echo -n "$PROJECT_ROOT" | md5sum | cut -c1-8)

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  Lingxi AgentOS 0.2 停止脚本${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""
echo "项目目录: $PROJECT_ROOT"
echo "项目标识: $PROJECT_ID"
echo ""

FOUND=0
KERNEL_PORT=""
FRONTEND_PORT=""
REDIS_HOST_PORT=""
REDIS_CONTAINER=""
STORED_KERNEL_PID=""
STORED_FRONTEND_PID=""
STORED_PROJECT_ROOT=""

# ========== 读取 .ports_02 文件 ==========
if [ ! -f "$PORTS_FILE" ]; then
    echo -e "${YELLOW}[INFO] 未找到 .ports_02 文件，本项目没有运行中的 0.2 实例${NC}"
    exit 0
fi

echo -e "${YELLOW}[INFO] 从 .ports_02 文件读取端口信息...${NC}"
while IFS='=' read -r key value; do
    case "$key" in
        OLD_KERNEL_PORT) KERNEL_PORT="$value" ;;
        OLD_FRONTEND_PORT) FRONTEND_PORT="$value" ;;
        REDIS_HOST_PORT) REDIS_HOST_PORT="$value" ;;
        REDIS_CONTAINER) REDIS_CONTAINER="$value" ;;
        PROJECT_ROOT) STORED_PROJECT_ROOT="$value" ;;
        OLD_KERNEL_PID) STORED_KERNEL_PID="$value" ;;
        OLD_FRONTEND_PID) STORED_FRONTEND_PID="$value" ;;
    esac
done < "$PORTS_FILE"

if [ -n "$STORED_PROJECT_ROOT" ] && [ "$PROJECT_ROOT" != "$STORED_PROJECT_ROOT" ]; then
    echo -e "${RED}[WARN] .ports_02 文件属于其他项目目录 [$STORED_PROJECT_ROOT]，拒绝操作${NC}"
    echo -e "${YELLOW}[INFO] 如需强制停止，请手动删除 $PORTS_FILE${NC}"
    exit 1
fi

echo -e "${YELLOW}[INFO] 内核端口: ${KERNEL_PORT:-未设置}${NC}"
echo -e "${YELLOW}[INFO] 前端端口: ${FRONTEND_PORT:-未设置}${NC}"
[ -n "$STORED_KERNEL_PID" ] && echo -e "${YELLOW}[INFO] 内核 PID: $STORED_KERNEL_PID${NC}"
[ -n "$STORED_FRONTEND_PID" ] && echo -e "${YELLOW}[INFO] 前端 PID: $STORED_FRONTEND_PID${NC}"

# ========== 关闭内核进程（带 PID 验证） ==========
if [ -n "$KERNEL_PORT" ] && command -v lsof &>/dev/null 2>&1; then
    PIDS=$(lsof -ti:$KERNEL_PORT 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        if [ -n "$STORED_KERNEL_PID" ]; then
            for pid in $PIDS; do
                if [ "$pid" = "$STORED_KERNEL_PID" ]; then
                    echo -e "${YELLOW}[INFO] 关闭内核进程: $pid (端口 $KERNEL_PORT)${NC}"
                    kill -9 "$pid" 2>/dev/null || true
                    FOUND=1
                else
                    echo -e "${YELLOW}[WARN] 端口 $KERNEL_PORT 上的进程已变更（存储PID=$STORED_KERNEL_PID，当前PID=$pid），跳过关闭以防误杀${NC}"
                fi
            done
        else
            echo -e "${YELLOW}[INFO] 关闭内核进程: $PIDS (端口 $KERNEL_PORT)${NC}"
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            FOUND=1
        fi
    fi
fi

# ========== 关闭前端进程（带 PID 验证） ==========
if [ -n "$FRONTEND_PORT" ] && command -v lsof &>/dev/null 2>&1; then
    PIDS=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
    if [ -n "$PIDS" ]; then
        if [ -n "$STORED_FRONTEND_PID" ]; then
            for pid in $PIDS; do
                if [ "$pid" = "$STORED_FRONTEND_PID" ]; then
                    echo -e "${YELLOW}[INFO] 关闭前端进程: $pid (端口 $FRONTEND_PORT)${NC}"
                    kill -9 "$pid" 2>/dev/null || true
                    FOUND=1
                else
                    echo -e "${YELLOW}[WARN] 端口 $FRONTEND_PORT 上的进程已变更（存储PID=$STORED_FRONTEND_PID，当前PID=$pid），跳过关闭以防误杀${NC}"
                fi
            done
        else
            echo -e "${YELLOW}[INFO] 关闭前端进程: $PIDS (端口 $FRONTEND_PORT)${NC}"
            echo "$PIDS" | xargs kill -9 2>/dev/null || true
            FOUND=1
        fi
    fi
fi

# ========== 关闭 Redis 容器（如有） ==========
if [ -n "$REDIS_CONTAINER" ] && command -v docker &>/dev/null 2>&1; then
    if docker ps -q -f "name=$REDIS_CONTAINER" 2>/dev/null | grep -q .; then
        echo -e "${YELLOW}[INFO] 停止 Redis 容器: $REDIS_CONTAINER${NC}"
        docker stop "$REDIS_CONTAINER" &>/dev/null 2>&1 || true
        FOUND=1
    fi
fi

sleep 1

# ========== 清理 .ports_02 文件 ==========
rm -f "$PORTS_FILE"

# ========== 结果 ==========
echo ""
if [ "$FOUND" -eq 0 ]; then
    echo -e "${YELLOW}[INFO] 没有发现运行中的 Lingxi AgentOS 0.2 服务${NC}"
else
    echo -e "${GREEN}[OK] Lingxi AgentOS 0.2 服务已停止${NC}"
fi
