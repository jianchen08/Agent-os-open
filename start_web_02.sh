#!/usr/bin/env bash
# ============================================================
# Lingxi AgentOS 0.2 新架构启动脚本
#
# 流程：编译内核 → 启动内核二进制 → 启动前端（连接新内核）
#
# 用法：
#   ./start_web_02.sh              # 完整启动（编译+内核+前端）
#   ./start_web_02.sh --no-build   # 跳过编译，直接启动
#   ./start_web_02.sh --kernel-only # 仅启动内核
#
# 环境变量：
#   LINGXI_KERNEL_PORT  内核端口（默认 9100）
#   LINGXI_FRONTEND_PORT 前端端口（默认 5290）
# ============================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
KERNEL_DIR="$PROJECT_ROOT/kernel"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
KERNEL_BIN="$KERNEL_DIR/target/release/lingxi-kernel"
PORTS_FILE="$PROJECT_ROOT/.ports_02"
PROJECT_ID=$(echo -n "$PROJECT_ROOT" | md5sum | cut -c1-8)
REDIS_CONTAINER="lingxi-redis-02-$PROJECT_ID"
COMPOSE_FILE="$PROJECT_ROOT/docker/0.2/docker-compose.yml"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  灵汐 AgentOS 0.2 新架构启动脚本${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""
echo "项目目录: $PROJECT_ROOT"
echo "项目标识: $PROJECT_ID"

# 解析参数
NO_BUILD=false
KERNEL_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --no-build)    NO_BUILD=true ;;
        --kernel-only) KERNEL_ONLY=true ;;
    esac
done

# ========== 查找可用端口 ==========
find_available_port() {
    local start_port=$1
    local port=$start_port
    local max_port=$((start_port + 100))
    while [ $port -le $max_port ]; do
        if ! lsof -ti:$port &>/dev/null 2>&1; then
            echo $port
            return 0
        fi
        port=$((port + 1))
    done
    return 1
}

# ========== 确保 Docker 和 Redis 就绪 ==========
ensure_docker_and_redis() {
    if ! command -v docker &>/dev/null; then
        echo -e "${YELLOW}[WARN] 未找到 Docker，跳过 Redis 编排${NC}"
        return 0
    fi

    if ! docker info &>/dev/null 2>&1; then
        echo -e "${YELLOW}[INFO] Docker 未启动，正在尝试启动...${NC}"
        if command -v systemctl &>/dev/null; then
            sudo systemctl start docker 2>/dev/null || true
        elif command -v service &>/dev/null; then
            sudo service docker start 2>/dev/null || true
        fi
        echo -e "${YELLOW}[INFO] 正在等待 Docker 启动...${NC}"
        DOCKER_READY=0
        for i in $(seq 1 40); do
            if docker info &>/dev/null 2>&1; then
                DOCKER_READY=1
                echo -e "${GREEN}[OK] Docker 已启动${NC}"
                break
            fi
            sleep 3
        done
        if [ "$DOCKER_READY" -eq 0 ]; then
            echo -e "${YELLOW}[WARN] Docker 未能在 2 分钟内启动，继续启动（部分功能可能不可用）${NC}"
            return 0
        fi
    fi

    # 使用 docker compose 管理 Redis（对标 docker/0.2/docker-compose.yml）
    if [ -f "$COMPOSE_FILE" ]; then
        echo -e "${YELLOW}[INFO] 使用 docker compose 启动 Redis（$COMPOSE_FILE）...${NC}"
        REDIS_HOST_PORT=$(find_available_port 6481)
        export REDIS_HOST_PORT
        KERNEL_HOST_PORT=$(find_available_port 8090)
        export KERNEL_HOST_PORT
        docker compose -f "$COMPOSE_FILE" up -d redis 2>/dev/null
        if [ $? -eq 0 ]; then
            echo -e "${YELLOW}[INFO] 等待 Redis 就绪...${NC}"
            for i in $(seq 1 20); do
                if docker exec "$(docker compose -f "$COMPOSE_FILE" ps -q redis 2>/dev/null)" redis-cli ping 2>/dev/null | grep -q PONG; then
                    echo -e "${GREEN}[OK] Redis 已就绪${NC}"
                    break
                fi
                sleep 1
            done
        else
            echo -e "${YELLOW}[WARN] docker compose 启动 Redis 失败，尝试独立容器...${NC}"
            _ensure_redis_container
        fi
    else
        echo -e "${YELLOW}[INFO] 未找到 docker-compose.yml，使用独立容器启动 Redis...${NC}"
        _ensure_redis_container
    fi
}

_ensure_redis_container() {
    if docker ps -q -f "name=$REDIS_CONTAINER" 2>/dev/null | grep -q .; then
        echo -e "${GREEN}[OK] Redis 容器 ($REDIS_CONTAINER) 已运行${NC}"
        return 0
    fi

    if docker ps -a -q -f "name=$REDIS_CONTAINER" 2>/dev/null | grep -q .; then
        echo -e "${YELLOW}[INFO] Redis 容器 ($REDIS_CONTAINER) 已存在但未运行，正在启动...${NC}"
        docker start "$REDIS_CONTAINER" &>/dev/null 2>&1
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}[OK] Redis 容器已启动${NC}"
            return 0
        fi
    fi

    REDIS_HOST_PORT=$(find_available_port 6481)
    echo -e "${YELLOW}[INFO] 正在创建 Redis 容器 ($REDIS_CONTAINER, 端口 $REDIS_HOST_PORT)...${NC}"
    docker run -d --name "$REDIS_CONTAINER" --restart unless-stopped \
        -p "$REDIS_HOST_PORT:6379" \
        redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --appendonly yes &>/dev/null 2>&1
    if [ $? -eq 0 ]; then
        echo -e "${YELLOW}[INFO] 等待 Redis 就绪...${NC}"
        for i in $(seq 1 20); do
            if docker exec "$REDIS_CONTAINER" redis-cli ping &>/dev/null 2>&1; then
                echo -e "${GREEN}[OK] Redis 已就绪${NC}"
                return 0
            fi
            sleep 1
        done
        echo -e "${YELLOW}[WARN] Redis 未能在 20 秒内就绪，继续启动${NC}"
    else
        echo -e "${YELLOW}[WARN] Redis 容器启动失败，继续启动（内核将使用内存模式）${NC}"
    fi
}

# ========== 端口分配 ==========
echo -e "${YELLOW}[INFO] 正在查找可用端口...${NC}"

KERNEL_PORT=$(find_available_port "${LINGXI_KERNEL_PORT:-9100}") || {
    echo -e "${RED}[ERROR] 无法找到可用的内核端口${NC}"
    exit 1
}
FRONTEND_PORT=$(find_available_port "${LINGXI_FRONTEND_PORT:-5290}") || {
    echo -e "${RED}[ERROR] 无法找到可用的前端端口${NC}"
    exit 1
}

echo -e "${GREEN}[OK] 内核端口: $KERNEL_PORT${NC}"
echo -e "${GREEN}[OK] 前端端口: $FRONTEND_PORT${NC}"
echo ""

# ========== 检查工具链 ==========
echo -e "${YELLOW}[CHECK] 检查工具链...${NC}"

if ! command -v cargo &>/dev/null; then
    echo -e "${RED}[ERROR] 未找到 cargo，请先安装 Rust 工具链${NC}"
    exit 1
fi
echo -e "${GREEN}  cargo: $(cargo --version)${NC}"

if [ "$KERNEL_ONLY" = false ]; then
    if ! command -v node &>/dev/null; then
        echo -e "${RED}[ERROR] 未找到 Node.js，请先安装 Node.js${NC}"
        exit 1
    fi
    echo -e "${GREEN}  node: $(node --version)${NC}"
fi
echo ""

# ========== 确保 Docker/Redis 就绪 ==========
ensure_docker_and_redis
echo ""

# ========== 步骤 1: 编译内核 ==========
if [ "$NO_BUILD" = true ]; then
    echo -e "${YELLOW}[SKIP] 跳过内核编译（--no-build）${NC}"
else
    echo -e "${YELLOW}[1/4] 编译 Rust 内核 (cargo build --release)...${NC}"
    echo -e "${YELLOW}       这可能需要几分钟（首次编译约 4-5 分钟，增量编译约 30 秒）${NC}"
    cd "$KERNEL_DIR"
    if cargo build --release --bin lingxi-kernel 2>&1; then
        echo -e "${GREEN}[OK] 内核编译成功${NC}"
    else
        echo -e "${RED}[ERROR] 内核编译失败${NC}"
        exit 1
    fi
    cd "$PROJECT_ROOT"
fi

# 验证二进制存在
if [ ! -f "$KERNEL_BIN" ]; then
    echo -e "${RED}[ERROR] 内核二进制不存在: $KERNEL_BIN${NC}"
    echo -e "${YELLOW}       请去掉 --no-build 参数重新运行${NC}"
    exit 1
fi
echo -e "${GREEN}  内核二进制: $KERNEL_BIN${NC}"
echo ""

# ========== 清理旧实例 ==========
cleanup_old() {
    if [ -f "$PORTS_FILE" ]; then
        source "$PORTS_FILE" 2>/dev/null
        if [ -n "${OLD_KERNEL_PID:-}" ]; then
            kill "$OLD_KERNEL_PID" 2>/dev/null && \
                echo -e "${YELLOW}[CLEAN] 已关闭旧内核进程: $OLD_KERNEL_PID${NC}" || true
        fi
        if [ -n "${OLD_FRONTEND_PID:-}" ]; then
            kill "$OLD_FRONTEND_PID" 2>/dev/null && \
                echo -e "${YELLOW}[CLEAN] 已关闭旧前端进程: $OLD_FRONTEND_PID${NC}" || true
        fi
        rm -f "$PORTS_FILE"
        sleep 1
    fi
}
cleanup_old

# ========== 清理函数 ==========
KERNEL_PID=""
FRONTEND_PID=""

cleanup() {
    echo ""
    echo -e "${YELLOW}[STOP] 正在停止所有服务...${NC}"
    [ -n "$KERNEL_PID" ] && kill "$KERNEL_PID" 2>/dev/null || true
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    rm -f "$PORTS_FILE"
    echo -e "${GREEN}[OK] 已停止${NC}"
    exit 0
}
trap cleanup INT TERM

# ========== 步骤 2: 启动内核 ==========
echo -e "${YELLOW}[2/4] 启动 Rust 内核 (端口 :$KERNEL_PORT)...${NC}"
export LINGXI_KERNEL_PORT=$KERNEL_PORT
export LINGXI_KERNEL_HOST=0.0.0.0
"$KERNEL_BIN" &
KERNEL_PID=$!

# 等待内核就绪
echo -e "${YELLOW}       等待内核启动...${NC}"
KERNEL_READY=false
for i in $(seq 1 15); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$KERNEL_PORT/health" 2>/dev/null | grep -q "200"; then
        KERNEL_READY=true
        echo -e "${GREEN}[OK] 内核已就绪 (http://localhost:$KERNEL_PORT)${NC}"
        break
    fi
    sleep 1
done

if [ "$KERNEL_READY" = false ]; then
    echo -e "${RED}[ERROR] 内核未能在 15 秒内就绪${NC}"
    kill "$KERNEL_PID" 2>/dev/null || true
    exit 1
fi

# 验证健康检查响应
HEALTH_RESPONSE=$(curl -s "http://localhost:$KERNEL_PORT/health" 2>/dev/null)
echo -e "${GREEN}       Health: $HEALTH_RESPONSE${NC}"

# 保存端口信息
echo "OLD_KERNEL_PID=$KERNEL_PID" > "$PORTS_FILE"
echo "OLD_KERNEL_PORT=$KERNEL_PORT" >> "$PORTS_FILE"
echo "OLD_FRONTEND_PORT=$FRONTEND_PORT" >> "$PORTS_FILE"
echo "REDIS_HOST_PORT=${REDIS_HOST_PORT:-6481}" >> "$PORTS_FILE"
echo "REDIS_CONTAINER=$REDIS_CONTAINER" >> "$PORTS_FILE"

# ========== 步骤 3: 启动前端 ==========
if [ "$KERNEL_ONLY" = true ]; then
    echo -e "${YELLOW}[SKIP] 仅内核模式（--kernel-only）${NC}"
else
    echo ""
    echo -e "${YELLOW}[3/4] 启动前端 (Vite :$FRONTEND_PORT, 连接内核 :$KERNEL_PORT)...${NC}"

    # 安装前端依赖（如需要）
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        echo -e "${YELLOW}       前端依赖未安装，正在安装...${NC}"
        cd "$FRONTEND_DIR" && npm install && cd "$PROJECT_ROOT"
    fi

    # 启动前端，通过 VITE_API_BASE_URL 指向内核
    cd "$FRONTEND_DIR"
    VITE_API_BASE_URL="http://localhost:$KERNEL_PORT" \
        npx vite --host 0.0.0.0 --port "$FRONTEND_PORT" &
    FRONTEND_PID=$!
    cd "$PROJECT_ROOT"

    # 等待前端就绪
    echo -e "${YELLOW}       等待前端启动...${NC}"
    FRONTEND_READY=false
    for i in $(seq 1 30); do
        if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$FRONTEND_PORT" 2>/dev/null | grep -q "200"; then
            FRONTEND_READY=true
            echo -e "${GREEN}[OK] 前端已就绪 (http://localhost:$FRONTEND_PORT)${NC}"
            break
        fi
        sleep 1
    done

    if [ "$FRONTEND_READY" = false ]; then
        echo -e "${YELLOW}[WARN] 前端未在 30 秒内就绪，但服务可能仍在启动中${NC}"
    fi

    echo "OLD_FRONTEND_PID=$FRONTEND_PID" >> "$PORTS_FILE"
fi

# ========== 输出信息 ==========
echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  服务已启动:${NC}"
echo -e "${CYAN}  内核 (Rust):  http://localhost:$KERNEL_PORT${NC}"
echo -e "${CYAN}  健康检查:     http://localhost:$KERNEL_PORT/health${NC}"
echo -e "${CYAN}  Schema API:  http://localhost:$KERNEL_PORT/api/v1/schema${NC}"
echo -e "${CYAN}  WebSocket:   ws://localhost:$KERNEL_PORT/ws${NC}"
if [ "$KERNEL_ONLY" = false ]; then
    echo -e "${CYAN}  前端:        http://localhost:$FRONTEND_PORT${NC}"
fi
echo -e "${CYAN}  Redis:       localhost:${REDIS_HOST_PORT:-6481} (${REDIS_CONTAINER})${NC}"
echo -e "${CYAN}${NC}"
echo -e "${CYAN}  内核 PID:    $KERNEL_PID${NC}"
[ -n "$FRONTEND_PID" ] && echo -e "${CYAN}  前端 PID:    $FRONTEND_PID${NC}"
echo -e "${CYAN}  端口文件:    $PORTS_FILE${NC}"
echo -e "${CYAN}  按 Ctrl+C 停止所有服务${NC}"
echo -e "${CYAN}========================================${NC}"

# 验证各端点
echo ""
echo -e "${YELLOW}[VERIFY] 端点验证:${NC}"
for endpoint in "/health" "/api/v1/schema" "/api/v1/agents" "/api/v1/pipelines" "/api/v1/tools"; do
    CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$KERNEL_PORT$endpoint" 2>/dev/null)
    if [ "$CODE" = "200" ]; then
        echo -e "  ${GREEN}✅ GET $endpoint → $CODE${NC}"
    else
        echo -e "  ${RED}❌ GET $endpoint → $CODE${NC}"
    fi
done

# POST chat 验证
CHAT_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "http://localhost:$KERNEL_PORT/api/v1/chat" \
    -H "Content-Type: application/json" -d '{"message":"test","session_id":"verify"}' 2>/dev/null)
if [ "$CHAT_CODE" = "200" ]; then
    echo -e "  ${GREEN}✅ POST /api/v1/chat → $CHAT_CODE${NC}"
else
    echo -e "  ${RED}❌ POST /api/v1/chat → $CHAT_CODE${NC}"
fi

echo ""
echo -e "${GREEN}[OK] 所有验证完成${NC}"

wait
