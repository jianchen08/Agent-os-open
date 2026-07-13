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
set -uo pipefail

d_count=0

# 判断是否处于启动期（uptime < 60s），启动期对瞬时 D 更宽容
uptime_raw="$(cat /proc/uptime 2>/dev/null | cut -d' ' -f1)"
uptime_int="${uptime_raw%.*}"
boot_phase=0
if [ -n "$uptime_int" ] && [ "$uptime_int" -lt 60 ] 2>/dev/null; then
    boot_phase=1
fi

# 已知的良性初始化进程，启动期可忽略其瞬时 D 状态。
# landscape-client(landscape-confi) 是 Ubuntu 系统信息采集服务,
# 偶发 D 状态但不影响 docker/项目运行。
ignore_names="systemd-sysctl|systemd-journal|multipathd|systemd-udevd|landscape-conf|landscape-clien|unattended-upgr|packagekitd|apt-daily|dpkg"

# 第一遍：采集所有处于 D 状态的 PID + Name
first_pass=""
for p in /proc/[0-9]*; do
    [ -r "$p/status" ] || continue
    st=$(timeout 1 awk '/^State:/{print $2; exit}' "$p/status" 2>/dev/null || true)
    if [ "$st" = "D" ]; then
        pid=$(basename "$p")
        name=$(timeout 1 awk '/^Name:/{print $2; exit}' "$p/status" 2>/dev/null || echo "?")
        first_pass="${first_pass}${pid}:${name}|"
    fi
done

# 双重采样：仅复查第一遍命中的 PID，避开遍历全量 /proc（降低被传染概率）
if [ -n "$first_pass" ]; then
    sleep 2
    IFS='|' read -ra _pairs <<< "$first_pass"
    for pair in "${_pairs[@]}"; do
        [ -z "$pair" ] && continue
        pid="${pair%%:*}"
        name="${pair#*:}"
        [ -r "/proc/$pid/status" ] || continue
        st=$(timeout 1 awk '/^State:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)
        if [ "$st" = "D" ]; then
            # 启动期忽略已知初始化进程的瞬时 D
            if [ "$boot_phase" = "1" ] && echo "$name" | grep -qE "^($ignore_names)$"; then
                echo "[INFO] boot-phase ignoring transient D: pid=$pid name=$name"
                continue
            fi
            d_count=$((d_count + 1))
            echo "[WARN] persistent D-state: pid=$pid name=$name"
        fi
    done
fi

# 阈值：启动期 >=2，运行期 >=1
if [ "$boot_phase" = "1" ]; then
    threshold=2
    phase_label="(boot-phase, uptime=${uptime_int}s)"
else
    threshold=1
    phase_label="(running, uptime=${uptime_int}s)"
fi

if [ "$d_count" -ge "$threshold" ]; then
    echo "[WARN] $d_count persistent D-state processes $phase_label -> kernel polluted"
    exit 8
fi
echo "[OK] WSL kernel healthy (persistent D-state: $d_count) $phase_label"
exit 0
