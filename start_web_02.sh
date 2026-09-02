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
#   AGENTOS_KERNEL_PORT  内核端口（默认 9100）
#   AGENTOS_FRONTEND_PORT 前端端口（默认 6390，避开 container_22404 的 5289/5290/6290）
#
# [监督形态说明 / 剩余项清仓批次 A2] 两套监督者可能并存：
#   1. 本脚本的 kernel_supervisor（G8 生命周期监督者）：只在退出码 75
#      （restart-as-unload：POST /api/v1/system/restart、watcher 的 cdylib
#      集合变更自动重启——A3）时拉起新进程；其它退出码诚实停机（崩溃不做
#      自动掩蔽）。
#   2. 外部会话监督者 .zcode_tmp_kernel_supervisor.sh（仓库根，由前一个
#      ZCode 会话的后台任务持有；写 .kernel_supervisor.log /
#      .vite_supervised.log）：每 5s 轮询 :9100，内核不在线即拉起（另每
#      10 周期拉起 vite :6390）。它会掩蔽诚实停机——若停掉内核后 ~5s 内
#      又自己回来，属主即该循环；处置：kill 其 bash 进程或移除脚本。
# ============================================================

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
KERNEL_DIR="$PROJECT_ROOT/kernel"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
KERNEL_BIN="$KERNEL_DIR/target/release/agentos-kernel"
PORTS_FILE="$PROJECT_ROOT/.ports_02"
PROJECT_ID=$(echo -n "$PROJECT_ROOT" | md5sum | cut -c1-8)
REDIS_CONTAINER="lingxi-redis-02-$PROJECT_ID"
COMPOSE_FILE="$PROJECT_ROOT/docker-compose.yml"

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

    # 使用根 docker-compose.yml 管理 Redis（只拉起 redis 服务）
    if [ -f "$COMPOSE_FILE" ]; then
        echo -e "${YELLOW}[INFO] 使用 docker compose 启动 Redis（$COMPOSE_FILE）...${NC}"
        REDIS_HOST_PORT=$(find_available_port 6690)
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

    REDIS_HOST_PORT=$(find_available_port 6690)
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

KERNEL_PORT=$(find_available_port "${AGENTOS_KERNEL_PORT:-9100}") || {    echo -e "${RED}[ERROR] 无法找到可用的内核端口${NC}"
    exit 1
}
FRONTEND_PORT=$(find_available_port "${AGENTOS_FRONTEND_PORT:-6390}") || {
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
    echo -e "${YELLOW}[1/5] 编译 Rust 内核 (cargo build --release)...${NC}"
    echo -e "${YELLOW}       这可能需要几分钟（首次编译约 4-5 分钟，增量编译约 30 秒）${NC}"
    cd "$KERNEL_DIR"
    if cargo build --release --bin agentos-kernel 2>&1; then
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

# 同源守卫：native cdylib 与内核源码异源编译会让 tool 派发点位 SIGSEGV
# （2026-09-01/08-31 两次实证）——启动前检查并给出重编指引。
echo -e "${YELLOW}[CHECK] 检查 native cdylib 与内核同源性...${NC}"
if ! python "$PROJECT_ROOT/scripts/check_native_artifacts_sync.py"; then
    echo -e "${RED}[ERROR] native cdylib / kernel exe 异源，按上方指引重编后重试${NC}"
    exit 1
fi
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
    pkill -f "$KERNEL_BIN" 2>/dev/null || true
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    rm -f "$PORTS_FILE"
    echo -e "${GREEN}[OK] 已停止${NC}"
    exit 0
}
trap cleanup INT TERM

# ========== 步骤 2: 准备插件 venv（首次运行；已有 .venv 跳过） ==========
# 插件 sidecar 按目录 venv 运行，内核不回退 PATH 裸 python——
# 新克隆不装 venv 则所有 Python 插件不可用（插件端点 502）。
echo -e "${YELLOW}[2/5] 准备 Python 插件 venv...${NC}"
if ! command -v uv >/dev/null 2>&1; then
    echo -e "${RED}[ERROR] 未找到 uv。插件依赖每目录 venv（uv sync）创建。${NC}"
    echo -e "${YELLOW}       安装: https://docs.astral.sh/uv/ 后重跑。${NC}"
    exit 1
fi
VENV_CREATED=0
# 插件目录三种子布局：system|tools/<name>、pipeline/<phase>/<name>、shared/<name> 顶层；
# 已有 .venv 跳过（幂等）
for manifest in \
    "$PROJECT_ROOT"/plugins/shared/*/plugin.json \
    "$PROJECT_ROOT"/plugins/shared/system/*/plugin.json \
    "$PROJECT_ROOT"/plugins/shared/tools/*/plugin.json \
    "$PROJECT_ROOT"/plugins/shared/pipeline/*/*/plugin.json
do
    [ -f "$manifest" ] || continue
    dir=$(dirname "$manifest")
    if [ -f "$dir/pyproject.toml" ] && [ ! -d "$dir/.venv" ]; then
        echo -e "       uv sync: $(basename "$dir")"
        if uv sync --project "$dir" >/dev/null 2>&1; then
            VENV_CREATED=$((VENV_CREATED + 1))
        else
            echo -e "${YELLOW}[WARN] uv sync 失败: $dir（检查 uv.lock/pyproject）${NC}"
        fi
    fi
done
echo -e "${GREEN}  插件 venv 就绪（本次新建 $VENV_CREATED 个；既有 .venv 跳过）${NC}"
echo ""

# ========== 步骤 3: 启动内核 ==========
echo -e "${YELLOW}[3/5] 启动 Rust 内核 (端口 :$KERNEL_PORT)...${NC}"
export AGENTOS_KERNEL_PORT=$KERNEL_PORT
export AGENTOS_KERNEL_HOST=0.0.0.0
export AGENTOS_PLUGINS_DIR="$PROJECT_ROOT/plugins/shared"
export AGENTOS_CONFIG_ROOT="$PROJECT_ROOT/config"
# G8 监督者循环：退出码 75 = restart requested（POST /api/v1/system/restart 或
# watcher 的 cdylib 集合变更自动重启排空后退出），自动拉起新进程；
# 其它退出码不重启（崩溃/启动错误不做自动掩蔽）。
# 注意（A2）：外部会话监督者（.zcode_tmp_kernel_supervisor.sh）可能同时在线，
# 会在任何停机后 ~5s 内拉起内核并掩蔽本循环的诚实停机——见文件头说明。
kernel_supervisor() {
    while true; do
        # 退出码必须经 || 捕获：set -e 下裸命令非零退出会在 rc=$? 执行前终止本子 shell，
        # 导致监督者不 respawn、退出码 75 的重启语义失效。
        rc=0
        "$KERNEL_BIN" || rc=$?
        if [ "$rc" -ne 75 ]; then
            echo "[supervisor] kernel exited (code $rc), supervisor stops."
            return "$rc"
        fi
        echo "[supervisor] G8 restart requested (exit 75) — respawning in 1s..."
        sleep 1
    done
}
kernel_supervisor &
KERNEL_PID=$!

# 等待内核就绪
echo -e "${YELLOW}       等待内核启动...${NC}"
KERNEL_READY=false
for i in $(seq 1 60); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$KERNEL_PORT/health" 2>/dev/null | grep -q "200"; then
        KERNEL_READY=true
        echo -e "${GREEN}[OK] 内核已就绪 (http://localhost:$KERNEL_PORT)${NC}"
        break
    fi
    sleep 1
done

if [ "$KERNEL_READY" = false ]; then
    echo -e "${RED}[ERROR] 内核未能在 60 秒内就绪${NC}"
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
echo "REDIS_HOST_PORT=${REDIS_HOST_PORT:-6690}" >> "$PORTS_FILE"
echo "REDIS_CONTAINER=$REDIS_CONTAINER" >> "$PORTS_FILE"

# ========== 步骤 3: 启动前端 ==========
if [ "$KERNEL_ONLY" = true ]; then
    echo -e "${YELLOW}[SKIP] 仅内核模式（--kernel-only）${NC}"
else
    echo ""
    echo -e "${YELLOW}[4/5] 启动前端 (Vite :$FRONTEND_PORT, 连接内核 :$KERNEL_PORT)...${NC}"

    # 安装前端依赖（如需要）
    # 不能只判断 node_modules 目录是否存在：该目录可能为空或不完整，
    # 此时 npx 会去远程下载 vite 并弹出 "Ok to proceed? (y)" 交互确认，
    # 而脚本是非交互后台运行，无人应答 -> 卡满 30s 超时。
    # 故改为检查 vite 可执行文件是否真正存在。
    if [ ! -x "$FRONTEND_DIR/node_modules/.bin/vite" ]; then
        if [ -d "$FRONTEND_DIR/node_modules" ]; then
            echo -e "${YELLOW}       node_modules 不完整，正在重新安装前端依赖...${NC}"
        else
            echo -e "${YELLOW}       前端依赖未安装，正在安装...${NC}"
        fi
        cd "$FRONTEND_DIR" && npm install || {
            echo -e "${RED}[ERROR] 前端依赖安装失败，无法启动前端${NC}"
            exit 1
        }
        cd "$PROJECT_ROOT"
    fi

    # 启动前端，通过 VITE_API_BASE_URL 指向内核
    cd "$FRONTEND_DIR"
    # --yes: 万一本地仍缺 vite，npx 自动下载而不弹交互确认。
    VITE_API_BASE_URL="http://localhost:$KERNEL_PORT" \
        npx --yes vite --host 0.0.0.0 --port "$FRONTEND_PORT" &
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
    echo -e "${CYAN}  Redis:       localhost:${REDIS_HOST_PORT:-6690} (${REDIS_CONTAINER})${NC}"
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
