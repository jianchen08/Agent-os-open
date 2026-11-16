#!/usr/bin/env bash
# AgentOS 内核裸起脚本（无 systemd 环境时与 deploy/agentos-kernel.service 等价）。
#
# - env 注入面与 electron/kernel-manager.ts buildKernelEnv 同口径；部署差异：
#   AGENTOS_BIND 默认 0.0.0.0（远程可达），并显式钉 AGENTOS_USER_ROOT；
# - 密钥必设：AGENTOS_TOKEN_SECRET / AGENTOS_ADMIN_PASSWORD 缺失直接报错退出
#   （不静默生成，理由见下方各报错）；
# - 监督循环语义对齐 start_web_02.sh kernel_supervisor：退出码 75（内核请求
#   重启：/api/v1/system/restart 排空退出 / cdylib 集合变更自动重启）1s 重拉
#   不计数；其它退出指数退避重拉，连续 5 次熔断；单次运行 ≥60s 重置退避；
# - trap 转发 SIGTERM/SIGINT 给内核子进程（内核已内置 SIGTERM 优雅退出，
#   sidecar 清理总预算 2s），随内核退出；
# - 日志：内核 CWD 可写时落 CWD 相对 logs/，脚本 cd 仓库根即落 <repo>/logs/
#   （与 systemd unit 的 WorkingDirectory 钉仓库根同一效果）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ========== env 注入面（可被外部环境变量覆盖端口/BIND） ==========
AGENTOS_BIND="${AGENTOS_BIND:-0.0.0.0}"
AGENTOS_KERNEL_PORT="${AGENTOS_KERNEL_PORT:-9100}"
export AGENTOS_BIND AGENTOS_KERNEL_PORT
# 编译期默认路径嵌入二进制，必须显式指向仓库内目录
export AGENTOS_PLUGINS_DIR="${AGENTOS_PLUGINS_DIR:-$REPO_ROOT/plugins/shared}"
export AGENTOS_CONFIG_ROOT="${AGENTOS_CONFIG_ROOT:-$REPO_ROOT/config}"
# 显式钉用户根：裸起/systemd 环境缺 HOME 时 OS 标准目录解析失败 →
# user_root=None 全链降级（库/密钥 .env/用户配置全失锚）
export AGENTOS_USER_ROOT="${AGENTOS_USER_ROOT:-$REPO_ROOT/user_root}"
# 缺 .venv 的 Python sidecar 由内核 boot 后台 uv sync 自愈（需 uv 在 PATH）
export AGENTOS_PLUGIN_VENV_AUTOPROVISION=1

# ========== 密钥（缺失即报错退出，不静默生成） ==========
if [ -z "${AGENTOS_TOKEN_SECRET:-}" ]; then
    echo "[ERROR] AGENTOS_TOKEN_SECRET 未设置：内核将每进程随机签名，重启后全部 token 失效（全端掉线）。生成：openssl rand -hex 32" >&2
    exit 1
fi
if [ -z "${AGENTOS_ADMIN_PASSWORD:-}" ]; then
    echo "[ERROR] AGENTOS_ADMIN_PASSWORD 未设置：首启将生成随机口令并仅打印一次到控制台，headless 下即丢失。" >&2
    exit 1
fi
export AGENTOS_TOKEN_SECRET AGENTOS_ADMIN_PASSWORD
# 可选 AGENTOS_CORS_ORIGINS：前端 origin 非 localhost 时必须在外部环境设置
# （逗号分隔完整 origin 白名单），此处原样继承，不做加工。

# ========== 插件 venv 前置（只提示不自动 sync，补齐走 deploy/init.sh） ==========
if [ ! -d "$REPO_ROOT/plugins/shared/_host/.venv" ]; then
    echo "[WARN] plugins/shared/_host/.venv 缺失：合宿成员插件将 spawn 失败。先执行 bash deploy/init.sh 补齐 venv 供给（autoprovision 只建壳不装依赖）。" >&2
fi

# ========== 内核二进制 ==========
KERNEL_BIN="$REPO_ROOT/kernel/target/release/agentos-kernel"
if [ ! -x "$KERNEL_BIN" ]; then
    echo "[ERROR] 内核二进制不存在：$KERNEL_BIN（前置：cd $REPO_ROOT/kernel && cargo build --release）" >&2
    exit 1
fi

cd "$REPO_ROOT"
echo "[start_server] 内核启动中: bind=$AGENTOS_BIND port=$AGENTOS_KERNEL_PORT repo=$REPO_ROOT"

# ========== 监督循环（语义对齐 start_web_02.sh kernel_supervisor） ==========
# 退避/熔断参数：环境变量可覆盖（测试注入小值快速验证），缺省即生产契约。
: "${AGENTOS_SUPERVISOR_BACKOFF_MS_BASE:=5000}"
: "${AGENTOS_SUPERVISOR_BACKOFF_MS_MAX:=45000}"
: "${AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS:=5}"
: "${AGENTOS_SUPERVISOR_STABLE_SECS:=60}"

KERNEL_CHILD_PID=""
STOP=0
on_signal() {
    STOP=1
    if [ -n "$KERNEL_CHILD_PID" ]; then
        kill -TERM "$KERNEL_CHILD_PID" 2>/dev/null || true
    fi
}
trap on_signal TERM INT

kernel_supervisor() {
    local fails=0 backoff_ms=0
    local spawn_ts=0 rc=0 runtime_secs=0
    while true; do
        # 退出码必须经 || 捕获：set -e 下裸命令非零退出会在 rc=$? 执行前终止本子 shell
        rc=0
        spawn_ts=$(date +%s)
        "$KERNEL_BIN" &
        KERNEL_CHILD_PID=$!
        wait "$KERNEL_CHILD_PID" || rc=$?
        if [ "$STOP" -eq 1 ]; then
            # wait 可能被信号打断提前返回：等内核真正退出（SIGTERM 优雅退出，
            # sidecar 清理总预算 2s）再随其退出码收场
            wait "$KERNEL_CHILD_PID" 2>/dev/null || true
            KERNEL_CHILD_PID=""
            return "$rc"
        fi
        KERNEL_CHILD_PID=""
        runtime_secs=$(( $(date +%s) - spawn_ts ))
        if [ "$rc" -eq 75 ]; then
            echo "[supervisor] G8 restart requested (exit 75) — respawning in 1s..."
            sleep 1
            if [ "$STOP" -eq 1 ]; then
                return "$rc"
            fi
            continue
        fi
        if [ "$runtime_secs" -ge "$AGENTOS_SUPERVISOR_STABLE_SECS" ]; then
            fails=0
            backoff_ms=0
        fi
        fails=$((fails + 1))
        echo "[supervisor] kernel exited (code $rc, ran ${runtime_secs}s), consecutive failures: $fails."
        if [ "$fails" -ge "$AGENTOS_SUPERVISOR_MAX_CONSECUTIVE_FAILS" ]; then
            echo "[supervisor] circuit break: $fails consecutive failures, supervisor stops."
            return "$rc"
        fi
        if [ "$backoff_ms" -eq 0 ]; then
            backoff_ms=$AGENTOS_SUPERVISOR_BACKOFF_MS_BASE
        fi
        if [ "$backoff_ms" -gt "$AGENTOS_SUPERVISOR_BACKOFF_MS_MAX" ]; then
            backoff_ms=$AGENTOS_SUPERVISOR_BACKOFF_MS_MAX
        fi
        echo "[supervisor] non-75 exit, backing off ${backoff_ms}ms before respawn."
        sleep "$((backoff_ms / 1000)).$((backoff_ms % 1000))"
        if [ "$STOP" -eq 1 ]; then
            return "$rc"
        fi
        backoff_ms=$((backoff_ms * 3))
    done
}

kernel_supervisor
