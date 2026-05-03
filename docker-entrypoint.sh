#!/bin/bash
# =============================================================================
# Agent OS Docker 入口脚本
# 负责等待依赖服务就绪、初始化数据目录、启动主服务
# =============================================================================

set -euo pipefail

# ---- 配置 ----
REDIS_HOST="${REDIS_HOST:-redis}"
REDIS_PORT="${REDIS_PORT:-6379}"
DATA_DIR="${DATA_DIR:-/app/data}"
MAX_WAIT_SECONDS="${MAX_WAIT_SECONDS:-60}"
WAIT_INTERVAL="${WAIT_INTERVAL:-2}"

# ---- 颜色输出 ----
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# =============================================================================
# 函数：等待依赖服务就绪
# =============================================================================
wait_for_redis() {
    local host="${REDIS_HOST}"
    local port="${REDIS_PORT}"
    local elapsed=0

    log_info "等待 Redis 就绪 (${host}:${port})..."

    while [ $elapsed -lt $MAX_WAIT_SECONDS ]; do
        # 使用 nc 或 bash 内置检测端口连通性
        if command -v nc >/dev/null 2>&1; then
            if nc -z "$host" "$port" 2>/dev/null; then
                log_info "Redis 已就绪 (${host}:${port})"
                return 0
            fi
        elif command -v timeout >/dev/null 2>&1; then
            if timeout 1 bash -c "echo > /dev/tcp/${host}/${port}" 2>/dev/null; then
                log_info "Redis 已就绪 (${host}:${port})"
                return 0
            fi
        elif (echo > /dev/tcp/"${host}"/"${port}") 2>/dev/null; then
            log_info "Redis 已就绪 (${host}:${port})"
            return 0
        fi

        log_warn "Redis 未就绪，${WAIT_INTERVAL}s 后重试 (${elapsed}/${MAX_WAIT_SECONDS}s)..."
        sleep $WAIT_INTERVAL
        elapsed=$((elapsed + WAIT_INTERVAL))
    done

    log_error "Redis 在 ${MAX_WAIT_SECONDS}s 内未就绪，终止启动"
    return 1
}

wait_for_service() {
    local host="$1"
    local port="$2"
    local name="$3"
    local elapsed=0

    log_info "等待 ${name} 就绪 (${host}:${port})..."

    while [ $elapsed -lt $MAX_WAIT_SECONDS ]; do
        if command -v nc >/dev/null 2>&1; then
            if nc -z "$host" "$port" 2>/dev/null; then
                log_info "${name} 已就绪 (${host}:${port})"
                return 0
            fi
        elif (echo > /dev/tcp/"${host}"/"${port}") 2>/dev/null; then
            log_info "${name} 已就绪 (${host}:${port})"
            return 0
        fi

        log_warn "${name} 未就绪，${WAIT_INTERVAL}s 后重试 (${elapsed}/${MAX_WAIT_SECONDS}s)..."
        sleep $WAIT_INTERVAL
        elapsed=$((elapsed + WAIT_INTERVAL))
    done

    log_error "${name} 在 ${MAX_WAIT_SECONDS}s 内未就绪"
    return 1
}

# =============================================================================
# 函数：初始化数据目录
# =============================================================================
init_data_dirs() {
    log_info "初始化数据目录..."

    mkdir -p "${DATA_DIR}"
    mkdir -p "${DATA_DIR}/logs"
    mkdir -p "${DATA_DIR}/sessions"
    mkdir -p "${DATA_DIR}/memory"
    mkdir -p "${DATA_DIR}/workspace"

    log_info "数据目录已就绪: ${DATA_DIR}"
}

# =============================================================================
# 函数：打印环境信息
# =============================================================================
print_env_info() {
    log_info "========================================"
    log_info "  Agent OS Docker 容器启动"
    log_info "========================================"
    log_info "环境:      ${APP_ENV:-production}"
    log_info "API 端口:  ${API_PORT:-8000}"
    log_info "Redis:     ${REDIS_HOST}:${REDIS_PORT}"
    log_info "数据目录:  ${DATA_DIR}"
    log_info "日志级别:  ${LOG_LEVEL:-INFO}"
    log_info "========================================"
}

# =============================================================================
# 主流程
# =============================================================================
main() {
    print_env_info

    # 1. 等待依赖服务
    if [ "${SKIP_REDIS_WAIT:-false}" != "true" ]; then
        wait_for_redis || exit 1
    else
        log_warn "跳过 Redis 等待 (SKIP_REDIS_WAIT=true)"
    fi

    # 2. 初始化数据目录
    init_data_dirs

    # 3. 启动主服务
    log_info "启动 Agent OS 服务..."
    exec "$@"
}

main "$@"
