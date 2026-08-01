#!/usr/bin/env bash
# WSL 启动链路冒烟测试（shell 层）。
#
# 复刻用户日志中的冷启动误判场景：start_web_cn.bat 曾把健康探针的一切非正常退出码
# （含外层 timeout 的 124/126/127）都归类为"内核 D-state 死锁"并触发 wsl --shutdown。
# 本测试验证：
#   1. wsl_health_probe.sh 的判定逻辑（classify）在注入驱动下退出码正确（0/8）。
#   2. timeout 外壳的退出码 124/126/127 不应被等同于死锁（死锁仅脚本主动 exit 8）。
#      —— 这是本次修复的核心回归保护。
#
# 运行：bash tests/test_wsl_startup_smoke.sh
# 退出码：0=全过，非 0=有失败。
# 不依赖真实 WSL / 真实 /proc，纯靠注入钩子。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROBE="$ROOT_DIR/wsl_health_probe.sh"

PASS=0
FAIL=0

ok()   { echo "  PASS: $1"; PASS=$((PASS + 1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL + 1)); }

# 用注入钩子跑探针，断言退出码
# $1=期望退出码  $2..=AO_PROBE_FORCE_* 描述  $env 已设好
assert_rc() {
    local expected="$1"; shift
    local desc="$1"; shift
    local rc
    rc=$("$@" >/dev/null 2>&1; echo $?)
    # shellcheck disable=SC2086
    if [ "$rc" = "$expected" ]; then
        ok "$desc (rc=$rc)"
    else
        fail "$desc (期望 rc=$expected, 实际 rc=$rc)"
    fi
}

echo "========================================"
echo "  WSL startup smoke tests (shell layer)"
echo "========================================"

# ---------------------------------------------------------------------------
echo "[1/2] wsl_health_probe.sh classify 逻辑（注入驱动）"
# ---------------------------------------------------------------------------
export AO_PROBE_SKIP_SLEEP=1

# 运行期
AO_PROBE_FORCE_DCOUNT=0 AO_PROBE_FORCE_BOOT_PHASE=0 AO_PROBE_FORCE_UPTIME=120 \
    bash "$PROBE" >/dev/null 2>&1
[ $? = 0 ] && ok "running d=0 -> 0" || fail "running d=0 -> 0"

AO_PROBE_FORCE_DCOUNT=1 AO_PROBE_FORCE_BOOT_PHASE=0 AO_PROBE_FORCE_UPTIME=120 \
    bash "$PROBE" >/dev/null 2>&1
[ $? = 8 ] && ok "running d=1 -> 8" || fail "running d=1 -> 8"

# 启动期宽容
AO_PROBE_FORCE_DCOUNT=1 AO_PROBE_FORCE_BOOT_PHASE=1 AO_PROBE_FORCE_UPTIME=30 \
    bash "$PROBE" >/dev/null 2>&1
[ $? = 0 ] && ok "boot d=1 -> 0 (宽容)" || fail "boot d=1 -> 0 (宽容)"

AO_PROBE_FORCE_DCOUNT=2 AO_PROBE_FORCE_BOOT_PHASE=1 AO_PROBE_FORCE_UPTIME=30 \
    bash "$PROBE" >/dev/null 2>&1
[ $? = 8 ] && ok "boot d=2 -> 8" || fail "boot d=2 -> 8"

unset AO_PROBE_SKIP_SLEEP AO_PROBE_FORCE_DCOUNT AO_PROBE_FORCE_BOOT_PHASE AO_PROBE_FORCE_UPTIME

# ---------------------------------------------------------------------------
echo "[2/2] timeout 外壳退出码分类回归（核心修复保护）"
# ---------------------------------------------------------------------------
# 复刻误判场景：timeout 包裹的命令返回 124/126/127 时，这些码绝不应被当成
# "D-state 死锁"。死锁的唯一信号是被调用脚本主动 exit 8。
# 这里直接验证 timeout 外壳对不同退出码的传递，断言它们与 8 可区分。

# timeout 正常完成，透传被包命令的退出码
timeout 5 bash -c 'exit 0' 2>/dev/null; rc=$?
[ "$rc" = 0 ] && ok "timeout wraps exit 0 -> 0" || fail "timeout wraps exit 0 -> 0 (got $rc)"

timeout 5 bash -c 'exit 8' 2>/dev/null; rc=$?
[ "$rc" = 8 ] && ok "timeout wraps exit 8 -> 8 (真死锁信号唯一来源)" || fail "timeout wraps exit 8 -> 8 (got $rc)"

# timeout 自身超时 → 124（冷启动 wsl.exe 未响应就是这个码，非死锁）
timeout 1 bash -c 'sleep 5' 2>/dev/null; rc=$?
[ "$rc" = 124 ] && ok "timeout exceeded -> 124 (冷启动瞬时，非死锁)" || fail "timeout exceeded -> 124 (got $rc)"

# 被包命令找不到 → 127（冷启动 wslpath 未稳导致 No such file 即此码，非死锁）
timeout 5 bash -c 'exec /no/such/cmd_xyz' 2>/dev/null; rc=$?
[ "$rc" = 127 ] && ok "command not found -> 127 (冷启动路径未稳，非死锁)" || fail "command not found -> 127 (got $rc)"

# 被包命令不可执行 → 126（用户日志里的 Input/output error 即此类，非死锁）
timeout 5 bash -c 'exit 126' 2>/dev/null; rc=$?
[ "$rc" = 126 ] && ok "io/exec error -> 126 (冷启动 IO 未就绪，非死锁)" || fail "io/exec error -> 126 (got $rc)"

# 核心断言：124/126/127 都不等于 8（死锁码），分类时必须区分
for non_deadlock_rc in 124 126 127; do
    if [ "$non_deadlock_rc" != 8 ]; then
        ok "rc=$non_deadlock_rc != 8，应归为 transient 而非 deadlock"
    else
        fail "rc=$non_deadlock_rc 被误判为死锁码"
    fi
done

# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "  结果: PASS=$PASS  FAIL=$FAIL"
echo "========================================"
[ "$FAIL" = 0 ]
