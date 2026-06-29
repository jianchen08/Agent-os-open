#!/usr/bin/env bash
# WSL2 内核健康度探针：检查是否存在 D 状态（不可中断磁盘睡眠）进程。
# 由 start_web_cn.bat 用 `timeout` 包住调用。
#
# 退出码约定：
#   0  健康（无 D 状态进程，或仅有极少数可忽略的瞬时 D）
#   8  内核被 D 状态死锁污染（需 wsl --shutdown 重启内核）
#   9  探针自身异常
#
# 设计要点：
#   - 不用 ps/pgrep（它们遍历 /proc 读 cmdline 会被 D 状态进程传染卡死），
#     改为只读 status 文件的 State: 行，单点读取，不触发 access_remote_vm。
#   - 每个 PID 的读取再套一层 timeout，防止单个 /proc/<pid>/status 也卡住。
#   - 上层 bat 已用 timeout 包住整个脚本，这里是双保险。
set -uo pipefail

d_count=0
for p in /proc/[0-9]*; do
    [ -r "$p/status" ] || continue
    st=$(timeout 1 awk '/^State:/{print $2; exit}' "$p/status" 2>/dev/null || true)
    if [ "$st" = "D" ]; then
        d_count=$((d_count + 1))
    fi
done

# 经验阈值：>=3 个 D 状态进程基本可断定是 WSL2 内核死锁扩散
# （正常瞬时 D 最多 1-2 个 IO 等待）
if [ "$d_count" -ge 3 ]; then
    echo "[WARN] 检测到 $d_count 个 D 状态进程，WSL2 内核疑似死锁污染"
    exit 8
fi
echo "[OK] WSL 内核健康 (D 状态进程 $d_count 个)"
exit 0
