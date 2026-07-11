#!/usr/bin/env bash
# WSL native docker 模式：启动项目容器并做真实状态校验。
# 由 start_web_cn.bat 调用。
#
# 退出码约定：
#   0  redis + frontend 均已 running
#   7  WSL cgroup 残留（D 状态内核线程未释放） -> 必须 wsl --shutdown，脚本无法自愈
#   1  其它 docker / compose 失败
set -uo pipefail

# 项目目录由 start_web_cn.bat 通过 $1 传入（wslpath 转换后的路径）。
# 未传参时用 wslpath 从当前 Windows 工作目录动态推导，保证直接运行脚本也能工作
# 且不依赖写死的挂载路径；wslpath 不可用时回退到脚本所在目录。
if [ -z "${1:-}" ]; then
    _guessed="$(wslpath "$(pwd)" 2>/dev/null || true)"
    PROJECT_DIR="${_guessed:-$(cd "$(dirname "$0")" && pwd)}"
else
    PROJECT_DIR="$1"
fi
cd "$PROJECT_DIR" 2>/dev/null || { echo "[ERROR] 项目目录不存在: $PROJECT_DIR"; exit 1; }

# 防御：compose 前先确认 daemon 的 unix socket 真正可响应
# （仅 docker version 走 TCP 也能通过，会掩盖 socket 未就绪的故障）
# docker ps 遍历容器也会被 D 状态传染卡死，必须加 timeout；超时即判污染返回 7。
socket_ready=0
for i in $(seq 1 12); do
    if timeout 8 docker ps >/dev/null 2>&1; then
        socket_ready=1
        break
    fi
    rc=$?
    if [ "$rc" -eq 124 ]; then
        echo "[WARN] docker ps 超时（疑似内核 D 状态污染）"
        exit 7
    fi
    sleep 2
done
if [ "$socket_ready" -ne 1 ]; then
    echo "[ERROR] docker daemon 不可用（/run/docker.sock 未就绪）"
    echo "[ERROR] 诊断: tail -30 /tmp/dockerd.log"
    exit 1
fi

# 清理上轮残留的任务容器（cua- 前缀），只保留 frontend + redis。
# 这些容器由 agent 任务运行时（IsolationManager）按需创建，重启时丢弃安全，系统会重建。
# 注意：若容器内进程处于 D 状态（WSL2 内核死锁），docker rm -f 会卡住/失败，
# 此时返回 7，由上层 start_web_cn.bat 自动 wsl --shutdown 重启内核后重试。
echo "[INFO] 清理上轮任务容器（cua- 前缀，仅保留 frontend + redis）..."
stuck=0
# 先把 docker ps -a 跑出来（带 timeout），再 grep；分开做才能正确捕获 timeout 退出码 124
all_names="$(timeout 15 docker ps -a --format '{{.Names}}' 2>/dev/null)"
rc=$?
if [ "$rc" -eq 124 ]; then
    echo "[WARN] docker ps -a 超时（疑似内核 D 状态污染）"
    exit 7
elif [ "$rc" -ne 0 ]; then
    echo "[ERROR] docker ps -a 失败 (rc=$rc)"
    exit "$rc"
fi
cua_list="$(printf '%s\n' "$all_names" | grep '^cua-' || true)"
while IFS= read -r cname; do
    [ -z "$cname" ] && continue
    if timeout 30 docker rm -f "$cname" >/dev/null 2>&1; then
        echo "  [OK] removed $cname"
    else
        echo "  [WARN] 清理 $cname 失败/超时（疑似内核 D 状态死锁）"
        stuck=1
    fi
done <<< "$cua_list"
if [ "$stuck" -ne 0 ]; then
    echo "[FATAL] 任务容器清理受阻，需 wsl --shutdown 重启内核后重试。"
    exit 7
fi

# 确保 daemon.json 配了国内镜像加速(慢网络下从 docker.io 拉镜像会超时)。
# 如果 daemon.json 不存在或缺少 registry-mirrors,写入并重启 docker。
DAEMON_JSON="/etc/docker/daemon.json"
NEED_MIRROR_FIX=0
if [ ! -f "$DAEMON_JSON" ]; then
    NEED_MIRROR_FIX=1
elif ! grep -q "registry-mirrors" "$DAEMON_JSON" 2>/dev/null; then
    NEED_MIRROR_FIX=1
fi
if [ "$NEED_MIRROR_FIX" = "1" ]; then
    echo "[INFO] Configuring Docker registry mirrors (daocloud + tencent)..."
    mkdir -p /etc/docker
    cat > "$DAEMON_JSON" << 'MIRROREOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://mirror.ccs.tencentyun.com"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
MIRROREOF
    # 重启 docker daemon 使镜像加速生效
    if systemctl restart docker 2>/dev/null; then
        echo "[OK] Docker daemon restarted with registry mirrors"
        sleep 3
    elif service docker restart 2>/dev/null; then
        echo "[OK] Docker service restarted with registry mirrors"
        sleep 3
    fi
fi

echo "[INFO] docker compose up -d"

# WSL 原生 docker 模式下，host.docker.internal 不被注入（非 Docker Desktop）。
# 获取 WSL 默认网关（= Windows 宿主地址），传给 docker-compose 的 BACKEND_HOST_IP，
# 使 frontend 容器能正确连接 Windows 宿主上的后端（8988）。
# Docker Desktop 模式不会执行此脚本，compose 自动回退 host.docker.internal。
if [ -z "${BACKEND_HOST_IP:-}" ]; then
    BACKEND_HOST_IP="$(ip route show default 2>/dev/null | awk '/default/{print $3; exit}')"
fi
if [ -n "$BACKEND_HOST_IP" ]; then
    echo "[INFO] BACKEND_HOST_IP=${BACKEND_HOST_IP}"
    export BACKEND_HOST_IP
fi

# 构建与启动分两步: build 用"无输出超时"监控(只要持续有输出就不杀,
# 连续 300s 无输出才判定卡死); up 启动已构建镜像,60s 无输出超时。
export COMPOSE_PROGRESS=plain
COMPOSE_OUT="${TMPDIR:-/tmp}/compose_up_$$.out"

# run_with_idle_timeout: 后台运行命令,监控其 stdout 输出,
# 连续 IDLE_TIMEOUT 秒无新输出(卡死)才杀掉;只要在持续下载/构建就不中断。
# $1=idle_timeout_sec  $2..=command
run_with_idle_timeout() {
    local idle_timeout="$1"; shift
    local out_file="${TMPDIR:-/tmp}/idle_monitor_$$.out"
    local last_size=0
    local stable_secs=0
    local check_interval=15

    "$@" > "$out_file" 2>&1 &
    local cmd_pid=$!

    while kill -0 "$cmd_pid" 2>/dev/null; do
        sleep "$check_interval"
        local cur_size
        cur_size=$(wc -c < "$out_file" 2>/dev/null || echo 0)
        if [ "$cur_size" -gt "$last_size" ]; then
            # 有新输出,重置计时器,把增量打出来
            tail -c +$((last_size + 1)) "$out_file" 2>/dev/null
            last_size="$cur_size"
            stable_secs=0
        else
            stable_secs=$((stable_secs + check_interval))
            if [ "$stable_secs" -ge "$idle_timeout" ]; then
                echo "[WARN] No output for ${idle_timeout}s, process likely hung. Killing..."
                kill -TERM "$cmd_pid" 2>/dev/null
                sleep 2
                kill -KILL "$cmd_pid" 2>/dev/null
                wait "$cmd_pid" 2>/dev/null
                rm -f "$out_file"
                return 124
            fi
        fi
    done

    # 进程已结束,输出剩余内容
    tail -c +$((last_size + 1)) "$out_file" 2>/dev/null
    wait "$cmd_pid" 2>/dev/null
    local rc=$?
    rm -f "$out_file"
    return $rc
}

# build: 无输出超时 300s(5分钟没动静才杀,下载慢但持续有进度不会触发)
echo "[INFO] docker compose build (idle timeout 300s = only kills if 5min no output)..."
run_with_idle_timeout 300 docker compose build
BUILD_RC=$?
if [ "$BUILD_RC" -ne 0 ] && [ "$BUILD_RC" -ne 124 ]; then
    echo "[ERROR] docker compose build failed (rc=$BUILD_RC)"
    exit 1
fi
if [ "$BUILD_RC" -eq 124 ]; then
    echo "[WARN] build hung (no output for 300s). Check network or wsl --shutdown and retry."
    exit 7
fi

# up: 启动已构建镜像,60s 无输出超时
echo "[INFO] docker compose up -d..."
run_with_idle_timeout 60 docker compose up -d
rc=$?
rm -f "$COMPOSE_OUT"

if [ "$rc" -eq 124 ]; then
    echo "[WARN] compose up hung (no output for 60s). wsl --shutdown and retry."
    exit 7
fi

# 关键：不要让管道吞掉 compose 的退出码；明确判断失败原因
if [ "$rc" -ne 0 ]; then
    # 命中以下任一特征，均说明 docker/containerd/runc 三方状态不一致，
    # 根源是上次容器停止时有线程以 D 状态卡在内核，旧 cgroup/task/state 永远清不掉。
    # 用户态无法自愈，必须 wsl --shutdown 重启内核。
    if echo "$out" | grep -qiE 'cgroup is not empty|failed to create (task|shim task|shim)|container with given ID already exists|task .* already exists'; then
        echo ""
        echo "[FATAL] Docker/containerd/runc 状态不一致：无法为容器创建任务。"
        echo "[FATAL] 通常是上次容器停止时，redis 等进程以 D 状态（不可中断磁盘睡眠）"
        echo "[FATAL] 卡在内核里，旧 cgroup/task/state 永远清不掉，脚本无法自愈。"
        echo "[FATAL] 请在 Windows 执行：  wsl --shutdown"
        echo "[FATAL] 等待约 10 秒后重新双击 start_web_cn.bat。"
        echo "[FATAL] （已关闭 redis AOF 持久化以降低复发概率）"
        exit 7
    fi
    echo "[ERROR] docker compose 失败 (rc=$rc)"
    exit "$rc"
fi

# 真正等待容器进入 running，而非盲目 sleep 后报 OK
# 容器名跟随 compose project（目录名），用 `docker compose ps` 按服务名查询，
# 不依赖固定容器名前缀（update_frontend.ps1 已采用同一范式）。
echo "[INFO] 等待容器进入 running ..."
ok=0
for i in $(seq 1 15); do
    redis_up="$(timeout 8 docker compose ps -q redis 2>/dev/null)"
    front_up="$(timeout 8 docker compose ps -q frontend 2>/dev/null)"
    # 有容器 ID 还需确认处于 running（compose ps -q 含已停止的）
    if [ -n "$redis_up" ] && [ -n "$front_up" ]; then
        redis_state="$(timeout 8 docker inspect -f '{{.State.Running}}' "$redis_up" 2>/dev/null)"
        front_state="$(timeout 8 docker inspect -f '{{.State.Running}}' "$front_up" 2>/dev/null)"
        if [ "$redis_state" = "true" ] && [ "$front_state" = "true" ]; then ok=1; break; fi
    fi
    sleep 2
done

echo "--- 实际运行状态 ---"
timeout 8 docker compose ps --format '{{.Service}}\t{{.Name}}\t{{.Status}}' 2>/dev/null || true
if [ "$ok" -ne 1 ]; then
    echo "[WARN] 部分容器未进入 running（可能仍在构建或已失败），详见上方输出"
    exit 1
fi
echo "[OK] redis + frontend 均已运行"
exit 0
