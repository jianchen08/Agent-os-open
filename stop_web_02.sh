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

# ========== 跨端进程工具（S15：停机链双端失效修复） ==========
# 三类失效已收口：
# ① PID 错位：存储的 OLD_*_PID 是启动器/监督者（函数子 shell、npx），
#    监听端口的内核/前端是其后代——PID 严格相等判定永远 miss 且监督者
#    不死会自动重拉内核（G8）。改为「先杀监督者，再按端口找监听进程」，
#    归属校验用命令行含项目根做二次确认，不再依赖 PID 相等。
# ② Git Bash 无 lsof：整个 kill 块静默跳过 = 停机 no-op。补 netstat/taskkill
#    的 Windows 回退路径。
# ③ lsof 缺失不再静默：显式提示走手动处置，不留"看似停了"的假终态。
IS_WINDOWS=0
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;; esac

port_pids() {
    local port="$1"
    if [ "$IS_WINDOWS" = "1" ]; then
        # Git Bash/MSYS 无 lsof：netstat -ano 的 LISTENING 行第 5 列为 PID
        netstat -ano 2>/dev/null | awk -v p=":$port" '$2 ~ p"$" && $0 ~ /LISTENING/ {print $5}' | sort -u
    elif command -v lsof >/dev/null 2>&1; then
        lsof -ti:"$port" 2>/dev/null || true
    fi
}

kill_pid() {
    local pid="$1"
    if [ "$IS_WINDOWS" = "1" ]; then
        # //F 强杀 //T 连子树（supervisor→内核→sidecar 一并终止）
        taskkill //F //T //PID "$pid" >/dev/null 2>&1 || kill -9 "$pid" 2>/dev/null || true
    else
        kill -9 "$pid" 2>/dev/null || true
    fi
}

pid_belongs_to_project() {
    local pid="$1"
    if [ "$IS_WINDOWS" = "1" ]; then
        powershell -NoProfile -Command \
            "(Get-CimInstance Win32_Process -Filter \"ProcessId=$pid\").CommandLine" \
            2>/dev/null | grep -F "$PROJECT_ROOT" >/dev/null 2>&1
    else
        ps -p "$pid" -o args= 2>/dev/null | grep -F "$PROJECT_ROOT" >/dev/null 2>&1
    fi
}

# 关闭一个服务面：先杀存储的启动器/监督者 PID（防自动重拉），再按端口
# 找监听进程补杀；监听进程经归属校验（命令行含项目根）确认后才杀。
stop_service() {
    local label="$1" port="$2" stored_pid="$3"
    local stopped=0

    if [ -n "$stored_pid" ] && kill -0 "$stored_pid" 2>/dev/null; then
        echo -e "${YELLOW}[INFO] 关闭 $label 启动器/监督者: $stored_pid${NC}"
        kill_pid "$stored_pid"
        stopped=1
    fi

    if [ -n "$port" ]; then
        local pids
        pids=$(port_pids "$port")
        for pid in $pids; do
            if pid_belongs_to_project "$pid"; then
                echo -e "${YELLOW}[INFO] 关闭 $label 监听进程: $pid (端口 $port)${NC}"
                kill_pid "$pid"
                stopped=1
            else
                echo -e "${RED}[WARN] 端口 $port 上的进程 $pid 命令行不含本项目路径，跳过关闭以防误杀（如需手动处置请自查）${NC}"
            fi
        done
    fi
    return $((1 - stopped))
}

# ========== 关闭内核进程 ==========
if [ -n "$KERNEL_PORT" ]; then
    if stop_service "内核" "$KERNEL_PORT" "$STORED_KERNEL_PID"; then
        FOUND=1
    elif ! command -v lsof >/dev/null 2>&1 && [ "$IS_WINDOWS" != "1" ]; then
        echo -e "${RED}[WARN] 本机无 lsof 且非 Windows，无法按端口定位 $KERNEL_PORT 监听进程，请手动核查${NC}"
    fi
fi

# ========== 关闭前端进程 ==========
if [ -n "$FRONTEND_PORT" ]; then
    if stop_service "前端" "$FRONTEND_PORT" "$STORED_FRONTEND_PID"; then
        FOUND=1
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
