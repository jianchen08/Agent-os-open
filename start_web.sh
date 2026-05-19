#!/usr/bin/env bash
# Agent OS Web Channel 启动脚本
# 同时启动后端 FastAPI 服务器和前端 Vite 开发服务器
# 支持多实例隔离：按项目目录区分，端口冲突时自动切换

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
PORTS_FILE="$PROJECT_ROOT/.ports"
PROJECT_ID=$(echo -n "$PROJECT_ROOT" | md5sum | cut -c1-8)
REDIS_CONTAINER="agent-os-redis-$PROJECT_ID"

echo "========================================"
echo "  Agent OS Web Channel 启动脚本"
echo "========================================"
echo ""
echo "项目目录: $PROJECT_ROOT"
echo "项目标识: $PROJECT_ID"

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

    if docker ps -q -f "name=$REDIS_CONTAINER" | grep -q .; then
        echo "[OK] Redis 容器 ($REDIS_CONTAINER) 已运行"
        return 0
    fi

    if docker ps -a -q -f "name=$REDIS_CONTAINER" | grep -q .; then
        echo "[INFO] Redis 容器 ($REDIS_CONTAINER) 已存在但未运行，正在启动..."
        docker start "$REDIS_CONTAINER" &>/dev/null
        if [ $? -eq 0 ]; then
            echo "[OK] Redis 容器已启动"
            return 0
        fi
    fi

    echo "[INFO] 正在创建 Redis 容器 ($REDIS_CONTAINER)..."
    REDIS_HOST_PORT=$(find_available_port 6379)
    docker run -d --name "$REDIS_CONTAINER" --restart unless-stopped \
        -p "$REDIS_HOST_PORT:6379" \
        redis:7-alpine redis-server --maxmemory 256mb --maxmemory-policy allkeys-lru --appendonly yes &>/dev/null
    if [ $? -eq 0 ]; then
        echo "[INFO] 等待 Redis 就绪..."
        for i in $(seq 1 20); do
            if docker exec "$REDIS_CONTAINER" redis-cli ping &>/dev/null; then
                echo "[OK] Redis 已就绪"
                return 0
            fi
            sleep 1
        done
        echo "[WARN] Redis 未能在 20 秒内就绪，继续启动"
    else
        echo "[WARN] Redis 容器启动失败，继续启动（将使用内存模式）"
    fi
}

ensure_docker_and_redis

# ========== 关闭当前项目的旧实例 ==========
if [ -f "$PORTS_FILE" ]; then
    echo "[INFO] 检测到本项目的旧实例，正在检查..."

    OLD_BACKEND_PORT=""
    OLD_FRONTEND_PORT=""
    OLD_PROJECT_ROOT=""
    OLD_PROJECT_ID=""
    OLD_BACKEND_PID=""
    OLD_FRONTEND_PID=""

    while IFS='=' read -r key value; do
        case "$key" in
            BACKEND_PORT) OLD_BACKEND_PORT="$value" ;;
            FRONTEND_PORT) OLD_FRONTEND_PORT="$value" ;;
            PROJECT_ROOT) OLD_PROJECT_ROOT="$value" ;;
            PROJECT_ID) OLD_PROJECT_ID="$value" ;;
            BACKEND_PID) OLD_BACKEND_PID="$value" ;;
            FRONTEND_PID) OLD_FRONTEND_PID="$value" ;;
        esac
    done < "$PORTS_FILE"

    if [ -n "$OLD_PROJECT_ROOT" ] && [ "$PROJECT_ROOT" != "$OLD_PROJECT_ROOT" ]; then
        echo "[INFO] 端口文件属于其他项目目录 [$OLD_PROJECT_ROOT]，跳过关闭"
        rm -f "$PORTS_FILE"
    else
        if [ -n "$OLD_BACKEND_PORT" ] && command -v lsof &>/dev/null; then
            OLD_PIDS=$(lsof -ti:$OLD_BACKEND_PORT 2>/dev/null || true)
            if [ -n "$OLD_PIDS" ]; then
                if [ -n "$OLD_BACKEND_PID" ]; then
                    for pid in $OLD_PIDS; do
                        if [ "$pid" = "$OLD_BACKEND_PID" ]; then
                            echo "[INFO] 关闭旧后端进程: $pid (端口 $OLD_BACKEND_PORT)"
                            kill -9 "$pid" 2>/dev/null || true
                        else
                            echo "[WARN] 端口 $OLD_BACKEND_PORT 上的进程已变更（旧PID=$OLD_BACKEND_PID，当前PID=$pid），跳过关闭"
                        fi
                    done
                else
                    echo "[INFO] 关闭旧后端进程: $OLD_PIDS (端口 $OLD_BACKEND_PORT)"
                    echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
                fi
            fi
        fi
        if [ -n "$OLD_FRONTEND_PORT" ] && command -v lsof &>/dev/null; then
            OLD_PIDS=$(lsof -ti:$OLD_FRONTEND_PORT 2>/dev/null || true)
            if [ -n "$OLD_PIDS" ]; then
                if [ -n "$OLD_FRONTEND_PID" ]; then
                    for pid in $OLD_PIDS; do
                        if [ "$pid" = "$OLD_FRONTEND_PID" ]; then
                            echo "[INFO] 关闭旧前端进程: $pid (端口 $OLD_FRONTEND_PORT)"
                            kill -9 "$pid" 2>/dev/null || true
                        else
                            echo "[WARN] 端口 $OLD_FRONTEND_PORT 上的进程已变更（旧PID=$OLD_FRONTEND_PID，当前PID=$pid），跳过关闭"
                        fi
                    done
                else
                    echo "[INFO] 关闭旧前端进程: $OLD_PIDS (端口 $OLD_FRONTEND_PORT)"
                    echo "$OLD_PIDS" | xargs kill -9 2>/dev/null || true
                fi
            fi
        fi
        rm -f "$PORTS_FILE"
        sleep 2
        echo "[OK] 旧实例检查完成"
    fi
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
echo "PROJECT_ROOT=$PROJECT_ROOT" >> "$PORTS_FILE"
echo "PROJECT_ID=$PROJECT_ID" >> "$PORTS_FILE"
echo "REDIS_HOST_PORT=${REDIS_HOST_PORT:-6379}" >> "$PORTS_FILE"
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
export REDIS_PORT=${REDIS_HOST_PORT:-6379}
export _AO_PROJECT_ID=$PROJECT_ID
PYTHONPATH="$PROJECT_ROOT/src" python "$PROJECT_ROOT/app_factory.py" &
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
cd "$FRONTEND_DIR" && _AO_PROJECT_ID=$PROJECT_ID npx vite --port "$FRONTEND_PORT" &
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

# 更新 .ports 文件，追加 PID 信息
echo "BACKEND_PID=$BACKEND_PID" >> "$PORTS_FILE"
echo "FRONTEND_PID=$FRONTEND_PID" >> "$PORTS_FILE"

echo ""
echo "========================================"
echo "  服务已启动:"
echo "  后端: http://localhost:$BACKEND_PORT"
echo "  前端: http://localhost:$FRONTEND_PORT"
echo "  API 文档: http://localhost:$BACKEND_PORT/docs"
echo ""
echo "  项目目录: $PROJECT_ROOT"
echo "  项目标识: $PROJECT_ID"
echo "  Redis 容器: $REDIS_CONTAINER"
echo "  端口文件: $PORTS_FILE"
echo "  按 Ctrl+C 停止所有服务"
echo "========================================"

wait
