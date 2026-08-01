#!/usr/bin/env bash
# WSL2 内核健康度探针：检查是否存在持续的 D 状态（不可中断磁盘睡眠）进程。
# 由 start_web_cn.bat 用 `timeout` 包住调用。
#
# 退出码约定：
#   0  健康（无持续 D 状态进程，或仅为启动期瞬时 D）
#   8  内核被 D 状态死锁污染（需 wsl --shutdown 重启内核）
#   9  探针自身异常
#
# 设计要点：
#   - 不用 ps/pgrep（它们遍历 /proc 读 cmdline 会被 D 状态进程传染卡死），
#     改为只读 status 文件的 State: 行，单点读取，不触发 access_remote_vm。
#   - 每个 PID 的读取再套一层 timeout，防止单个 /proc/<pid>/status 也卡住。
#   - 上层 bat 已用 timeout 包住整个脚本，这里是双保险。
#   - 双重采样：发现 D 进程后 sleep 2 再复查同 PID，仍为 D 才计入。
#     瞬时 D（正常 IO 等待、启动期 sysctl 初始化）通常 2 秒内消失，
#     内核死锁的 D 持续存在。
#   - 启动期宽容：WSL 刚重启（uptime < 60s）时 systemd 初始化进程可能短暂
#     处于 D 状态，属正常现象；此时要求 >=2 个持续 D 才判污染，避免误判
#     导致无限 wsl --shutdown 循环。运行期则严格判断（>=1 即污染）。
#
# 可测化钩子（仅测试使用，生产环境不设置）：
#   AO_PROBE_FORCE_DCOUNT=<n>   跳过真实 /proc 采样，直接用指定持续 D 计数判定
#   AO_PROBE_FORCE_UPTIME=<s>   跳过 /proc/uptime 读取，强制指定 uptime（秒）
#   AO_PROBE_FORCE_IGNORED=<n>  启动期被忽略的良性 D 进程数（与 FORCE_DCOUNT 配合，仅影响日志）
#   AO_PROBE_SKIP_SLEEP=1       跳过采样间 sleep 2（测试加速）
set -uo pipefail

# 已知的良性初始化进程，启动期可忽略其瞬时 D 状态。
# landscape-client(landscape-confi) 是 Ubuntu 系统信息采集服务,
# 偶发 D 状态但不影响 docker/项目运行。
# 注意：/proc/<pid>/status 的 Name 字段最长 15 字符，故用截断形式。
IGNORE_NAMES="systemd-sysctl|systemd-journal|multipathd|systemd-udevd|landscape-conf|landscape-clien|unattended-upgr|packagekitd|apt-daily|dpkg"

# ──────────────────────────────────────────────────────────────────────────
# classify_d_state: 核心判定逻辑（纯函数，可单测）
#
# 入参（位置参数）：
#   $1 d_count      双重采样后仍为 D 的进程数（已剔除启动期被忽略的良性进程）
#   $2 ignored      启动期被忽略的良性 D 进程数（仅用于日志展示）
#   $3 boot_phase   1=启动期(uptime<60s) 0=运行期
#   $4 uptime_int   uptime 秒数（仅用于日志展示）
# 出参：
#   stdout: [OK]/[WARN] 汇总行（与原日志格式一致）
#   返回码: 0 健康 / 8 内核污染
# ──────────────────────────────────────────────────────────────────────────
classify_d_state() {
    local d_count="${1:-0}"
    local ignored="${2:-0}"
    local boot_phase="${3:-0}"
    local uptime_int="${4:-0}"

    # 阈值：启动期 >=2，运行期 >=1
    local threshold phase_label
    if [ "$boot_phase" = "1" ]; then
        threshold=2
        phase_label="(boot-phase, uptime=${uptime_int}s)"
    else
        threshold=1
        phase_label="(running, uptime=${uptime_int}s)"
    fi

    if [ "$d_count" -ge "$threshold" ]; then
        echo "[WARN] $d_count persistent D-state processes $phase_label -> kernel polluted"
        return 8
    fi
    echo "[OK] WSL kernel healthy (persistent D-state: $d_count) $phase_label"
    return 0
}

# ──────────────────────────────────────────────────────────────────────────
# 采样阶段：读 /proc，双重采样统计持续 D 进程
# ──────────────────────────────────────────────────────────────────────────

# 判断是否处于启动期（uptime < 60s），启动期对瞬时 D 更宽容
compute_boot_phase() {
    local uptime_raw uptime_int
    uptime_raw="$(cat /proc/uptime 2>/dev/null | cut -d' ' -f1)"
    uptime_int="${uptime_raw%.*}"
    if [ -n "$uptime_int" ] && [ "$uptime_int" -lt 60 ] 2>/dev/null; then
        echo "1:$uptime_int"
    else
        echo "0:$uptime_int"
    fi
}

# 采集所有处于 D 状态的 PID + Name（第一遍）
sample_first_pass() {
    local first_pass="" p st pid name
    for p in /proc/[0-9]*; do
        [ -r "$p/status" ] || continue
        st=$(timeout 1 awk '/^State:/{print $2; exit}' "$p/status" 2>/dev/null || true)
        if [ "$st" = "D" ]; then
            pid=$(basename "$p")
            name=$(timeout 1 awk '/^Name:/{print $2; exit}' "$p/status" 2>/dev/null || echo "?")
            first_pass="${first_pass}${pid}:${name}|"
        fi
    done
    printf '%s' "$first_pass"
}

# 双重采样：仅复查第一遍命中的 PID，避开遍历全量 /proc（降低被传染概率）
# 输出契约（关键：诊断行与计数值分流，避免 tail 解析歧义）：
#   - 诊断行 [INFO]/[WARN] → 写 stderr（供调用方原样回显）
#   - 计数值              → 写 stdout，固定两行：d_count\nignored
sample_persistent_d() {
    local first_pass="$1"
    local boot_phase="$2"
    local d_count=0 ignored=0

    if [ -z "$first_pass" ]; then
        echo "0"
        echo "0"
        return
    fi

    # AO_PROBE_SKIP_SLEEP=1 时跳过 sleep（测试加速）
    if [ "${AO_PROBE_SKIP_SLEEP:-0}" != "1" ]; then
        sleep 2
    fi

    local pair pid name st
    local _ifs="$IFS"
    IFS='|'
    set -f
    for pair in $first_pass; do
        [ -z "$pair" ] && continue
        pid="${pair%%:*}"
        name="${pair#*:}"
        [ -r "/proc/$pid/status" ] || continue
        st=$(timeout 1 awk '/^State:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)
        if [ "$st" = "D" ]; then
            # 启动期忽略已知初始化进程的瞬时 D
            if [ "$boot_phase" = "1" ] && echo "$name" | grep -qE "^($IGNORE_NAMES)$"; then
                echo "[INFO] boot-phase ignoring transient D: pid=$pid name=$name" >&2
                ignored=$((ignored + 1))
                continue
            fi
            d_count=$((d_count + 1))
            echo "[WARN] persistent D-state: pid=$pid name=$name" >&2
        fi
    done
    set +f
    IFS="$_ifs"

    echo "$d_count"
    echo "$ignored"
}

# ──────────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────────

# 测试注入快捷路径：AO_PROBE_FORCE_DCOUNT 设置时跳过全部真实采样
if [ -n "${AO_PROBE_FORCE_DCOUNT+x}" ]; then
    boot_phase="${AO_PROBE_FORCE_BOOT_PHASE:-0}"
    uptime_int="${AO_PROBE_FORCE_UPTIME:-0}"
    classify_d_state "${AO_PROBE_FORCE_DCOUNT}" "${AO_PROBE_FORCE_IGNORED:-0}" "$boot_phase" "$uptime_int"
    exit $?
fi

# 真实采样路径
bp_line="$(compute_boot_phase)"
boot_phase="${bp_line%%:*}"
uptime_int="${bp_line#*:}"

first_pass="$(sample_first_pass)"

# sample_persistent_d：诊断行已分流到 stderr（此处自然透传给上层），
# stdout 固定返回两行数值（d_count / ignored）。
counts="$(sample_persistent_d "$first_pass" "$boot_phase")"
d_count="$(printf '%s\n' "$counts" | sed -n '1p')"
ignored="$(printf '%s\n' "$counts" | sed -n '2p')"
d_count="${d_count:-0}"
ignored="${ignored:-0}"

classify_d_state "$d_count" "$ignored" "$boot_phase" "$uptime_int"
exit $?
