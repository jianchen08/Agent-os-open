#!/usr/bin/env bash
# Agent OS Web Channel 启动脚本
# 同时启动后端 FastAPI 服务器和前端 Vite 开发服务器
# 支持多实例隔离：按项目目录区分，端口冲突时自动切换

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
PORTS_FILE="$PROJECT_ROOT/.ports"

echo "========================================"
echo "  Agent OS Web Channel 启动脚本"
echo "========================================"
echo ""
echo "项目目录: $PROJECT_ROOT"

# 检查 Python
if ! command -v python &>/dev/null; then
    echo "[ERROR] 未找到 Python，请先安装 Python"
    exit 1
fi

# 检查 Node
if ! command -v node &>/dev/null; then
    echo "[ERROR] 未找到 Node.js，请先安装 Node.js"
    exit 1
fi

# ========== 确保 Docker 和 Redis 就绪 ==========
ensure_docker_and_redis() {
    if ! command -v docker &>/dev/null; then
        echo "[WARN] 未找到 Docker，跳过 Docker/Redis 检查"
        return 0
    fi

    if ! docker info &>/dev/null; then
        echo "[INFO] Docker 未启动，正在尝试启动..."
        if command -v systemctl &>/dev/null; then
            sudo systemctl start docker 2>/dev/null || true
        elif command -v service &>/dev/null; then
            sudo service docker start 2>/dev/null || true
        elif command -v open &>/dev/null; then
            open -a Docker 2>/dev/null || true
        fi
        echo "[INFO] 正在等待 Docker 启动..."
        DOCKER_READY=0
        for i in $(seq 1 40); do
            if docker info &>/dev/null; then
                DOCKER_READY=1
                echo "[OK] Docker 已启动"
                break
            fi
            sleep 3
        done
        if [ "$DOCKER_READY" -eq 0 ]; then
            echo "[WARN] Docker 未能在 2 分钟内启动，继续启动（部分功能可能不可用）"
            return 0
        fi
    fi

    if docker ps -q -f "name=agent-os-redis" | grep -q .; then
        echo "[OK] Redis 容器已运行"
        return 0
    fi

    if docker ps -a -q -f "name=agent-os-redis" | grep -q .; then
        echo "[INFO] Redis 容器已存在但未运行，正在启动..."
        docker start agent-os-redis &>/dev/null
        if [ $? -eq 0 ]; then
            echo "[OK] Redis 容器已启动"
            return 0
        fi
    fi

    if [ ! -f "$PROJECT_ROOT/docker-compose.yml" ]; then
        echo "[WARN] 未找到 docker-compose.yml，跳过 Redis 启动"
        return 0
    fi

    echo "[INFO] 正在通过 docker compose 启动 Redis..."
    docker compose -f "$PROJECT_ROOT/docker-compose.yml" up -d redis 2>/dev/null
    if [ $? -eq 0 ]; then
        echo "[INFO] 等待 Redis 就绪..."
        for i in $(seq 1 20); do
            if docker exec agent-os-redis redis-cli ping &>/dev/null; then
                echo "[OK] Redis 已就绪"
                return 0
            fi
            sleep 1
        done
        echo "[WARN] Redis 未能在 20 秒内就绪，继续启动"
    else
        echo "[WARN] docker compose 启动 Redis 失败，继续启动（将使用内存模式）"
    fi
}

ensure_docker_and_redis

# ========== 关闭当前项目的旧实例 ==========
if [ -f "$PORTS_FILE" ]; then
    echo "[INFO] 检测到本项目的旧实例，正在关闭..."
    source "$PORTS_FILE"
    if [ -n "$BACKEND_PORT" ]; then
        OLD_PIDS=$(lsof -ti:$BACKEND_PORT 2>/dev/null || true)
        if [ -n "$OLD_PIDS" ]; then
            echo "[INFO] 关闭旧后端进程: $OLD_PIDS (端口 $BACKEND_PORT)"
            echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
        fi
    fi
    if [ -n "$FRONTEND_PORT" ]; then
        OLD_PIDS=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
        if [ -n "$OLD_PIDS" ]; then
            echo "[INFO] 关闭旧前端进程: $OLD_PIDS (端口 $FRONTEND_PORT)"
            echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
        fi
    fi
    rm -f "$PORTS_FILE"
    sleep 2
    echo "[OK] 旧实例已关闭"
fi

# ========== 查找可用端口 ==========
echo "[INFO] 正在查找可用端口..."

find_available_port() {
    local start_port=$1
    local port=$start_port
    local max_port=$((start_port + 100))
    while [ $port -le $max_port ]; do
        if ! lsof -ti:$port &>/dev/null; then
            echo $port
            return 0
        fi
        port=$((port + 1))
    done
    return 1
}

BACKEND_PORT=$(find_available_port 8888) || {
    echo "[ERROR] 无法找到可用的后端端口"
    exit 1
}
FRONTEND_PORT=$(find_available_port 5188) || {
    echo "[ERROR] 无法找到可用的前端端口"
    exit 1
}

echo "[OK] 后端端口: $BACKEND_PORT"
echo "[OK] 前端端口: $FRONTEND_PORT"

echo "BACKEND_PORT=$BACKEND_PORT" > "$PORTS_FILE"
echo "FRONTEND_PORT=$FRONTEND_PORT" >> "$PORTS_FILE"
echo "[INFO] 端口信息已保存到 $PORTS_FILE"

# ========== 安装前端依赖 ==========
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
    echo "[INFO] 前端依赖未安装，正在安装..."
    cd "$FRONTEND_DIR" && npm install && cd "$PROJECT_ROOT"
    echo ""
fi

# 清理函数
cleanup() {
    echo ""
    echo "[INFO] 正在停止所有服务..."
    [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null || true
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    rm -f "$PORTS_FILE"
    echo "[INFO] 已停止"
    exit 0
}
trap cleanup INT TERM

# ========== 启动后端 ==========
echo "[1/2] 启动后端服务器 (FastAPI + WebSocket :$BACKEND_PORT)..."
export BACKEND_PORT=$BACKEND_PORT
PYTHONPATH="$PROJECT_ROOT/src" python "$PROJECT_ROOT/start_server.py" &
BACKEND_PID=$!

# 等待后端就绪
echo "[INFO] 等待后端服务就绪..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$BACKEND_PORT/health" 2>/dev/null | grep -q "200"; then
        echo "[OK] 后端已就绪"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[WARN] 后端未在 30 秒内就绪，继续启动前端..."
    fi
    sleep 1
done

# ========== 启动前端 ==========
echo "[2/2] 启动前端开发服务器 (Vite :$FRONTEND_PORT)..."
export VITE_API_BASE_URL="http://localhost:$BACKEND_PORT"
cd "$FRONTEND_DIR" && npx vite --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

# 等待前端就绪并打开浏览器
echo "[INFO] 等待前端服务就绪..."
for i in $(seq 1 30); do
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:$FRONTEND_PORT" 2>/dev/null | grep -q "200"; then
        echo "[OK] 前端已就绪"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "[WARN] 前端未在 30 秒内就绪，尝试打开浏览器..."
    fi
    sleep 1
done

# 打开浏览器
echo "[INFO] 打开浏览器..."
if command -v xdg-open &>/dev/null; then
    xdg-open "http://localhost:$FRONTEND_PORT"
elif command -v open &>/dev/null; then
    open "http://localhost:$FRONTEND_PORT"
elif command -v start &>/dev/null; then
    start "http://localhost:$FRONTEND_PORT"
fi

echo ""
echo "========================================"
echo "  服务已启动:"
echo "  后端: http://localhost:$BACKEND_PORT"
echo "  前端: http://localhost:$FRONTEND_PORT"
echo "  API 文档: http://localhost:$BACKEND_PORT/docs"
echo ""
echo "  项目目录: $PROJECT_ROOT"
echo "  端口文件: $PORTS_FILE"
echo "  按 Ctrl+C 停止所有服务"
echo "========================================"

wait
