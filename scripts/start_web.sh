#!/usr/bin/env bash
# Agent OS Web Channel 启动脚本
# 同时启动后端 FastAPI 服务器和前端 Vite 开发服务器
# 重复运行会自动关闭旧进程后重启

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
BACKEND_PORT=8888
FRONTEND_PORT=5188

echo "========================================"
echo "  Agent OS Web Channel 启动脚本"
echo "========================================"
echo ""

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

# ========== 关闭旧进程 ==========
echo "[INFO] 检查并关闭旧进程..."

# 关闭占用 8888 端口的进程
if command -v lsof &>/dev/null; then
    OLD_BACKEND_PIDS=$(lsof -ti:$BACKEND_PORT 2>/dev/null || true)
    if [ -n "$OLD_BACKEND_PIDS" ]; then
        echo "[INFO] 关闭旧后端进程: $OLD_BACKEND_PIDS"
        echo "$OLD_BACKEND_PIDS" | xargs kill -9 2>/dev/null || true
    fi
fi

# 关闭占用 5188 端口的进程
if command -v lsof &>/dev/null; then
    OLD_FRONTEND_PIDS=$(lsof -ti:$FRONTEND_PORT 2>/dev/null || true)
    if [ -n "$OLD_FRONTEND_PIDS" ]; then
        echo "[INFO] 关闭旧前端进程: $OLD_FRONTEND_PIDS"
        echo "$OLD_FRONTEND_PIDS" | xargs kill -9 2>/dev/null || true
    fi
fi

sleep 2

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
    echo "[INFO] 已停止"
    exit 0
}
trap cleanup INT TERM

# ========== 启动后端 ==========
echo "[1/2] 启动后端服务器 (FastAPI + WebSocket :$BACKEND_PORT)..."
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
cd "$FRONTEND_DIR" && npm run dev &
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
echo "  按 Ctrl+C 停止所有服务"
echo "========================================"

wait
