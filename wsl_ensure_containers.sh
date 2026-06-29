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
# 未传参时回退到默认值，保证直接运行脚本也能工作。
PROJECT_DIR="${1:-/mnt/d/myproject/container_224042d3b925}"
cd "$PROJECT_DIR" 2>/dev/null || { echo "[ERROR] 项目目录不存在: $PROJECT_DIR"; exit 1; }

# 防御：compose 前先确认 daemon 的 unix socket 真正可响应
# （仅 docker version 走 TCP 也能通过，会掩盖 socket 未就绪的故障）
for i in $(seq 1 12); do
    [ -S /run/docker.sock ] && docker ps >/dev/null 2>&1 && break
    sleep 2
done
if ! docker ps >/dev/null 2>&1; then
    echo "[ERROR] docker daemon 不可用（/run/docker.sock 未就绪）"
    echo "[ERROR] 诊断: tail -30 /tmp/dockerd.log"
    exit 1
fi

echo "[INFO] docker compose up -d"
out="$(docker compose up -d 2>&1)"
rc=$?
echo "$out" | tail -8

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
echo "[INFO] 等待容器进入 running ..."
ok=0
for i in $(seq 1 15); do
    redis_up="$(docker ps --filter name=agent-os-redis-22404 --filter status=running --format '{{.Names}}')"
    front_up="$(docker ps --filter name=agent-os-frontend-22404 --filter status=running --format '{{.Names}}')"
    if [ -n "$redis_up" ] && [ -n "$front_up" ]; then ok=1; break; fi
    sleep 2
done

echo "--- 实际运行状态 ---"
docker ps --format '{{.Names}}\t{{.Status}}' | grep 'agent-os-' || true
if [ "$ok" -ne 1 ]; then
    echo "[WARN] 部分容器未进入 running（可能仍在构建或已失败），详见上方输出"
    exit 1
fi
echo "[OK] redis + frontend 均已运行"
exit 0
