#!/usr/bin/env bash
# AgentOS AVD 自动回归（Phase 1 G-M2 车道，方案 §Phase1 1.0 车道表）。
#
# 设备口径（2026-10-14 起）：MuMu 模拟器 nx 版——MuMuManager.exe CLI 全程免 GUI
# （create/setting/control），Android 15/x86_64，adb 127.0.0.1:<port>（实例 0=16384，
# 端口经 MuMuManager info 动态解析）。实例 0 是 P0.2 驻留现场（可复跑 REST 流），
# 本车道默认要求显式 --mumu-index 且**勿动实例 0**（测试用新实例）。
#
# 步骤链：boot → install → seed_kernel → health → login → proot_boot → message →
# restart_recovery → cancel（取消传播：流中断+终帧+follow-up 立发，R1 同刀）→
# task_run（完整任务执行链：建任务→轮询 completed→产物落盘+面板 API，R2 同刀）→
# tool_loop（agentic loop：REST 发依赖链消息→断言 role=tool 序列+设备侧文件链+
# 终文含关键串，R6 同刀）→ idle_firstcall（idle 回收后首调：GC sweep 取证+首调
# ≤阈值+宿主账完整+配置还原，R7 同刀）→ approval（审批流：default 档+写区授权
# 卡在账+API 批准 permanent+文件落盘+名单还原，R8 同刀）→ upgrade（覆盖安装
# 升级：install -r→versionCode 单调+数据保全断言（凭据/探针会话/.env 键数[新位，
# 旧布局基包升前快照回退旧位取数]/任务产物 marker 升前写升后读新位）+种子对账
# 路径断言，R9 同刀）→ network_recovery（断网恢复：断公网出站→断网态对话如实
# 降级不假死+内核存活+恢复后 provider 自愈真回复+断网轮不幽灵补发，精细测试
# 队列④车道化；2026-10-16 MuMu 实跑过=断言 a/b/c 绿+断言 d 红=失败轮不落库
# 立案）→ model_switch（多
# 会话模型切换：REST 配置面 defaults.chat+tiers 齐切 A→B→新会话生效（会话账
# llm_model=B）+设备 user llm.yaml 文件面 tiers 齐切（坑㊵ 契约）+旧会话跟随
# 新默认语义+失效模型探针不假死+切回 A 恢复+快照测后还原，精细测试队列⑥车道
# 化；2026-10-16 首实跑 FAIL=车道脚本两缺陷立案（bad/switchback PUT etag 未刷新
# 409×2+旧会话断言 pid-差集法结构性不可判），五断言 a/b/c 过）→
# screen_keepalive（息屏保活：KEYCODE_POWER 息屏（dumpsys power 双字段生效判据）
# →health 轮询不停服+息屏期任务推进+亮屏恢复真回复 degraded=false+FGS 现状取证
# [只记录不断言]；机械面车道，真机 Doze/电流定谳挂账须真机轮，精细测试队列⑦
# 车道化）。
# 每步独立可跑（--step），输出结构化结果行（RESULT step=... status=PASS|FAIL
# evidence="..."），全链 --all 汇总退出码。
#
# 前置口径（如实记录）：seed_kernel=health 的自足前置——P0.1 冒烟口径（kernel
# x86_64-linux-android + config/ 推 /data/local/tmp 原生直跑，非 proot）；proot
# 产品化产物（APK+rootfs 种子首启解压）就绪是 G-M2 后半（message/
# restart_recovery）的转绿条件，当前占位不假绿。
#
# 用法：
#   scripts/avd_regression.sh --list
#   scripts/avd_regression.sh --mumu-index 1 --step boot
#   scripts/avd_regression.sh --mumu-index 1 --step install \
#       --apk release/mobile/x86_64/apk/app-debug.apk
#   scripts/avd_regression.sh --mumu-index 1 --step seed_kernel \
#       --kernel-path release/mobile/x86_64/kernel/agentos-kernel
#   scripts/avd_regression.sh --mumu-index 1 --step health
#
# 关键参数（均可用环境变量带默认）：--mumu-index(AGENTOS_MUMU_INDEX)
#   --manager(AGENTOS_MUMU_MANAGER) --serial(AGENTOS_AVD_SERIAL，多机时显式钉)
#   --apk(AGENTOS_AVD_APK) --kernel-path(AGENTOS_KERNEL_BIN) --package(默认
#   com.lingxi.agentos) --port(内核端口默认 9100) --user/--password
#   (AGENTOS_KERNEL_USER/PASS) --timeout-s(BOOT 轮询上限默认 180)
#   --net-recovery-chat-timeout-s(断网态单轮 chat 返回上限默认 120)
#   --net-recovery-recover-wait-s(恢复动作后每轮等待默认 15，探针确认循环 4 轮)
#   --net-recovery-probe-ip(断网对照探针默认 223.5.5.5；8.8.8.8 境内常不可达
#   防假阴性故不取默认)
#   --model-switch-chat-timeout-s(model_switch 单轮 chat 同步上限默认 300)
#   --model-a/--model-b(切换对；A 缺省=切换前 defaults.chat 现值快照，B 缺省
#   minimax-m3.1-flash-preview；须为设备 models 映射真实键，大小写敏感——
#   ae3525b13 教训)
#   --model-bad(失效模型探针名，默认 nonexistent-model，须为映射外名字)
#   --keepalive-screen-off-s(screen_keepalive 息屏窗口默认 75s；60-90s 机械面
#   覆盖窗——真机 Doze/电流定谳挂账须真机轮，不属本车道)
#   --keepalive-health-polls/--keepalive-health-interval-s(息屏期 health 轮询
#   默认 4 轮×15s=60s 不停服覆盖窗)
#   --keepalive-task-timeout-s(亮屏后任务终态轮询上限默认 300s)
#   --keepalive-chat-timeout-s(亮屏后恢复对话同步上限默认 180s)
#
# 现状（2026-10-16）：前十四步已 MuMu 实测绿（R13 十步/R15 tool_loop/R17
# upgrade/R20 upgrade 跨种子转绿，轮次证据见 docs/working/CI车道_台账.md）；
# network_recovery 第 15 步实跑过（R22，断言 d 红=缺陷立案）+model_switch 第
# 16 步首实跑 FAIL（R22，车道脚本两缺陷立案，修复后重跑判达标）；第 17 步
# screen_keepalive=脚本先行落地（2026-10-16 静态验证过，MuMu 实跑待空档；真机
# Doze/电流定谳挂账）。（跑前须先过 proot_boot 产品化链+已配 key）。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SDK_ROOT="${ANDROID_SDK_ROOT:-$REPO_ROOT/.zctmp/tools/android-sdk}"
export JAVA_HOME="${JAVA_HOME:-$REPO_ROOT/.zctmp/tools/jdk21/jdk}" # 嵌套 jdk/ 子目录（现役配方）

MUMU_MANAGER="${AGENTOS_MUMU_MANAGER:-/c/Program Files/Netease/MuMu/nx_main/MuMuManager.exe}"
MUMU_INDEX="${AGENTOS_MUMU_INDEX:-}"
SERIAL="${AGENTOS_AVD_SERIAL:-}"
APK="${AGENTOS_AVD_APK:-}"
KERNEL_BIN_SRC="${AGENTOS_KERNEL_BIN:-$REPO_ROOT/release/mobile/x86_64/kernel/agentos-kernel}"
PACKAGE="com.lingxi.agentos"
KERNEL_PORT="${AGENTOS_KERNEL_PORT:-9100}"
KERNEL_USER="${AGENTOS_KERNEL_USER:-}"
KERNEL_PASS="${AGENTOS_KERNEL_PASS:-}"
BOOT_TIMEOUT_S=180
DEVICE_DIR="/data/local/tmp/gm2"
MODE="list"
STEPS_SELECTED=()
RECOVERY_TIMEOUT_S=180   # 复启后 health 恢复等待（轮数，每轮 2s）
PROOT_BOOT_TIMEOUT_S=900 # 产品化链拉起等待上限（首启含 1.5GB 级解压+装配，每轮 2s）
LAST_SESSION_ID=""       # message → restart_recovery 同进程链（--all）时传递
TASK_RUN_TIMEOUT_S=300   # task_run 终态轮询上限（LLM 多轮+评估闸门；RPM 限流窗内会拉长）
TOOL_LOOP_TIMEOUT_S=300  # tool_loop 单轮 REST chat 同步等待上限（多轮工具循环）
APPROVAL_TIMEOUT_S=300   # approval 单轮 REST chat 同步等待上限（审批卡+两轮 LLM）
IDLE_TIMEOUT_SECS=15     # idle_firstcall：设备 .env 注入的空闲回收阈值（15s+30s 扫描=~45s 可等）
IDLE_FIRSTCALL_TIMEOUT_S=10 # idle_firstcall：回收后首调总耗时断言上界（3#8 预期；LLM TTFB 大时经参数放宽）
IDLE_EXTRA_WAIT_S=40     # idle_firstcall：静置时长=阈值+此余量（30s GC 扫描周期+裕度）
RESTART_TIMEOUT_S=120    # idle_firstcall：步内两次产品化链重启的 health 等待轮数
UPGRADE_BOOT_TIMEOUT_S=300 # upgrade：install -r 后 health 等待上限（跨种子含重解包+重装配，每轮 2s）
NETRECOVERY_CHAT_TIMEOUT_S=120  # network_recovery：断网态单轮 chat 返回上限（如实降级须在 provider 超时内返回，不假死）
NETRECOVERY_RECOVER_WAIT_S=15   # network_recovery：恢复动作后每轮等待（探针确认循环 4 轮=NAT/路由重建余量）
NETRECOVERY_PROBE_IP="223.5.5.5" # network_recovery：断网对照探针（公共 DNS，境内稳定；参数可换）
NET_RESTORE_CMDS=()      # network_recovery 恢复命令栈（全局：trap EXIT 兜底触发时步内 local 已销毁）
NET_RESTORE_SERIAL=""    # 同上：恢复目标 serial（全局）
MODELSWITCH_CHAT_TIMEOUT_S=300 # model_switch：单轮 REST chat 同步等待上限（LLM 真调用，RPM 节奏会拉长）
MODELSWITCH_MODEL_A="${AGENTOS_MODEL_A:-}" # 切换源档；空=取切换前 defaults.chat 现值快照（参数化逃生口）
MODELSWITCH_MODEL_B="${AGENTOS_MODEL_B:-minimax-m3.1-flash-preview}" # 切换目标档（须为设备 models 映射真实键，大小写敏感）
MODELSWITCH_BAD_MODEL="${AGENTOS_MODEL_BAD:-nonexistent-model}" # 失效模型探针名（须为映射外名字）
MSW_PY=""                # model_switch：宿主 python（步内探测后落全局；msw_py/trap 兜底消费）
MSW_RESTORE_TOK=""       # model_switch：还原用 token（trap EXIT 兜底触发时步内 local 已销毁）
MSW_SNAPSHOT_FILE=""     # 同上：快照文件路径（GET 掩码视图全量 data+etag）
KEEPALIVE_SCREEN_OFF_S="${AGENTOS_KEEPALIVE_SCREEN_OFF_S:-75}" # screen_keepalive：息屏窗口时长（60-90s 机械面覆盖窗；真机 Doze/电流定谳挂账）
KEEPALIVE_HEALTH_POLLS="${AGENTOS_KEEPALIVE_HEALTH_POLLS:-4}" # screen_keepalive：息屏期 health 轮询次数（不停服覆盖面）
KEEPALIVE_HEALTH_INTERVAL_S="${AGENTOS_KEEPALIVE_HEALTH_INTERVAL_S:-15}" # screen_keepalive：息屏期 health 轮询间隔
KEEPALIVE_TASK_TIMEOUT_S="${AGENTOS_KEEPALIVE_TASK_TIMEOUT_S:-300}" # screen_keepalive：亮屏后任务终态轮询上限（LLM 多轮+评估闸门）
KEEPALIVE_CHAT_TIMEOUT_S="${AGENTOS_KEEPALIVE_CHAT_TIMEOUT_S:-180}" # screen_keepalive：亮屏后恢复对话同步上限（LLM 真调用+首调 respawn 缝）
KEEPALIVE_RESTORE_SERIAL="" # screen_keepalive：醒屏兜底目标 serial（trap EXIT 兜底触发时步内 local 已销毁）
KA_PW_LAST=""            # screen_keepalive：dumpsys power 双字段摘要（步内副产，evidence 用）

ADB="$SDK_ROOT/platform-tools/adb"
BASE_URL="http://127.0.0.1:$KERNEL_PORT"

PASS_COUNT=0
FAIL_COUNT=0

usage() {
    sed -n '2,74p' "$0" | sed 's/^# \{0,1\}//'
}

log() { echo "[avd-reg] $*"; }

# 结构化结果行：RESULT 行供机器解析，evidence 同步落 stderr 供人看
emit_result() {
    local step="$1" status="$2" evidence="$3"
    echo "RESULT step=$step status=$status evidence=\"$evidence\""
    if [[ "$status" == "PASS" ]]; then
        PASS_COUNT=$((PASS_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "[avd-reg][FAIL] $step: $evidence" >&2
    fi
}

die() {
    echo "[avd-reg][ERROR] $*" >&2
    exit 2
}

require_adb() {
    [[ -x "$ADB" || -x "$ADB.exe" ]] || die "adb 不存在: $ADB（便携 SDK 缺失，见 .zctmp/tools）"
}

require_mumu() {
    [[ -f "$MUMU_MANAGER" ]] || { emit_result "$1" FAIL "MuMuManager 不存在: $MUMU_MANAGER"; return 1; }
    [[ -n "$MUMU_INDEX" ]] || {
        emit_result "$1" FAIL "缺 --mumu-index/AGENTOS_MUMU_INDEX（MuMu 实例号；实例 0 为 P0.2 驻留现场，测试用新实例）"
        return 1
    }
}

mumu_adb_port() {
    "$MUMU_MANAGER" info -v "$MUMU_INDEX" 2>/dev/null \
        | grep -oE '"adb_port"[[:space:]]*:[[:space:]]*[0-9]+' \
        | grep -oE '[0-9]+' | head -1
}

resolve_serial() {
    if [[ -n "$SERIAL" ]]; then
        echo "$SERIAL"
        return 0
    fi
    if [[ -n "$MUMU_INDEX" && -f "$MUMU_MANAGER" ]]; then
        local port
        port="$(mumu_adb_port)"
        if [[ -n "$port" ]]; then
            "$ADB" connect "127.0.0.1:$port" >/dev/null 2>&1
            echo "127.0.0.1:$port"
            return 0
        fi
    fi
    require_adb
    local online
    online="$(adb_t 15 devices | awk 'NR>1 && $2=="device" {print $1}' | head -1)"
    [[ -n "$online" ]] || die "未指定 --serial 且无在线设备"
    echo "$online"
}

# adb client 在瞬断通道上无超时阻塞（实测挂死并继承调用方 stdout 管道）——
# 一律经 timeout 包裹（-k 兜底强杀）；adb_t <timeout-s> <adb args...>
adb_t() {
    local t="$1"
    shift
    timeout -k 5 "$t" "$ADB" "$@"
}

# adb push/shell 的设备绝对路径会被 Git Bash MSYS 改写为宿主盘路径（坑④：
# push 假成功 "1 file pushed" 但设备上无文件）——设备路径命令一律带本守卫。
adb_device_cmd() {
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' adb_t "${ADB_TIMEOUT_S:-120}" "$@"
}

# MuMu 通道会瞬断（offline 假阴性：shell 静默返空，pidof/health 轮询全灭——
# 实测内核实驻留 pidof 可见而轮询判死）。探活+重连是所有轮询步的公共前置。
adb_is_alive() { # $1 = serial
    local pong
    pong="$("$ADB" -s "$1" shell echo ok 2>/dev/null | tr -d '\r\n')"
    [[ "$pong" == "ok" ]]
}

adb_reconnect() { # $1 = serial（127.0.0.1:* 形态；usb 形态 disconnect 报错可容忍）
    adb_t 15 -s "$1" disconnect >/dev/null 2>&1
    [[ "$1" == 127.0.0.1:* ]] && adb_t 15 connect "$1" >/dev/null 2>&1
    sleep 1
}

# ── 步骤 ─────────────────────────────────────────────────────────────────

step_boot() {
    require_mumu boot || return 1
    require_adb
    local port
    port="$(mumu_adb_port)"
    if [[ -z "$port" ]]; then
        log "boot: 实例 $MUMU_INDEX 不存在，create -ver auto ..."
        if ! "$MUMU_MANAGER" create -v "$MUMU_INDEX" -ver auto; then
            emit_result boot FAIL "MuMu create -v $MUMU_INDEX 失败"
            return 1
        fi
    fi
    # root 直通（P0.2 配方：adb root 直通，uid=0；best-effort 不作 boot 闸门）
    if "$MUMU_MANAGER" setting -v "$MUMU_INDEX" -k root_permission -val true >/dev/null 2>&1; then
        log "boot: root_permission=true"
    else
        log "boot: root_permission 设置失败（best-effort，不阻塞）"
    fi
    log "boot: control launch 实例 $MUMU_INDEX ..."
    if ! "$MUMU_MANAGER" control -v "$MUMU_INDEX" launch >/dev/null 2>&1; then
        emit_result boot FAIL "MuMu control launch 失败（实例 $MUMU_INDEX）"
        return 1
    fi
    local i done_flag
    for ((i = 1; i <= BOOT_TIMEOUT_S; i++)); do
        if [[ -z "$port" ]]; then
            port="$(mumu_adb_port)"
        fi
        if [[ -n "$port" ]]; then
            # player 可能整进程死亡（MuMu 不稳定实测）：info 说没起就周期性重拉
            # （launch 对已起实例幂等；限频防刷）
            if (( i >= 10 && i % 10 == 0 )); then
                local started
                started="$("$MUMU_MANAGER" info -v "$MUMU_INDEX" 2>/dev/null | grep -c '"is_android_started": true')"
                if [[ "$started" -eq 0 ]]; then
                    log "boot: info 报 is_android_started=false，重发 control launch"
                    "$MUMU_MANAGER" control -v "$MUMU_INDEX" launch >/dev/null 2>&1
                fi
            fi
            "$ADB" connect "127.0.0.1:$port" >/dev/null 2>&1
            done_flag="$("$ADB" -s "127.0.0.1:$port" shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')"
            if [[ "$done_flag" == "1" ]]; then
                emit_result boot PASS "MuMu 实例 $MUMU_INDEX adb=127.0.0.1:$port sys.boot_completed=1（${i}s）"
                return 0
            fi
        fi
        sleep 1
    done
    emit_result boot FAIL "boot 超时（${BOOT_TIMEOUT_S}s，adb port=${port:-未出现}）"
    return 1
}

step_install() {
    [[ -n "$APK" ]] || { emit_result install FAIL "缺 --apk/AGENTOS_AVD_APK"; return 1; }
    [[ -f "$APK" ]] || { emit_result install FAIL "APK 不存在: $APK"; return 1; }
    local serial
    serial="$(resolve_serial)" || { emit_result install FAIL "无可用设备"; return 1; }
    require_adb
    if adb_t 300 -s "$serial" install -r "$APK" >/dev/null 2>&1; then
        emit_result install PASS "adb install -r 成功（$APK -> $serial）"
        return 0
    fi
    emit_result install FAIL "adb install 失败（见 adb 输出）"
    return 1
}

# health 的自足前置：P0.1 冒烟口径——kernel（x86_64-linux-android）+ config/ 推
# /data/local/tmp 原生直跑（非 proot）。幂等：先杀驻留内核再播再起。
# 口令/令牌为本实例一次性冒烟值（stdout 可见，不落台账不入仓）。
step_seed_kernel() {
    local serial
    serial="$(resolve_serial)" || { emit_result seed_kernel FAIL "无可用设备"; return 1; }
    require_adb
    if [[ ! -f "$KERNEL_BIN_SRC" ]]; then
        emit_result seed_kernel FAIL "内核二进制不存在: $KERNEL_BIN_SRC（先跑 deploy/build_mobile.sh --arch x86_64）"
        return 1
    fi
    if [[ ! -d "$REPO_ROOT/config" ]]; then
        emit_result seed_kernel FAIL "config/ 缺失: $REPO_ROOT/config"
        return 1
    fi
    local seed_dir="$REPO_ROOT/release/mobile/avd_seed"
    local payload="$seed_dir/payload"
    rm -rf "$payload"
    mkdir -p "$payload"
    cp -f "$KERNEL_BIN_SRC" "$payload/agentos-kernel"
    cp -r "$REPO_ROOT/config" "$payload/config"
    local tarball="$seed_dir/kernel_seed.tar.gz"
    (cd "$payload" && tar -czf "$tarball" agentos-kernel config)
    # push 源须 Windows 形态（adb.exe 不读 MSYS POSIX 路径；设备目标仍由
    # MSYS_NO_PATHCONV 守卫防改写，坑④双刃的两面各自处理）
    local tarball_win
    tarball_win="$(cygpath -w "$tarball")"
    log "seed_kernel: push $tarball -> $DEVICE_DIR（$serial）"
    adb_device_cmd -s "$serial" push "$tarball_win" /data/local/tmp/gm2_seed.tar.gz >/dev/null || {
        emit_result seed_kernel FAIL "adb push 失败"
        return 1
    }
    adb_device_cmd -s "$serial" shell "mkdir -p $DEVICE_DIR && tar -xzf /data/local/tmp/gm2_seed.tar.gz -C $DEVICE_DIR && chmod 755 $DEVICE_DIR/agentos-kernel" || {
        emit_result seed_kernel FAIL "设备端解包失败"
        return 1
    }
    # 幂等：杀驻留内核（坑：pkill/pgrep -f agentos-kernel 会自匹配携带同串的
    # shell——P0.2 教训，用 pidof 精确匹配）
    local old_pid
    old_pid="$(adb_device_cmd -s "$serial" shell pidof agentos-kernel 2>/dev/null | tr -d '\r')"
    if [[ -n "$old_pid" ]]; then
        adb_device_cmd -s "$serial" shell "kill $old_pid" >/dev/null 2>&1
        sleep 2
        log "seed_kernel: 已杀驻留内核 pid=$old_pid"
    fi
    local pass secret
    pass="gm2-${RANDOM}${RANDOM}"
    secret="$(printf '%s|%s|%s' "$pass" "$serial" "$(date +%s)" | md5sum | cut -c1-32)"
    log "seed_kernel: 启动内核（port=$KERNEL_PORT AGENTOS_ADMIN_PASSWORD=$pass 冒烟一次性值）"
    # stdin 须一并断开：adb shell 等会话内所有持有 stdin/stdout 的后代退出，
    # nohup 只盖 stdout/stderr，缺 </dev/null 会话即挂死（实测）。
    # 口令注入名对齐 P0.2 实证：AGENTOS_ADMIN_PASSWORD（裸 ADMIN_PASSWORD 不被
    # 读取，内核自动生成随机口令，login 即 400——实测）。
    adb_device_cmd -s "$serial" shell \
        "cd $DEVICE_DIR && AGENTOS_BIND=127.0.0.1 AGENTOS_KERNEL_PORT=$KERNEL_PORT \
AGENTOS_PLUGINS_DIR=$DEVICE_DIR/plugins AGENTOS_CONFIG_ROOT=$DEVICE_DIR/config \
AGENTOS_USER_ROOT=$DEVICE_DIR/user_root TOKEN_SECRET=$secret AGENTOS_ADMIN_PASSWORD=$pass \
nohup ./agentos-kernel >kernel.log 2>&1 </dev/null &"
    local i pid
    for ((i = 1; i <= 45; i++)); do
        pid="$(adb_device_cmd -s "$serial" shell pidof agentos-kernel 2>/dev/null | tr -d '\r')"
        [[ -n "$pid" ]] && break
        # 通道瞬断会静默返空（假阴性实测：内核实驻留而轮询判死）——探活失败即重连
        if ! adb_is_alive "$serial"; then
            log "seed_kernel: adb 通道瞬断，重连"
            adb_reconnect "$serial"
        fi
        sleep 1
    done
    if [[ -n "$pid" ]]; then
        emit_result seed_kernel PASS "内核驻留 pid=$pid port=$KERNEL_PORT（P0.1 原生口径，非 proot）"
        return 0
    fi
    emit_result seed_kernel FAIL "内核未驻留（pidof 30s 无果；设备端 $DEVICE_DIR/kernel.log 可查）"
    return 1
}

# 前置：seed_kernel 已在设备侧起内核（127.0.0.1:$KERNEL_PORT）；host 侧经
# adb forward 直达（host→device 方向=forward；reverse 是 device→host，语义反）
step_health() {
    local serial
    serial="$(resolve_serial)" || { emit_result health FAIL "无可用设备"; return 1; }
    require_adb
    if ! adb_is_alive "$serial"; then
        log "health: adb 通道瞬断，重连"
        adb_reconnect "$serial"
    fi
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" || {
        emit_result health FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local http_code try
    for try in 1 2 3; do
        http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
        [[ "$http_code" == "200" ]] && break
        sleep 2
    done
    if [[ "$http_code" == "200" ]]; then
        emit_result health PASS "GET /health -> 200（$BASE_URL via adb forward <- $serial）"
        return 0
    fi
    emit_result health FAIL "GET /health -> ${http_code:-无响应}（内核未起或 seed_kernel 前置未过）"
    return 1
}

step_login() {
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result login FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS；seed_kernel 日志有冒烟值）"
        return 1
    }
    # TODO(G-M2 接线)：登录响应 token 字段名与随包内核口令注入形态（本机口令自动生成，
    # §5.3）在 proot 产品化时定版；当前仅断言端点 200，不伪造会话语义。
    local serial
    serial="$(resolve_serial)" || { emit_result login FAIL "无可用设备"; return 1; }
    require_adb
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result login FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local body http_code
    body="{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}"
    http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        -X POST -H 'Content-Type: application/json' -d "$body" \
        "$BASE_URL/api/v1/auth/login" 2>/dev/null)"
    if [[ "$http_code" == "200" ]]; then
        emit_result login PASS "POST /api/v1/auth/login -> 200"
        return 0
    fi
    emit_result login FAIL "POST /api/v1/auth/login -> ${http_code:-无响应}"
    return 1
}

# 产品化链拉起（R5）：am start 壳 → KernelHostPlugin 编排（首启解压/离线装配/
# proot 起核/前台服务）→ health 恢复。凭据经 run-as 读壳 SharedPreferences
# （debug 构建专属通道；值为一次性冒烟值，stdout 可见不入台账不入仓）。
step_proot_boot() {
    local serial
    serial="$(resolve_serial)" || { emit_result proot_boot FAIL "无可用设备"; return 1; }
    require_adb
    log "proot_boot: 复启 $PACKAGE（KernelHostPlugin.load 编排本机栈）"
    adb_t 20 -s "$serial" shell         "monkey -p $PACKAGE -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
    local i http_code
    for ((i = 1; i <= PROOT_BOOT_TIMEOUT_S; i++)); do
        if ! adb_is_alive "$serial"; then
            log "proot_boot: adb 通道瞬断，重连"
            adb_reconnect "$serial"
        fi
        adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
        http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
        [[ "$http_code" == "200" ]] && break
        if (( i % 30 == 0 )); then
            log "proot_boot: 等待本机栈 health（${i} 轮，首启含解压+装配属预期）"
        fi
        sleep 2
    done
    if [[ "$http_code" != "200" ]]; then
        emit_result proot_boot FAIL "本机栈 health 未恢复（${PROOT_BOOT_TIMEOUT_S} 轮；壳 logcat tag=LingxiKernelHost）"
        return 1
    fi
    # 凭据提取（run-as，debug 构建限定）
    local creds pass len
    creds="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat shared_prefs/lingxi_kernel.xml 2>/dev/null" | tr -d '
')"
    pass="$(printf '%s' "$creds" | sed -n 's/.*<string name="admin_password">\([^<]*\)<\/string>.*/\1/p' | head -1)"
    if [[ -z "$pass" ]]; then
        emit_result proot_boot FAIL "health 200 但凭据读取失败（run-as/shared_prefs 无 admin_password）"
        return 1
    fi
    len="${#pass}"
    log "proot_boot: CRED admin=admin password=$pass（一次性冒烟值，不入台账不入仓）"
    emit_result proot_boot PASS "health 200（$((i * 2))s，port=$KERNEL_PORT 产品化链）；凭据已取得（len=$len）"
    return 0
}

step_message() {
    # G-M2 后半接线（R5）：消息往返=P0.2 出口判据的自动化形态——REST 登录 →
    # 建会话 → POST /api/v1/chat → 断言回复文本到达。前置=proot_boot（产品化
    # 栈拉起）或 seed_kernel（P0.1 原生口径）其一在 $KERNEL_PORT 驻留。
    # 凭据：--user/--password 显式给（产品化链=壳 SharedPreferences 经 run-as 读，
    # 见 proot_boot 步输出）；LLM 走真 provider（MuMu NAT 直连，config 随种子）。
    local serial
    serial="$(resolve_serial)" || { emit_result message FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result message FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result message FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local login tok
    login="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}")"
    tok="$(printf '%s' "$login" | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result message FAIL "login 失败: $(printf '%s' "$login" | head -c 120)"
        return 1
    }
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result message FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    local chat reply body_file
    # CJK 请求体走临时文件：Windows curl.exe 对命令行参数按 ANSI 代码页收，
    # UTF-8 中文直接进 -d 会被截坏（实测 invalid unicode code point）；
    # 文件路径须 Windows 形态（cygpath 转），MSYS /tmp 路径 curl.exe 读不到
    # （实测空体 EOF）
    body_file="$REPO_ROOT/.zctmp/gm2_body.$$.json"
    printf '{"message":"这是 G-M2 自动回归消息，请只回复一句话确认收到。","session_id":"%s"}' "$sid" \
        > "$body_file"
    chat="$(curl -s --max-time 300 -X POST "$BASE_URL/api/v1/chat" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        --data-binary @"$(cygpath -w "$body_file")")"
    rm -f "$body_file"
    reply="$(printf '%s' "$chat" | head -c 200)"
    if [[ -n "$chat" ]] && printf '%s' "$chat" | grep -q '"content"'; then
        emit_result message PASS "chat 往返 thread=$sid 回复头: $reply"
        LAST_SESSION_ID="$sid"
        return 0
    fi
    emit_result message FAIL "chat 无 content 回复: $reply"
    return 1
}

step_restart_recovery() {
    # G-M2 后半接线（R5）：杀应用重开 → 前台服务重拉本机栈 → 会话状态无损。
    # 断言三面：杀后 am start 复启；health 恢复 200；重启前创建的会话仍在
    # （GET /api/v1/sessions 含 LAST_SESSION_ID——SQLite 随 rootfs 落盘）。
    local serial
    serial="$(resolve_serial)" || { emit_result restart_recovery FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result restart_recovery FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    local pre_sid="$LAST_SESSION_ID"
    [[ -n "$pre_sid" ]] || {
        emit_result restart_recovery FAIL "无前置会话（先跑 message 步建会话）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
    log "restart_recovery: force-stop $PACKAGE"
    adb_t 20 -s "$serial" shell am force-stop "$PACKAGE" >/dev/null 2>&1
    sleep 3
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    [[ "$code" != "200" ]] || {
        emit_result restart_recovery FAIL "force-stop 后内核仍响应（孤儿进程?）code=$code"
        return 1
    }
    log "restart_recovery: 复启 $PACKAGE（前台服务应重拉本机栈）"
    adb_t 20 -s "$serial" shell \
        "monkey -p $PACKAGE -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
    local i http_code got_sid sess tok
    for ((i = 1; i <= RECOVERY_TIMEOUT_S; i++)); do
        if ! adb_is_alive "$serial"; then
            adb_reconnect "$serial"
        fi
        adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
        http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
        [[ "$http_code" == "200" ]] && break
        sleep 2
    done
    if [[ "$http_code" != "200" ]]; then
        emit_result restart_recovery FAIL "复启后 health 未恢复（${RECOVERY_TIMEOUT_S} 轮，code=${http_code:-无}）"
        return 1
    fi
    tok="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result restart_recovery FAIL "复启后登录失败（凭据漂移?）"
        return 1
    }
    sess="$(curl -s --max-time 15 "$BASE_URL/api/v1/sessions" -H "Authorization: Bearer $tok")"
    got_sid="$(printf '%s' "$sess" | grep -c "$pre_sid")"
    if [[ "$got_sid" -ge 1 ]]; then
        emit_result restart_recovery PASS "force-stop→复启→health 200（$((i * 2))s）登录 200 会话 $pre_sid 在账"
        return 0
    fi
    emit_result restart_recovery FAIL "会话 $pre_sid 复启后不可见（状态丢失）"
    return 1
}

# G-M2 第 9 步（R1 移动版精细测试同刀）：取消传播自动化。方案影响矩阵"取消"行
# （中断→立即断流→可继续下一轮+信号量归还无泄漏）的车道化：REST 登录/建会话 →
# WS user_input 流式 → 首 text_delta 后发 stop_generation → 断言断流（取消后
# 不再出现 text_delta 越窗）+ interrupted 终帧 + follow-up 立发正常完成；总耗时
# 有上界（python 内 130s deadline + bash timeout 150s）。
# 断言取 WS 事件面：REST 消息面 status 冷路径与 WS 不同构（kernel
# session_routes.rs:737 回退 completed，blob 实为 interrupted——2026-10-15 R1
# 缺陷在案，修复前 REST status 断言会误红，勿在此断言）。
# 前置：$KERNEL_PORT 有驻留内核（proot_boot 产品化链或 seed_kernel 原生链）
# 且已配 key（message 步同前置）；宿主 python 带 websocket-client。
step_cancel() {
    local serial
    serial="$(resolve_serial)" || { emit_result cancel FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result cancel FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    local py_bin=""
    for cand in python python3 py; do
        if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import websocket" >/dev/null 2>&1; then
            py_bin="$cand"
            break
        fi
    done
    [[ -n "$py_bin" ]] || {
        emit_result cancel FAIL "宿主 python 缺 websocket-client（pip install websocket-client）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result cancel FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local login tok
    login="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}")"
    tok="$(printf '%s' "$login" | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result cancel FAIL "login 失败: $(printf '%s' "$login" | head -c 120)"
        return 1
    }
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result cancel FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    # WS 时序断言经 python（超时上界 150s：取消轮+follow-up 轮+余量）
    local out
    out="$(PORT="$KERNEL_PORT" TOKEN="$tok" SID="$sid" timeout -k 5 150 "$py_bin" - << 'PYEOF'
import json, os, sys, time
import websocket

port, token, sid = os.environ["PORT"], os.environ["TOKEN"], os.environ["SID"]
ws = websocket.create_connection(
    f"ws://127.0.0.1:{port}/ws/chat?token={token}&version=3.0.0", timeout=60
)
t0 = time.time()


def send(msg):
    ws.send(json.dumps(msg))


def run_turn(content, cancel_after_delta=None):
    """发一轮；cancel_after_delta=N 时在第 N 个 text_delta 后发 stop_generation。
    返回 {cancelled, delta_after_cancel, msg_status, round_finished, failed}。"""
    r = {"cancelled": False, "delta_after_cancel": None,
         "msg_status": None, "round_finished": None, "failed": None}
    send({"type": "user_input", "thread_id": sid, "content": content,
          "pipeline_id": "", "attachments": [], "enable_thinking": False,
          "thinking_strength": "", "client_message_id": f"gm2-cancel-{time.time()}"})
    deltas = 0
    deadline = t0 + 130
    while time.time() < deadline:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            break
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        et = ev.get("type", "?")
        d = ev.get("data", {}) or {}
        if et == "text_delta":
            deltas += 1
            if r["cancelled"]:
                r["delta_after_cancel"] = time.time() - cancel_sent_at
            if cancel_after_delta is not None and deltas >= cancel_after_delta and not r["cancelled"]:
                send({"type": "stop_generation", "thread_id": sid,
                      "reason": "gm2-cancel", "pipeline_id": ""})
                r["cancelled"] = True
                cancel_sent_at = time.time()
        elif et == "new_message":
            m = d.get("message", {}) or {}
            if m.get("role") == "assistant":
                r["msg_status"] = m.get("status")
        elif et == "pipeline_round_finished":
            r["round_finished"] = time.time() - t0
            r["failed"] = d.get("failed")
            break
    return r


cancel_sent_at = 0.0
try:
    cancel_turn = run_turn(
        "请从1数到300，每个数字一行，不要用任何工具，直接输出。", cancel_after_delta=1)
    followup = run_turn("再从300数到400，每个数字一行，不要用工具。")
finally:
    ws.close()

problems = []
if not cancel_turn["cancelled"]:
    problems.append("cancel-never-sent")
if cancel_turn["msg_status"] != "interrupted":
    problems.append(f"cancel-status={cancel_turn['msg_status']}")
if cancel_turn["round_finished"] is None:
    problems.append("cancel-no-round-finish")
if not followup["round_finished"] or followup["failed"]:
    problems.append(f"followup-finish={followup['round_finished']} failed={followup['failed']}")
if followup["msg_status"] != "completed":
    problems.append(f"followup-status={followup['msg_status']}")
total = time.time() - t0
print(f"CANCEL-DETAIL cancel={json.dumps(cancel_turn)} followup={json.dumps(followup)} total_s={total:.1f}")
if problems:
    print(f"CANCEL-FAIL {';'.join(problems)}")
    sys.exit(1)
print(f"CANCEL-OK total_s={total:.1f}")
PYEOF
)"
    local detail ok_line
    detail="$(printf '%s' "$out" | grep -a '^CANCEL-DETAIL' | head -1)"
    if printf '%s' "$out" | grep -aq '^CANCEL-OK'; then
        ok_line="$(printf '%s' "$out" | grep -a '^CANCEL-OK' | head -1)"
        emit_result cancel PASS "取消传播全链绿（${ok_line#CANCEL-OK }）${detail:+ $detail}"
        return 0
    fi
    emit_result cancel FAIL "取消传播断言红: $(printf '%s' "$out" | grep -a '^CANCEL-FAIL' | head -1) ${detail:+ $detail}"
    return 1
}

# G-M2 第 10 步（R2 移动版精细测试同刀）：完整任务执行链自动化。REST 建会话+
# 建根任务（前端面板表单同源路径 POST /ext/task_service/tasks/root，task_id=
# pipeline_id）→ 轮询终态 completed（TASK_RUN_TIMEOUT_S 上限）→ 断言两面：
# ①产物文件在设备工作区真实落盘（run-as 直读 proot rootfs，内容精确匹配）；
# ②任务面板 API 数据可见（GET /ext/task_service/tasks 列表含该任务且 completed）。
#
# 已知缝（2026-10-15 R2 在案，桌面同构代码）：
# - 缺陷E：boot 期宿主握手早于内核 chat.send_message capability 注册（G2 boot
#   校验先拉起全部宿主，chat 注册在启动末期）——净 boot 后首次建任务必报
#   「chat capability 未注入（sidecar 未接线）」。脚本按装机实测同款 Workaround
#   自动 disable→enable task_submit_tool 强制 respawn 换新握手后重试一次。
# - 评估口径：验收标准只带 files（file_check 本地存在性检查）。expect 语义指标
#   的评估子管道经 chat.send_message 派发，移动栈上租户解析失败（缺陷G 在案），
#   带 expect 的任务会卡评估闸门提醒循环不收敛——CI 车道只锁 files 路径。
# 前置：$KERNEL_PORT 有驻留内核（proot_boot 产品化链）+已配 key+task 域插件在位。
step_task_run() {
    local serial
    serial="$(resolve_serial)" || { emit_result task_run FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result task_run FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result task_run FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local login tok
    login="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}")"
    tok="$(printf '%s' "$login" | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result task_run FAIL "login 失败: $(printf '%s' "$login" | head -c 120)"
        return 1
    }
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result task_run FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    # 任务书请求体走临时文件（CJK 经 curl 命令行会被 Windows 代码页截坏，同 message 步）
    local body_file task_resp task_id
    body_file="$REPO_ROOT/.zctmp/gm2_taskrun.$$.json"
    printf '{"title":"G-M2 task_run auto regression","description":"This is a simple direct task. Do it yourself, do NOT delegate to any sub-agent. Goal: create file gm2_task_artifact.txt in your current task workspace with exactly one line: G-M2-TASK-RUN-OK . Then list the workspace directory to confirm the file exists. Finish with task_evaluate and a short report. Do NOT write any long text.","target_type":"agent","target_id":"general_agent_flash","thread_id":"%s","acceptance_criteria":{"files":["gm2_task_artifact.txt"]}}' "$sid" \
        > "$body_file"
    local create_once
    create_once() {
        curl -s --max-time 70 -X POST "$BASE_URL/ext/task_service/tasks/root" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$body_file")"
    }
    task_resp="$(create_once)"
    task_id="$(printf '%s' "$task_resp" | grep -oE '"id": *"[0-9a-f]+"' | head -1 | grep -oE '[0-9a-f]{8,}' | head -1)"
    if [[ -z "$task_id" ]] && printf '%s' "$task_resp" | grep -q "chat capability"; then
        # 缺陷E Workaround（装机实测同款）：respawn task_submit 宿主换新握手
        log "task_run: 首建命中 chat capability 未注入（缺陷E），respawn task_submit_tool 后重试"
        curl -s --max-time 20 -X PUT "$BASE_URL/api/v1/plugins/task_submit_tool/enabled" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            -d '{"enabled":false}' >/dev/null 2>&1
        sleep 3
        curl -s --max-time 30 -X PUT "$BASE_URL/api/v1/plugins/task_submit_tool/enabled" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            -d '{"enabled":true}' >/dev/null 2>&1
        sleep 5
        task_resp="$(create_once)"
        task_id="$(printf '%s' "$task_resp" | grep -oE '"id": *"[0-9a-f]+"' | head -1 | grep -oE '[0-9a-f]{8,}' | head -1)"
    fi
    rm -f "$body_file"
    [[ -n "$task_id" ]] || {
        emit_result task_run FAIL "建任务失败: $(printf '%s' "$task_resp" | head -c 200)"
        return 1
    }
    log "task_run: 任务已出生 task_id=$task_id session=$sid，轮询终态（上限 ${TASK_RUN_TIMEOUT_S}s）"
    local i status detail
    for ((i = 1; i <= TASK_RUN_TIMEOUT_S; i += 5)); do
        sleep 5
        detail="$(curl -s --max-time 10 "$BASE_URL/ext/task_service/tasks/$task_id" \
            -H "Authorization: Bearer $tok" 2>/dev/null)"
        status="$(printf '%s' "$detail" | grep -oE '"status": *"[a-z_]+"' | head -1 | grep -oE '[a-z_]+' | tail -1)"
        case "$status" in
            completed) break ;;
            failed|cancelled|canceled)
                emit_result task_run FAIL "任务终态=$status（非 completed；task_id=$task_id，评估闸门/执行链见内核日志）"
                return 1
            ;;
        esac
        if ! adb_is_alive "$serial"; then
            adb_reconnect "$serial"
        fi
    done
    if [[ "$status" != "completed" ]]; then
        emit_result task_run FAIL "轮询超时（${TASK_RUN_TIMEOUT_S}s）终态=$status task_id=$task_id"
        return 1
    fi
    local elapsed=$((i))
    # 断言①：产物文件设备侧真实落盘（workspace 自任务详情 metadata；R9-A 修复后
    # .ai_workspaces 宿主面=files/user_root（proot bind），不再随种子重解包清除；
    # 找不到再按文件名在 .ai_workspaces 下兜底搜索）
    local ws dev_path artifact
    ws="$(printf '%s' "$detail" | grep -oE '"workspace": *"[^"]*"' | head -1 | sed 's/.*: *"//; s/"$//')"
    dev_path="files/user_root${ws#/opt/agentos}/gm2_task_artifact.txt"
    artifact="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat $dev_path 2>/dev/null" | tr -d '\r\n')"
    if [[ "$artifact" != "G-M2-TASK-RUN-OK" ]]; then
        artifact="$(adb_device_cmd -s "$serial" shell \
            "run-as $PACKAGE find files/user_root/.ai_workspaces -name gm2_task_artifact.txt 2>/dev/null | head -1" | tr -d '\r\n')" \
            && [[ -n "$artifact" ]] \
            && artifact="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat $artifact 2>/dev/null" | tr -d '\r\n')"
    fi
    if [[ "$artifact" != "G-M2-TASK-RUN-OK" ]]; then
        emit_result task_run FAIL "产物文件断言失败（期望 G-M2-TASK-RUN-OK，实得 '${artifact:0:40}'；workspace=${ws:-未知}）"
        return 1
    fi
    # 断言②：任务面板 API 数据可见（前端任务面板数据源同款列表端点）
    local panel hits
    panel="$(curl -s --max-time 15 "$BASE_URL/ext/task_service/tasks?limit=20" -H "Authorization: Bearer $tok")"
    hits="$(printf '%s' "$panel" | grep -c "$task_id")"
    if [[ "$hits" -ge 1 ]] && printf '%s' "$panel" | grep -q '"status": *"completed"'; then
        :
    else
        emit_result task_run FAIL "面板 API 未见任务 $task_id（completed）"
        return 1
    fi
    emit_result task_run PASS "任务全链 completed（${elapsed}s，task_id=$task_id）产物 gm2_task_artifact.txt 内容精确匹配+面板 API 在账"
    return 0
}

# G-M2 第 11 步（R6 移动版精细测试同刀）：agentic loop 自动化——一条消息触发
# 有数据依赖的多步工具链，断言循环真的迭代了且每轮结果真实喂回 LLM：
# ①REST /api/v1/chat 同步发依赖链消息（写 A=BASE-123 → 读 A → 把 A 内容写入
#   B → 读 B 验证 → 回复引用读到的内容）；
# ②终文含关键串 BASE-123（终文引用中间内容=消费了工具结果）；
# ③GET messages 断言 role=tool 块 ≥3（循环深度下界）；
# ④设备侧文件链内容断言（run-as 直读 proot rootfs：A 精确=BASE-123，B 含
#   BASE-123——B 的内容来自 A=依赖链真实传导）。
# 前置：$KERNEL_PORT 有驻留内核（proot_boot 产品化链）+已配 key（message 同前置）。
# 已知观测（R2-H，登记不立案）：循环内某工具插件首次装载会触发组宿主整组重载
# （members N→N+1），在飞调用靠 MCP 重连幸存——脚本不做重载豁免，循环断言以
# 终态为准。
step_tool_loop() {
    local serial
    serial="$(resolve_serial)" || { emit_result tool_loop FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result tool_loop FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result tool_loop FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local login tok
    login="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}")"
    tok="$(printf '%s' "$login" | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result tool_loop FAIL "login 失败: $(printf '%s' "$login" | head -c 120)"
        return 1
    }
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result tool_loop FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    # 依赖链消息走临时文件（CJK 经 curl 命令行会被 Windows 代码页截坏，同 message 步；
    # 文件名带 CI 前缀防与人工轮产物撞名）
    local body_file chat t0 t1 reply
    body_file="$REPO_ROOT/.zctmp/gm2_toolloop.$$.json"
    printf {'"message":"亲自执行、禁委派、禁写作长文。依次执行：①用工具创建文件 chain_tool_loop_a.txt，内容写入 BASE-123；②用工具读取 chain_tool_loop_a.txt，把读到的内容写入 chain_tool_loop_b.txt；③用工具读取 chain_tool_loop_b.txt 验证包含 BASE-123；④把验证结果作为回复告诉我，回复中必须引用你从文件读到的内容。","session_id":"%s"}' "$sid" \
        > "$body_file"
    t0=$(date +%s)
    chat="$(curl -s --max-time "$TOOL_LOOP_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        --data-binary @"$(cygpath -w "$body_file")")"
    t1=$(date +%s)
    rm -f "$body_file"
    if ! printf '%s' "$chat" | grep -q "BASE-123"; then
        emit_result tool_loop FAIL "终文未含 BASE-123（$((t1 - t0))s: $(printf '%s' "$chat" | head -c 160)）"
        return 1
    fi
    # 断言②：role=tool 块 ≥3（循环深度下界；容忍 "role":"tool"/"role": "tool" 两形态）
    local msgs tool_count
    msgs="$(curl -s --max-time 20 "$BASE_URL/api/v1/sessions/$sid/messages" -H "Authorization: Bearer $tok")"
    tool_count="$(printf '%s' "$msgs" | grep -oE '"role": *"tool"' | wc -l)"
    if [[ "$tool_count" -lt 3 ]]; then
        emit_result tool_loop FAIL "role=tool 块仅 ${tool_count} 个（<3，循环未按依赖链迭代）"
        return 1
    fi
    # 断言③：设备侧文件链（找不到再按文件名在 .ai_workspaces 下兜底搜索）
    local ws_dir dev_a a_content b_content
    ws_dir="files/user_root/.ai_workspaces/sessions/$(printf '%s' "$sid" | sed 's/^thread-//; s/-//g' | cut -c1-12)"
    a_content="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat $ws_dir/chain_tool_loop_a.txt 2>/dev/null" | tr -d '\r\n')"
    if [[ "$a_content" != "BASE-123" ]]; then
        a_content="$(adb_device_cmd -s "$serial" shell \
            "run-as $PACKAGE find files/user_root/.ai_workspaces -name chain_tool_loop_a.txt 2>/dev/null | head -1" | tr -d '\r\n')" \
            && [[ -n "$a_content" ]] \
            && a_content="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat $a_content 2>/dev/null" | tr -d '\r\n')"
    fi
    if [[ "$a_content" != "BASE-123" ]]; then
        emit_result tool_loop FAIL "设备侧 chain_tool_loop_a.txt 断言失败（期望 BASE-123，实得 '${a_content:0:40}'）"
        return 1
    fi
    dev_a="$ws_dir/chain_tool_loop_b.txt"
    b_content="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat $dev_a 2>/dev/null" | tr -d '\r\n')"
    if ! [[ "$b_content" == *BASE-123* ]]; then
        b_content="$(adb_device_cmd -s "$serial" shell \
            "run-as $PACKAGE find files/user_root/.ai_workspaces -name chain_tool_loop_b.txt 2>/dev/null | head -1" | tr -d '\r\n')" \
            && [[ -n "$b_content" ]] \
            && b_content="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat $b_content 2>/dev/null" | tr -d '\r\n')"
    fi
    if ! [[ "$b_content" == *BASE-123* ]]; then
        emit_result tool_loop FAIL "设备侧 chain_tool_loop_b.txt 未含 BASE-123（实得 '${b_content:0:40}'，依赖链未传导）"
        return 1
    fi
    emit_result tool_loop PASS "agentic loop 全链绿（$((t1 - t0))s，thread=$sid）role=tool 块=${tool_count}≥3+设备侧文件链 A=BASE-123/B 含 BASE-123+终文引用读到的内容"
    return 0
}

# G-M2 第 12 步（R7 移动版精细测试同刀）：idle 回收后首调——3#8 并行 respawn 的
# 设备口径验收。方案 §3.1 首调税（修前 25.6s=串行懒 respawn 级联 19.6s 主因）+
# §七#3 验收（idle 回收后首调 ≤10s）。
#
# 流程（自包含，跑完即还原）：
#   ①run-as 向设备用户 .env 幂等注入 AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS（内核仅在
#     启动期把 .env 装入进程环境，PUT /api/v1/config/env 对 AGENTOS_* 保留名 422
#     拒写——重启是唯一生效路径，R7 实测在案）；
#   ②force-stop+复启（health 等待）→ 登录 → boot 后首调（对照口径，测量不断言）；
#   ③静置 阈值+IDLE_EXTRA_WAIT_S（GC 每 30s 扫描）→ 设备内核日志取证 GC sweep：
#     有 idle-unloading=回收成立；只有 skipped=in-flight 门钉住（R7 实测可钉
#     5min+），加倍静置重试一轮，仍无则如实 FAIL（不假绿）；
#   ④回收后首调（≤IDLE_FIRSTCALL_TIMEOUT_S 断言）+ 第二调（记录）；
#   ⑤宿主账：python 宿主进程 ≥3 且零僵尸；
#   ⑥还原：.env 移除注入行+复启（防短阈值污染后续轮）。
# 口径注记（R7 实测）：首调总耗时含 LLM TTFB（provider 方差 1-20s+），红时以
# RESULT evidence 的分段数字归因（respawn 段 vs LLM 段）；阈值经
# --idle-firstcall-timeout-s 放宽属预期用法。
step_idle_firstcall() {
    local serial
    serial="$(resolve_serial)" || { emit_result idle_firstcall FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result idle_firstcall FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    local env_line="AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS=${IDLE_TIMEOUT_SECS}"
    local dev_env="files/user_root/.env"
    # ①注入（幂等：已有该键先删行再追加；grep -v 全量重写，不回显文件内容）
    adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -v AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS $dev_env > $dev_env.tmp && mv $dev_env.tmp $dev_env && echo $env_line >> $dev_env && tail -c 80 $dev_env | grep -c AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS'" >/dev/null 2>&1
    local injected
    injected="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'tail -c 80 $dev_env | grep -c AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS'" | tr -d '\r\n ')"
    [[ "$injected" == "1" ]] || {
        emit_result idle_firstcall FAIL "设备 .env 注入失败（run-as 写入或回读未确认）"
        return 1
    }
    # ②重启生效 + 登录
    local restart_ok i http_code tok pass
    restart_kernel_and_wait() {
        adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
        adb_t 20 -s "$serial" shell am force-stop "$PACKAGE" >/dev/null 2>&1
        sleep 3
        adb_t 20 -s "$serial" shell "monkey -p $PACKAGE -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
        http_code=""
        for ((i = 1; i <= RESTART_TIMEOUT_S; i++)); do
            adb_is_alive "$serial" || adb_reconnect "$serial"
            adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
            http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
            [[ "$http_code" == "200" ]] && return 0
            sleep 2
        done
        return 1
    }
    restart_kernel_and_wait || {
        adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -v AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS $dev_env > $dev_env.tmp && mv $dev_env.tmp $dev_env'" >/dev/null 2>&1
        emit_result idle_firstcall FAIL "注入后复启 health 未恢复（${RESTART_TIMEOUT_S} 轮；.env 已尝试还原）"
        return 1
    }
    pass="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat shared_prefs/lingxi_kernel.xml 2>/dev/null" | tr -d '\r' | sed -n 's/.*<string name="admin_password">\([^<]*\)<\/string>.*/\1/p' | head -1)"
    tok="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$pass\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result idle_firstcall FAIL "重启后登录失败"
        return 1
    }
    local sess sid body_file
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result idle_firstcall FAIL "建会话失败"
        return 1
    }
    chat_once() { # $1=消息文本 → stdout=回复体
        body_file="$REPO_ROOT/.zctmp/gm2_idlefc.$$.json"
        printf '{"message":"%s","session_id":"%s"}' "$1" "$sid" > "$body_file"
        curl -s --max-time 120 -X POST "$BASE_URL/api/v1/chat" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$body_file")"
    }
    # boot 后首调（对照口径，测量不断言；同时让链上成员产生 GC 候选记账）
    local t0 t1 resp boot_s
    t0=$(date +%s)
    resp="$(chat_once "重启后首调，只回复两个字：收到。不要使用任何工具。")"
    t1=$(date +%s)
    boot_s=$((t1 - t0))
    printf '%s' "$resp" | grep -q '"content"' || {
        adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -v AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS $dev_env > $dev_env.tmp && mv $dev_env.tmp $dev_env'" >/dev/null 2>&1
        emit_result idle_firstcall FAIL "boot 后首调无回复（$(printf '%s' "$resp" | head -c 120)）"
        return 1
    }
    # ③静置等 GC sweep + 日志取证（threshold 行=回收成立；skipped=in-flight 钉住）
    # 取证窗口从重启后日志行号起算（当日日志含历史轮 GC 行，整文件 grep 会假阳性）
    local dev_log gc_hits sleep_total log_mark
    dev_log="files/rootfs/opt/agentos/logs/kernel.log.$(adb_t 15 -s "$serial" shell date +%Y-%m-%d | tr -d '\r')"
    log_mark="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'wc -l < $dev_log'" | tr -d '\r\n ')"
    gc_hits=""
    for attempt in 1 2; do
        sleep_total=$((IDLE_TIMEOUT_SECS + IDLE_EXTRA_WAIT_S))
        (for ((s = 0; s < sleep_total; s += 10)); do sleep 10; done) &
        local spinner=$!
        # 等待期维持 adb 通道（MuMu 瞬断坑）
        while kill -0 "$spinner" 2>/dev/null; do
            adb_is_alive "$serial" || adb_reconnect "$serial"
            sleep 10
        done
        wait "$spinner"
        gc_hits="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'tail -n +$log_mark $dev_log 2>/dev/null | grep -h idle-unloading | tail -3'" | tr -d '\r')"
        [[ -n "$gc_hits" ]] && break
        log "idle_firstcall: 第 ${attempt} 次静置未见 idle-unloading（in-flight 门可钉住 unload，R7 实测），加倍静置重试"
    done
    # ④回收后首调（断言上界）+ 第二调（记录）
    local t2 t3 first_s resp2 t4 t5 second_s
    t2=$(date +%s)
    resp="$(chat_once "回收后首调，只回复两个字：收到。不要使用任何工具。")"
    t3=$(date +%s)
    first_s=$((t3 - t2))
    resp2="$(chat_once "第二调，只回复两个字：正常。不要使用任何工具。")"
    t4=$(date +%s)
    second_s=$((t4 - t3))
    # ⑤宿主账：python 宿主 ≥3 且零僵尸（懒语义允许未用宿主缺位）
    local proc_account zombies
    proc_account="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'ps -A 2>/dev/null | grep -c python; ps -A 2>/dev/null | grep python | grep -c \" Z \"'" | tr -d '\r')"
    zombies="$(printf '%s\n' "$proc_account" | sed -n '2p')"
    proc_account="$(printf '%s\n' "$proc_account" | sed -n '1p')"
    # ⑥还原（默认 300s 阈值，防短阈值污染后续轮）
    local restored
    adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -v AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS $dev_env > $dev_env.tmp && mv $dev_env.tmp $dev_env'" >/dev/null 2>&1
    restored="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -c AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS $dev_env'" | tr -d '\r\n ')"
    if [[ "$restored" != "0" ]]; then
        emit_result idle_firstcall FAIL "设备 .env 还原失败（AGENTOS_PLUGIN_IDLE_TIMEOUT_SECS 仍残留，须人工清理）"
        return 1
    fi
    restart_kernel_and_wait || log "idle_firstcall: 还原后复启未确认 health（配置已还原，内核随下次启动生效）"
    local gc_note problems=""
    if [[ -n "$gc_hits" ]]; then
        gc_note="GC取证=yes($(printf '%s' "$gc_hits" | tail -1 | grep -oE 'plugin=[a-z_]+ host=[a-z:0-9]+'))"
    else
        gc_note="GC取证=no(in-flight门/无候选)"
        problems+="gc-not-observed;"
    fi
    [[ "$first_s" -le "$IDLE_FIRSTCALL_TIMEOUT_S" ]] || problems+="firstcall=${first_s}s>${IDLE_FIRSTCALL_TIMEOUT_S}s;"
    [[ "$proc_account" -ge 3 ]] || problems+="host-account=${proc_account}<3;"
    [[ "$zombies" == "0" ]] || problems+="zombies=${zombies:-?};"
    printf '%s' "$resp" | grep -q '"content"' || problems+="firstcall-no-content;"
    printf '%s' "$resp2" | grep -q '"content"' || problems+="second-no-content;"
    if [[ -z "$problems" ]]; then
        emit_result idle_firstcall PASS "回收后首调 ${first_s}s≤${IDLE_FIRSTCALL_TIMEOUT_S}s+第二调 ${second_s}s+boot首调对照 ${boot_s}s+${gc_note}+宿主账 python=${proc_account} 零僵尸+阈值已还原默认"
        return 0
    fi
    emit_result idle_firstcall FAIL "idle_firstcall 红: ${problems%;} boot首调=${boot_s}s 第一调=${first_s}s 第二调=${second_s}s ${gc_note} 宿主账=${proc_account}"
    return 1
}

# G-M2 第 13 步（R8 移动版精细测试同刀）：审批流自动化——写区授权卡全链：
# REST 设 default 档 → 发 out-of-zone 文件写（位置闸全档执法，唯一移动端可达
# 审批触发面）→ 断言 pending 在账 → API 批准（永久写入配置）→ 断言工具执行+
# 文件落盘 → 名单还原。
#
# 口径注记（2026-10-15 R8 实测在案）：
# - 「仅本管道」批准的当次派发不携带 authorized_zones 参数（R8-A 在案：批准后
#   写入仍被工具层兜底校验拒绝），故 CI 走「永久写入配置」路径——授权落名单
#   文件，工具层每次调用重读名单，重试轮即可落盘。
# - 批准当次调用仍会失败（R8-A），第一轮以"写失败"收尾属预期；第二轮重试才
#   是断言面。两轮都吃 LLM 真调用（RPM 节奏）。
# - 高风险档（auto/bypass）切换需确认卡；default 为低风险直切。
# 前置：$KERNEL_PORT 有驻留内核（proot_boot 产品化链）+已配 key（message 同前置）。
step_approval() {
    local serial
    serial="$(resolve_serial)" || { emit_result approval FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result approval FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result approval FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    local login tok
    login="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}")"
    tok="$(printf '%s' "$login" | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result approval FAIL "login 失败: $(printf '%s' "$login" | head -c 120)"
        return 1
    }
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result approval FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    # ①default 档显式落表（低风险直切；回读断言 explicit）
    local switched
    switched="$(curl -s --max-time 15 -X POST "$BASE_URL/ext/pipeline_security_check/permission_mode" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        -d "{\"session_id\":\"$sid\",\"mode\":\"default\"}")"
    printf '%s' "$switched" | grep -q '"switched": *true' || {
        emit_result approval FAIL "default 档设置失败: $(printf '%s' "$switched" | head -c 120)"
        return 1
    }
    # ②触发 out-of-zone 文件写（第一轮：预期卡+批准后当次失败（R8-A）属预期）
    local body_file chat1
    body_file="$REPO_ROOT/.zctmp/gm2_approval.$$.json"
    printf '{"message":"请直接调用文件写入工具（不要征求意见、不要改路径），把内容 G-M2-APPROVAL-OK 写入 /opt/agentos/gm2_approval_probe.txt，写完一句话汇报。","session_id":"%s"}' "$sid" \
        > "$body_file"
    chat1="$(curl -s --max-time "$APPROVAL_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        --data-binary @"$(cygpath -w "$body_file")")"
    # ③断言 pending 在账
    local pend rid
    pend="$(curl -s --max-time 10 "$BASE_URL/ext/approval_service/interaction/pending" \
        -H "Authorization: Bearer $tok")"
    rid="$(printf '%s' "$pend" | grep -oE '"id": *"[0-9a-f-]{36}"' | head -1 | grep -oE '[0-9a-f-]{36}')"
    [[ -n "$rid" ]] || {
        emit_result approval FAIL "审批卡未在账（pending 无 36 位 id；chat1 头: $(printf '%s' "$chat1" | head -c 120)）"
        rm -f "$body_file"
        return 1
    }
    # ④API 批准（永久写入配置——授权落名单，工具层重读名单后可落盘）
    local approved
    approved="$(curl -s --max-time 15 -X POST "$BASE_URL/ext/approval_service/interaction/response" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        -d "{\"request_id\":\"$rid\",\"response\":{\"response_type\":\"answered\",\"selected_option\":\"permanent\"}}")"
    printf '%s' "$approved" | grep -q '"success": *true' || {
        emit_result approval FAIL "API 批准失败: $(printf '%s' "$approved" | head -c 120)"
        rm -f "$body_file"
        return 1
    }
    # ⑤重试轮（名单已含 /opt/agentos → 零卡直过 → 文件落盘）
    local chat2
    printf '{"message":"永久授权已通过。请再调用一次文件写入工具，把内容 G-M2-APPROVAL-OK 写入 /opt/agentos/gm2_approval_probe.txt，写完一句话确认文件已创建。","session_id":"%s"}' "$sid" \
        > "$body_file"
    chat2="$(curl -s --max-time "$APPROVAL_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        --data-binary @"$(cygpath -w "$body_file")")"
    rm -f "$body_file"
    printf '%s' "$chat2" | grep -q "G-M2-APPROVAL-OK" || {
        emit_result approval FAIL "重试轮终文未含 G-M2-APPROVAL-OK: $(printf '%s' "$chat2" | head -c 140)"
        return 1
    }
    # ⑥设备侧文件落盘断言（run-as 直读 proot rootfs）
    local artifact
    artifact="$(adb_device_cmd -s "$serial" shell \
        "run-as $PACKAGE cat files/rootfs/opt/agentos/gm2_approval_probe.txt 2>/dev/null" | tr -d '\r\n')"
    # ⑦名单还原（防污染后续轮）+ 残卡清理
    curl -s --max-time 15 -X POST "$BASE_URL/ext/pipeline_security_check/zones/remove" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        -d '{"section":"entries","path":"/opt/agentos"}' >/dev/null 2>&1
    curl -s --max-time 10 -X POST "$BASE_URL/ext/approval_service/interaction/$rid/cancel" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}' >/dev/null 2>&1
    if [[ "$artifact" == "G-M2-APPROVAL-OK" ]]; then
        emit_result approval PASS "审批全链绿（session=$sid 卡=$rid 批准=permanent）文件落盘精确匹配+名单已还原"
        return 0
    fi
    emit_result approval FAIL "文件落盘断言失败（期望 G-M2-APPROVAL-OK，实得 '${artifact:0:40}'）"
    return 1
}

# G-M2 第 14 步（R9 移动版精细测试同刀，可选步）：覆盖安装升级自动化——
# adb install -r 覆盖 → versionCode 单调断言 → 数据保全断言 → 种子对账路径断言。
#
# 口径（2026-10-15 R9 实测在案）：
# - 对账键=种子资产 SHA-256 digest（坑㊲ 修复后），logcat LingxiKernelHost
#   "seed extract start (seeded=<old> now=<new>)" 行=重解包路径；无此行+health
#   恢复=digest 相同跳解包快路径。两路径均合法，步断言"路径可判定"并如实记录。
# - 数据保全断言面：壳凭据（SharedPreferences，rootfs 外）必须存活；探针会话
#   （REST 建，install -r 前 planted）必须仍在账；user .env 若升级前存在则升级后
#   必须仍在。跨种子重解包连坐清除用户数据已修（R9-A，2026-10-15：user_root+
#   .ai_workspaces 迁出 rootfs 至 files/user_root 平级+proot bind 映射，extractSeed
#   清除半径收窄到种子部署面）——升级/降级重解包数据保全，本步为回归锁。
# - 前置：$KERNEL_PORT 产品化链驻留（proot_boot 同前置）+凭据（--password 或
#   run-as 提取）；--apk 给新包（同包覆盖=skip 路径验证；跨版本包=extract 路径）。
step_upgrade() {
    local serial
    serial="$(resolve_serial)" || { emit_result upgrade FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$APK" ]] || { emit_result upgrade FAIL "缺 --apk/AGENTOS_AVD_APK（覆盖用新包）"; return 1; }
    [[ -f "$APK" ]] || { emit_result upgrade FAIL "APK 不存在: $APK"; return 1; }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
    local pre_code
    pre_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    [[ "$pre_code" == "200" ]] || {
        emit_result upgrade FAIL "前置不满足：$KERNEL_PORT 无驻留内核（先跑 proot_boot 产品化链）"
        return 1
    }
    # 凭据：显式给出或 run-as 提取（壳 prefs，rootfs 外）
    local pass
    if [[ -n "$KERNEL_PASS" ]]; then
        pass="$KERNEL_PASS"
    else
        pass="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE cat shared_prefs/lingxi_kernel.xml 2>/dev/null" | tr -d '\r' | sed -n 's/.*<string name="admin_password">\([^<]*\)<\/string>.*/\1/p' | head -1)"
    fi
    [[ -n "$pass" ]] || { emit_result upgrade FAIL "凭据缺失（--password 或 run-as 提取均无）"; return 1; }
    local pre_vc
    pre_vc="$(adb_t 15 -s "$serial" shell "dumpsys package $PACKAGE" 2>/dev/null | grep -m1 -oE 'versionCode=[0-9]+' | cut -d= -f2)"
    [[ -n "$pre_vc" ]] || { emit_result upgrade FAIL "pre versionCode 读取失败（包未安装?）"; return 1; }
    # ①升前造数据：登录+探针会话+.env 存在性/键数快照（保全断言基线）
    local tok sid pre_env pre_keycount
    tok="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$pass\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || { emit_result upgrade FAIL "升前登录失败"; return 1; }
    sid="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}' \
        | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || { emit_result upgrade FAIL "升前建探针会话失败"; return 1; }
    pre_env="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'test -f files/user_root/.env && echo yes || echo no'" | tr -d '\r\n')"
    local pre_env_src="new"
    if [[ "$pre_env" != "yes" ]]; then
        # 旧布局基包（R9-A 修复前 user_root 在种子树内）：升前快照回退旧位取数，
        # 升后仍断新位键数不丢——缺此回退则 23→24 升级轮 env 断言退化 n/a（假绿面）
        pre_env_src="legacy"
        pre_env="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'test -f files/rootfs/opt/agentos/user_root/.env && echo yes || echo no'" | tr -d '\r\n')"
    fi
    if [[ "$pre_env" == "yes" ]]; then
        if [[ "$pre_env_src" == "legacy" ]]; then
            pre_keycount="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -c _API_KEY files/rootfs/opt/agentos/user_root/.env'" | tr -d '\r\n ')"
        else
            pre_keycount="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -c _API_KEY files/user_root/.env'" | tr -d '\r\n ')"
        fi
    else
        pre_keycount="0"
    fi
    # 任务产物面 marker（R9-A 断言增补：任务产物是用户数据保全面之一）——升前写入
    # 现存产物面（旧布局基包=种子树内旧位，缺失即建；新布局=新位），升后断言新位
    # 同名同值可读。断行为（产物数据升后仍在），不断迁移实现细节。
    local marker_name="gm2_upgrade_marker.txt" marker_body pre_ws ws_probe ws_pre_dir
    marker_body="GM2-R9A-MARKER-$(date +%s)"
    ws_probe="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'test -d files/user_root/.ai_workspaces && echo new || echo old'" | tr -d '\r\n')"
    if [[ "$ws_probe" == "new" ]]; then
        pre_ws="new"
        ws_pre_dir="files/user_root/.ai_workspaces"
    else
        pre_ws="legacy"
        ws_pre_dir="files/rootfs/opt/agentos/.ai_workspaces"
        adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'mkdir -p $ws_pre_dir'" >/dev/null
    fi
    adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'printf %s $marker_body > $ws_pre_dir/$marker_name'" >/dev/null
    ws_probe="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'cat $ws_pre_dir/$marker_name 2>/dev/null'" | tr -d '\r\n')"
    [[ "$ws_probe" == "$marker_body" ]] || {
        emit_result upgrade FAIL "升前产物 marker 写入/回读失败（face=$pre_ws）——车道环境问题，非升级结论"
        return 1
    }
    # ②覆盖安装（logcat 先清场：对账行证据窗口=本次 boot）
    adb_t 20 -s "$serial" logcat -c >/dev/null 2>&1
    local install_out
    install_out="$(adb_t 300 -s "$serial" install -r "$APK" 2>&1)"
    if printf '%s' "$install_out" | grep -q "INSTALL_FAILED_VERSION_DOWNGRADE"; then
        emit_result upgrade FAIL "install -r 被拒（versionCode 单调执法，--apk 须不旧于在装 ${pre_vc}）: $(printf '%s' "$install_out" | tail -1 | head -c 120)"
        return 1
    fi
    printf '%s' "$install_out" | grep -q "^Success" || {
        emit_result upgrade FAIL "install -r 失败: $(printf '%s' "$install_out" | tail -1 | head -c 120)"
        return 1
    }
    # ③versionCode 单调（覆盖安装不得回拨）
    local post_vc
    post_vc="$(adb_t 15 -s "$serial" shell "dumpsys package $PACKAGE" 2>/dev/null | grep -m1 -oE 'versionCode=[0-9]+' | cut -d= -f2)"
    # ④复启等 health（install 替换包会杀进程；跨种子含重解包+重装配属预期耗时）
    local t0 boot_s http_code
    t0=$(date +%s)
    adb_t 20 -s "$serial" shell "monkey -p $PACKAGE -c android.intent.category.LAUNCHER 1" >/dev/null 2>&1
    for ((i = 1; i <= UPGRADE_BOOT_TIMEOUT_S; i++)); do
        adb_is_alive "$serial" || adb_reconnect "$serial"
        adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1
        http_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
        [[ "$http_code" == "200" ]] && break
        sleep 2
    done
    boot_s=$(( $(date +%s) - t0 ))
    [[ "$http_code" == "200" ]] || {
        emit_result upgrade FAIL "升级后 health 未恢复（${UPGRADE_BOOT_TIMEOUT_S} 轮，vc=${pre_vc}→${post_vc:-?}）"
        return 1
    }
    # ⑤对账路径判定（LingxiKernelHost seed extract 行=extract；无行+health=skip）
    local seed_line path="skip" seeded="same" now="same"
    seed_line="$(adb_t 20 -s "$serial" logcat -d -s LingxiKernelHost 2>/dev/null | tr -d '\r' | grep -m1 'seed extract start')"
    if [[ -n "$seed_line" ]]; then
        path="extract"
        seeded="$(printf '%s' "$seed_line" | grep -oE 'seeded=[0-9a-f]*' | cut -d= -f2 | cut -c1-8)"
        now="$(printf '%s' "$seed_line" | grep -oE 'now=[0-9a-f]*' | cut -d= -f2 | cut -c1-8)"
        [[ "$seeded" == "null" ]] && seeded="null(首装)"
    fi
    # ⑥数据保全断言（凭据/会话/.env 三面）
    local tok2 sess_found post_env post_keycount problems=""
    tok2="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$pass\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok2" ]] || problems+="credentials-lost;"
    sess_found=0
    if [[ -n "$tok2" ]]; then
        sess_found="$(curl -s --max-time 15 "$BASE_URL/api/v1/sessions" -H "Authorization: Bearer $tok2" | grep -c "$sid")"
    fi
    [[ "$sess_found" -ge 1 ]] || problems+="session-lost;"
    post_env="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'test -f files/user_root/.env && echo yes || echo no'" | tr -d '\r\n')"
    local env_note="n/a"
    if [[ "$pre_env" == "yes" ]]; then
        post_keycount="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'grep -c _API_KEY files/user_root/.env'" 2>/dev/null | tr -d '\r\n ')"
        [[ "$post_env" == "yes" && "$post_keycount" == "$pre_keycount" ]] || problems+="env-lost(${pre_keycount}→${post_keycount:-0});"
        env_note="${pre_keycount}→${post_keycount:-0}"
    fi
    [[ "$post_vc" -ge "$pre_vc" ]] 2>/dev/null || problems+="vc-not-monotonic(${pre_vc}→${post_vc:-?});"
    # 产物面回读（升后必须落新布局位且同值）
    local post_marker marker_note="lost"
    post_marker="$(adb_device_cmd -s "$serial" shell "run-as $PACKAGE sh -c 'cat files/user_root/.ai_workspaces/$marker_name 2>/dev/null'" | tr -d '\r\n')"
    if [[ "$post_marker" == "$marker_body" ]]; then
        marker_note="ok"
    else
        problems+="workspace-lost(pre=${pre_ws});"
    fi
    if [[ -z "$problems" ]]; then
        emit_result upgrade PASS "覆盖安装升级绿（vc=${pre_vc}→${post_vc} 对账路径=${path} seeded=${seeded} now=${now} boot=${boot_s}s 会话保全=yes env保全=${env_note}[快照=${pre_env_src}] 产物保全=${marker_note}[face=${pre_ws}]）"
        return 0
    fi
    emit_result upgrade FAIL "升级红: ${problems%;}（vc=${pre_vc}→${post_vc:-?} 对账路径=${path} seeded=${seeded} now=${now} boot=${boot_s}s 产物face=${pre_ws}）"
    return 1
}

# G-M2 第 15 步（精细测试队列④车道化，2026-10-15 脚本先行；2026-10-16 R22
# MuMu 实跑过=断言 a/b/c 绿+断言 d 红=失败轮不落库缺陷立案）：
# 断网恢复自动化——四断言面（断用户可见行为）：
#  a) 断网态发起对话：REST chat 在 --net-recovery-chat-timeout-s 上限内返回
#     （不假死），回复面如实降级（degraded=true 或 content 含失败语义文本）。
#     现状依据=2026-10-15 R16 ⑧实机：LLM 不可达时回复 content=「LLM 调用失败：
#     llm_core.execute llm.complete_stream 工具执行…」错误面直写 content，方案
#     影响矩阵同口径「LLM 通道挂→对话报错可见禁静默降级」（无降级标志且无失败
#     文本=疑似假回复即红）；
#  b) 断网期内核存活：断网置入生效后+断网态对话返回后两查 /health 均 200；
#  c) 恢复后自愈：网络恢复→新一轮对话 degraded=false+content 非空（provider
#     通道自愈，无需重启内核；允许一次重试=恢复首调可能命中 DNS 负缓存物理
#     现象，第二次仍败才 FAIL）；
#  d) 断网轮语义：实现面无重试队列（chat 同步管道，LLM 失败即当轮终态落库，
#     R16 ⑧同证），恢复后 GET messages 中断网轮失败回复指纹仍在且仅一次
#     （超时失败即失败，不幽灵补发不状态翻转）——本断言锁现状；实现面引入
#     重试队列时随实现改断言并记录。
#
# 断网手段选型（判据在案；实跑第一轮以探针对照复核）：
# - 主=iptables OUTPUT 白名单 DROP（内核 netfilter 强制层；root 直通=boot 步
#   root_permission=true，uid=0 可写 filter 表）：MuMu 设备网络形态版本敏感
#   （老版=eth0 虚拟网卡 NAT/桥接，无 wlan0/rmnet，svc/provider 对其无效；
#   2026-10-16 Android 15 nx 实测=wlan0 WIFI provider 形态 10.0.2.0/24 网关
#   10.0.2.2——直连网段解析因此不绑网卡名 eth/wlan/rmnet 通吃；svc/飞行模式对
#   wlan0 形态可能真生效）——iptables 仍是主手段：唯一不依赖 provider/ROM
#   语义的强制层（netfilter hook 在协议栈，非系统组件语义）。规则=ACCEPT lo+
#   直连网段（保 adb/forward 通道不与断网同死）→DROP 其余（公网出站含 DNS
#   全断），恢复=逐条 -D 删规则即愈（无 reboot）。MuMu 内核若裁剪 netfilter
#   （概率低：netd 强依赖 iptables）则主手段不可用如实转备。
# - 备=飞行模式（settings put global airplane_mode_on+AIRPLANE_MODE 广播，
#   protected broadcast 须 root 直通下发）+svc wifi/data disable——仅 iptables
#   不可用（无二进制/规则写入失败/直连网段解析失败）时启用；MuMu 上生效性
#   存疑，同以探针对照判定，无效即如实 FAIL「断网手段均未生效」报备不硬写。
#   已知风险=飞行模式若真下线 eth0 会连带断 adb/forward 通道（恢复只能依赖
#   系统自身或人工重启实例）——此即 iptables 为主手段的原因之一。
# - 生效判据=对照探针三闭环：断网前设备 ping --net-recovery-probe-ip 可达
#   （基线成立，否则 FAIL 指路换探针）、断网后同探针不可达（不可达才算断上，
#   防规则未生效假绿）、恢复后可达（防恢复动作未落假绿）。
# 前置：$KERNEL_PORT 产品化链驻留（health 200 自查，不代拉起，proot_boot 同
# 前置）+已配 key（设备 user .env 有 *_API_KEY，只取计数零落账；纯 pm clear
# 态预期 FAIL 属产品语义，同 message 步）。
# 还原保证：恢复动作幂等+trap EXIT 兜底（未预期退出/timeout 击杀也还原设备
# 网络，防污染同实例后续车道——设备留在断网态会让后续轮全假红）。
step_network_recovery() {
    local serial
    serial="$(resolve_serial)" || { emit_result network_recovery FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result network_recovery FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    NET_RESTORE_SERIAL="$serial"
    NET_RESTORE_CMDS=()
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result network_recovery FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    # 前置自查①：产品化链驻留（health 200，不代拉起）
    local pre_code
    pre_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    [[ "$pre_code" == "200" ]] || {
        emit_result network_recovery FAIL "前置不满足：$KERNEL_PORT 无驻留内核（先跑 proot_boot 产品化链）"
        return 1
    }
    # 前置自查②：已配 key（设备 user .env *_API_KEY 键数；只取计数，key 现值零落账）
    local keycount
    keycount="$(adb_device_cmd -s "$serial" shell \
        "run-as $PACKAGE sh -c 'grep -c _API_KEY files/user_root/.env'" 2>/dev/null | tr -d '\r\n ')"
    [[ "${keycount:-0}" -ge 1 ]] 2>/dev/null || {
        emit_result network_recovery FAIL "前置不满足：设备 user .env 无 *_API_KEY（未配 key；纯 pm clear 态预期 FAIL 属产品语义）"
        return 1
    }
    local tok
    tok="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result network_recovery FAIL "login 失败"
        return 1
    }
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        emit_result network_recovery FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    # 设备侧探针（ping 出站可达性，rc 判定；toybox ping -W 秒）
    net_probe_ok() { # 0=可达
        local rc
        rc="$(adb_device_cmd -s "$serial" shell \
            "ping -c 1 -W 2 $NETRECOVERY_PROBE_IP >/dev/null 2>&1; echo \$?" | tr -d '\r\n ')"
        [[ "$rc" == "0" ]]
    }
    # 设备侧 iptables 单命令成功判定（adb shell 不传播 rc，内嵌 echo 标记判定）
    ipt_run() { # $1=iptables 命令体 → 0=设备端执行成功
        adb_device_cmd -s "$serial" shell "$1 && echo IPTOK" 2>/dev/null | grep -q IPTOK
    }
    # 幂等恢复：主手段逐条删规则+备手段归位（任何退出路径都执行，防设备留断网态）
    net_restore() {
        local c
        if [[ ${#NET_RESTORE_CMDS[@]} -gt 0 ]]; then
            for c in "${NET_RESTORE_CMDS[@]}"; do
                adb_device_cmd -s "$NET_RESTORE_SERIAL" shell "$c" >/dev/null 2>&1
            done
            NET_RESTORE_CMDS=()
        fi
        adb_device_cmd -s "$NET_RESTORE_SERIAL" shell \
            "settings put global airplane_mode_on 0; am broadcast -a android.intent.action.AIRPLANE_MODE --ez state false >/dev/null 2>&1; svc wifi enable >/dev/null 2>&1; svc data enable >/dev/null 2>&1" \
            >/dev/null 2>&1
        return 0
    }
    trap 'net_restore' EXIT
    # ①断网前探针基线（防「探标本就不可达」假绿）
    net_probe_ok || {
        trap - EXIT
        emit_result network_recovery FAIL "探针 $NETRECOVERY_PROBE_IP 断网前即不可达（基线不成立，换 --net-recovery-probe-ip）"
        return 1
    }
    # ②置断网：主=iptables OUTPUT 白名单 DROP；不可用转备=飞行模式+svc
    local mode="iptables" ipt_err="" nets nw
    # 幂等清残留（上轮异常退出可能留 DROP 规则；失败可容忍=无残留时 -D 报错）
    adb_device_cmd -s "$serial" shell "iptables -D OUTPUT -j DROP" >/dev/null 2>&1
    nets="$(adb_device_cmd -s "$serial" shell "ip route show 2>/dev/null" | tr -d '\r' \
        | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+ dev (eth|wlan|rmnet)[0-9]+' | cut -d' ' -f1)"
    if [[ -n "$nets" ]] && ipt_run "iptables -A OUTPUT -o lo -j ACCEPT"; then
        NET_RESTORE_CMDS+=("iptables -D OUTPUT -o lo -j ACCEPT")
        for nw in $nets; do
            if ipt_run "iptables -A OUTPUT -d $nw -j ACCEPT"; then
                NET_RESTORE_CMDS+=("iptables -D OUTPUT -d $nw -j ACCEPT")
            else
                ipt_err="直连网段规则写入失败: $nw"
                break
            fi
        done
        if [[ -z "$ipt_err" ]] && ipt_run "iptables -A OUTPUT -j DROP"; then
            NET_RESTORE_CMDS+=("iptables -D OUTPUT -j DROP")
        else
            ipt_err="${ipt_err:-DROP 规则写入失败}"
        fi
    else
        ipt_err="${ipt_err:-iptables 不可用或直连网段解析失败}"
    fi
    if [[ -n "$ipt_err" ]]; then
        log "network_recovery: iptables 不可用（$ipt_err），转备手段=飞行模式+svc（生效性存疑，探针说了算）"
        adb_device_cmd -s "$serial" shell \
            "settings put global airplane_mode_on 1 && am broadcast -a android.intent.action.AIRPLANE_MODE --ez state true >/dev/null 2>&1; svc wifi disable >/dev/null 2>&1; svc data disable >/dev/null 2>&1" \
            >/dev/null 2>&1
        mode="airplane+svc"
        sleep 3
        if ! adb_is_alive "$serial"; then
            emit_result network_recovery FAIL "备手段置入后 adb 通道死亡（飞行模式真下线 eth0 连带断 adb，函数头风险在案；须人工恢复实例网络后重跑）"
            return 1
        fi
    fi
    sleep 2
    # ③断网生效对照判定（探针不可达才算断上）
    if net_probe_ok; then
        net_restore
        trap - EXIT
        emit_result network_recovery FAIL "断网手段未生效（mode=$mode 探针 $NETRECOVERY_PROBE_IP 仍可达——iptables 规则面异常或飞行模式对 MuMu eth0 无效，如实红报备）"
        return 1
    fi
    # ④断言 b 前半：断网期内核存活（置入后一查）
    local h1
    h1="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    # ⑤断言 a：断网态对话（上限内返回+如实降级：degraded=true 或 content 失败语义）
    local body_file chat t0 t1 reply degraded_flag=0 fail_flag=0
    body_file="$REPO_ROOT/.zctmp/gm2_netrec.$$.json"
    printf '{"message":"这是 G-M2 断网态探针消息，请只回复一句话确认收到。","session_id":"%s"}' "$sid" \
        > "$body_file"
    t0=$(date +%s)
    chat="$(curl -s --max-time "$NETRECOVERY_CHAT_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        --data-binary @"$(cygpath -w "$body_file")")"
    t1=$(date +%s)
    rm -f "$body_file"
    reply="$(printf '%s' "$chat" | head -c 200)"
    printf '%s' "$chat" | grep -q '"degraded": *true' && degraded_flag=1
    printf '%s' "$chat" | grep -q '失败' && fail_flag=1
    # ⑥断言 b 后半：断网态对话返回后内核仍存活（二查）
    local h2
    h2="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    # 断网轮失败回复指纹（断言 d 基线；content 提取容忍 "content":"x"/"content": "x" 两形态）
    local fp
    fp="$(printf '%s' "$chat" | grep -oE '"content": *"[^"]{0,48}' | head -1 | sed 's/.*: *"//')"
    # ⑦恢复网络（幂等）+ 恢复探针确认（4 轮×RECOVER_WAIT_S）
    net_restore
    local r_ok=0 r_try
    for r_try in 1 2 3 4; do
        sleep "$NETRECOVERY_RECOVER_WAIT_S"
        if net_probe_ok; then r_ok=1; break; fi
    done
    # ⑧断言 c：恢复后自愈对话（允许一次重试=DNS 负缓存；真回复=degraded=false+content）
    local chat2 ok2=0 try2
    for try2 in 1 2; do
        body_file="$REPO_ROOT/.zctmp/gm2_netrec2.$$.json"
        printf '{"message":"网络已恢复，请只回复两个字：收到。不要使用任何工具。","session_id":"%s"}' "$sid" \
            > "$body_file"
        chat2="$(curl -s --max-time "$NETRECOVERY_CHAT_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$body_file")")"
        rm -f "$body_file"
        if printf '%s' "$chat2" | grep -q '"degraded": *false' \
            && printf '%s' "$chat2" | grep -q '"content"'; then
            ok2=1
            break
        fi
        sleep 10
    done
    # ⑨断言 d：断网轮不幽灵补发（失败回复指纹仍在 messages 且仅一次）
    local msgs fp_hits=0
    if [[ -n "$fp" ]]; then
        msgs="$(curl -s --max-time 20 "$BASE_URL/api/v1/sessions/$sid/messages" -H "Authorization: Bearer $tok")"
        fp_hits="$(printf '%s' "$msgs" | grep -oF "$fp" | wc -l)"
    fi
    trap - EXIT
    # 聚合 problems 单行输出（与既有步同格式）
    local problems=""
    [[ "$h1" == "200" ]] || problems+="health-down-after-off;"
    [[ "$h2" == "200" ]] || problems+="health-down-after-chat;"
    if [[ -z "$chat" ]]; then
        problems+="chat-hang(>${NETRECOVERY_CHAT_TIMEOUT_S}s-no-return);"
    elif [[ "$degraded_flag" -ne 1 && "$fail_flag" -ne 1 ]]; then
        problems+="degradation-not-honest(无降级标志且无失败文本,疑似假回复);"
    fi
    [[ "$r_ok" -eq 1 ]] || problems+="net-not-restored;"
    [[ "$ok2" -eq 1 ]] || problems+="no-selfheal-reply;"
    if [[ -n "$fp" ]]; then
        [[ "$fp_hits" -eq 1 ]] || problems+="ghost-replay-or-lost(fp_hits=$fp_hits);"
    else
        problems+="fingerprint-empty;"
    fi
    if [[ -z "$problems" ]]; then
        emit_result network_recovery PASS "断网恢复全链绿（mode=$mode thread=$sid 断网态对话 $((t1 - t0))s 内返回[degraded=$degraded_flag 失败文本=$fail_flag]+health 断网期 200×2+恢复探针 ok+恢复轮真回复 degraded=false+断网轮指纹不幽灵补发）"
        return 0
    fi
    emit_result network_recovery FAIL "network_recovery 红: ${problems%;} mode=$mode 断网轮回复头: ${reply//\"/\'}"
    return 1
}

# G-M2 第 16 步（精细测试队列⑥车道化，2026-10-16 脚本先行；首实跑 FAIL=R22
# 车道两缺陷立案——bad/switchback PUT 复用初快照 etag→409×2 + 旧会话断言
# pid-差集法撞 pipeline 随 thread 稳定语义不可判；断言 a/b/c 过；同日 M1/M2
# 修复【实跑现状：修复待下轮 MuMu 实跑复验，判达标后本注记更新】：M1=每次
# PUT 前 fresh GET 取当轮 etag（restore 步同模式，msw_fresh_patch_put 公共
# 路径）；M2=旧会话断言换 messages slot 计数增量取证键）：
# 多会话模型切换自动化——五断言面（断用户可见行为；切换 REST 全程留痕不碰 UI，
# UI 面归精细测试实跑轮）：
#  a) 切换默认模型：REST 配置面 GET/PUT /api/v1/plugins/llm_service/config/llm
#     （内核单一配置面：掩码+ETag 乐观锁 if_match+写恒用户空间+首写播种；前端
#     设置页/向导同通道，frontend services/api/config.ts editLlmFile 同构往返，
#     掩码哨兵由后端合并故 GET 掩码视图可整树回 PUT）把 defaults.chat+tiers
#     三档从 A 齐切到 B。A/B 取设备 models 映射真实键（键名大小写必须与真实键
#     一致——ae3525b13 教训：MiniMax-M3.1-Flash-Preview 大小写不匹配致 provider
#     查空）；--model-a 缺省=切换前 defaults.chat 现值快照，--model-b 缺省
#     minimax-m3.1-flash-preview（B 不在映射时回退同 provider 且异于 A 的任一
#     真实键，仍无则 FAIL 指路 --model-b）；
#  b) 新会话生效：切后新会话对话，GET /api/v1/pipelines/state 会话账该轮
#     state.llm_model==B 的 model_name。现状依据：llm_core（pipeline_llm_core）
#     plugin.json export_fields 含 llm_model，plugin.py execute 结果键
#     "llm_model"=self._model=models[B].model_name（_config_models
#     get_llm_core_config 缺省回落键名）；/pipelines/state（routes.rs
#     pipelines_state_handler）出口=registry 快照+DB 镜像含插件出口字段，前端
#     用量指示器同源。messages 列表无逐条模型字段（role/content/status/
#     agentId/toolCalls 等，无 model），会话账是模型标记的现役取证面；
#  c) tiers 齐切语义（坑㊵ 契约）：切后设备 user llm.yaml
#     （files/user_root/config/plugins/llm/llm.yaml，写恒用户空间落点）文件面
#     defaults.chat 与 tiers.large/medium/small 一致翻新为 B——主对话 agent
#     config/agents/main/agentos.yaml 钉 model_tier: large 走 tiers 解析，只切
#     chat 不切 tiers=agent 仍解析旧模型（坑㊵：向导只写 chat 净装态对话必死，
#     af7d1cdd1 修复=三档齐切）；切换载荷与向导 applyDefaultModel 同构；
#  d) 旧会话语义=跟随新默认（现状断言，依据三条）：①llm_core/plugin.py
#     _apply_model_from_state 每轮 execute 从 state 现解析（优先级
#     state.model_id > state.model_tier→defaults.tiers > defaults.chat），实例
#     无会话级模型钉住；②agent_config_load 只把 agent yaml 静态 model_tier
#     （main=large）写 state，REST chat 链路无人写 state.model_id（前端模型片
#     =纯显示，ChatContainer 由 agent config+tiers 派生不回传）；③坑㊵ R9 实测
#     向导齐切后下一轮对话即新模型无需重启（llm_core server.py
#     on_config_changed 每调用刷新注入视图）。→ 切换前建的旧会话切后再聊，
#     会话账该轮 llm_model==B（若实跑钉住 A=现状与代码语义不符，红如实立案）。
#     取证键（R22-M2 修正）：pipeline 随 thread 稳定（R22 实跑双轮同 pid 直证），
#     pid-差集法定位「切后新轮」结构性不可判 → 改按旧会话 messages slot 计数
#     增量（GET /api/v1/sessions/{id}/messages 的 total，切前 N→切后 ≥N+2=
#     user+assistant 新轮已落）定位新轮，再读会话账同 pid 行 llm_model 原地
#     翻新值（state 行随每轮 execute 翻新）；slot 计数不可靠窗口（④-A 修复
#     在飞，落库语义过渡）→ 兜底取证=内核日志 `Calling LLM: model=` 行
#     （llm_core 每调用记录实际解析模型 self._model=model_name，与 state.
#     llm_model 同源同值；`LLM call succeeded` 行与 chat REST 响应体均无模型
#     字段，故取此行——logcat 清场收窄到仅本轮对话，窗口内唯一调用取末行）；
#  e) 失效模型探针+恢复：切到 models 映射外名字（--model-bad 默认
#     nonexistent-model，齐切）——硬断言两面：「不假死」（chat 上限内返回且有
#     content）+「切回 A 恢复」（新会话 llm_model==A 的 model_name）。降级语义
#     按现状探针记录不硬写断言：代码面现状=llm_core 解析未命中 models 映射时
#     logger.warning 并保持上一成功解析的模型字段（_apply_model_from_state
#     「解析失败保持当前字段不动」）→ 对话以旧模型继续成功=静默回落，与
#     「如实报错/degraded=true 不静默回落」（对齐 R16⑧ 失败直写语义）的预期
#     疑似不符——立案见台账（修复后随实现改断言）；探针记录该轮 llm_model/
#     degraded/失败文本标志，如实上报两种现状。
# 还原保证：快照（GET 全量 data+etag）测后还原（REST PUT 回快照+REST 视图与
# 设备文件双对照）+trap EXIT 兜底（还原不完整时留守，脚本退出再试一次；防换档/
# 失效模型名污染同实例后续车道——全部车道共用设备）。
# 前置：$KERNEL_PORT 产品化链驻留（health 200 自查不代拉起，proot_boot 同款）+
# 已配 key（设备 user .env *_API_KEY 计数零落账；纯 pm clear 态预期 FAIL 属产品
# 语义，同 message 步）。
step_model_switch() {
    local serial
    serial="$(resolve_serial)" || { emit_result model_switch FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result model_switch FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    # 宿主 python（stdlib json：会话账解析/PUT 载荷拼装；cancel 步同款依赖探测；
    # 落全局 MSW_PY——msw_py 与 trap 兜底函数都消费它）
    MSW_PY=""
    local cand
    for cand in python python3 py; do
        if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import json" >/dev/null 2>&1; then
            MSW_PY="$cand"
            break
        fi
    done
    [[ -n "$MSW_PY" ]] || {
        emit_result model_switch FAIL "宿主 python 不可用（会话账解析/载荷拼装需要，stdlib json）"
        return 1
    }
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result model_switch FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    # 前置自查①：产品化链驻留（health 200，不代拉起）
    local pre_code
    pre_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    [[ "$pre_code" == "200" ]] || {
        emit_result model_switch FAIL "前置不满足：$KERNEL_PORT 无驻留内核（先跑 proot_boot 产品化链）"
        return 1
    }
    # 前置自查②：已配 key（设备 user .env *_API_KEY 键数；只取计数，key 现值零落账）
    local keycount
    keycount="$(adb_device_cmd -s "$serial" shell \
        "run-as $PACKAGE sh -c 'grep -c _API_KEY files/user_root/.env'" 2>/dev/null | tr -d '\r\n ')"
    [[ "${keycount:-0}" -ge 1 ]] 2>/dev/null || {
        emit_result model_switch FAIL "前置不满足：设备 user .env 无 *_API_KEY（未配 key；纯 pm clear 态预期 FAIL 属产品语义）"
        return 1
    }
    local tok
    tok="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result model_switch FAIL "login 失败"
        return 1
    }
    MSW_RESTORE_TOK="$tok"
    # ①快照：GET 掩码视图全量 data+etag（还原基准；写恒用户空间落点=设备
    #   files/user_root/config/plugins/llm/llm.yaml，首写播种后存在）
    local snap_file="$REPO_ROOT/.zctmp/gm2_msw_snap.$$.json"
    local cfg_file="$REPO_ROOT/.zctmp/gm2_msw_cfg.$$.json"
    local state_file="$REPO_ROOT/.zctmp/gm2_msw_state.$$.json"
    local body_file="$REPO_ROOT/.zctmp/gm2_msw_body.$$.json"
    local put_file="$REPO_ROOT/.zctmp/gm2_msw_put.$$.json"
    local msgs_file="$REPO_ROOT/.zctmp/gm2_msw_msgs.$$.json"
    local http_code
    http_code="$(curl -s -o "$snap_file" -w '%{http_code}' --max-time 20 \
        "$BASE_URL/api/v1/plugins/llm_service/config/llm" -H "Authorization: Bearer $tok")"
    [[ "$http_code" == "200" ]] || {
        emit_result model_switch FAIL "GET config/llm -> ${http_code:-无响应}"
        return 1
    }
    MSW_SNAPSHOT_FILE="$snap_file"
    # ── 工具函数（步内定义=全局存活；消费面全全局变量，trap 期可用）──
    msw_py() { # $1=mode，其余经 argv 透传（值不经 shell 展开不注入）；modes 见 PYEOF
        "$MSW_PY" - "$@" << 'PYEOF'
import json, sys

mode, path = sys.argv[1], sys.argv[2]


def name_of(models, key):
    """models[key].model_name，缺省回落键名（get_llm_core_config 同款回落）。"""
    if not key:
        return ""
    conf = models.get(key) or {}
    return conf.get("model_name", key)


if mode == "facts":  # <get_json> <want_a> <want_b> → 快照事实+模型选定
    body = json.load(open(path, encoding="utf-8"))
    data = body.get("data") or {}
    models = data.get("models") or {}
    defaults = data.get("defaults") or {}
    tiers = defaults.get("tiers") or {}
    want_a, want_b = sys.argv[3], sys.argv[4]
    a = want_a or defaults.get("chat", "")
    base_id = tiers.get("large") or a
    b = ""
    if want_b and want_b != a and want_b in models:
        b = want_b
    else:
        # 回退：同 provider 且异于 A 的任一真实键（键名保真自映射，ae3525b13）
        prov = (models.get(a) or {}).get("provider", "")
        for k in sorted(models):
            if k != a and (models.get(k) or {}).get("provider", "") == prov:
                b = k
                break
    print("ETAG=" + str(body.get("etag", "")))
    print("A=" + a)
    print("ANAME=" + name_of(models, a))
    print("BASE=" + name_of(models, base_id))
    print("B=" + b)
    print("BNAME=" + name_of(models, b))
    for t in ("large", "medium", "small"):
        print("TIER_" + t.upper() + "=" + str(tiers.get(t, "")))
elif mode == "patch":  # <get_json> <out_json> <模型名> → PUT 载荷（defaults 齐切）
    body = json.load(open(path, encoding="utf-8"))
    data = body.get("data") or {}
    defaults = dict(data.get("defaults") or {})
    m = sys.argv[4]
    defaults["chat"] = m
    # 三档齐切=向导 applyDefaultModel 同构载荷（坑㊵ 契约）；embedding 等其余
    # defaults 键原样保留
    defaults["tiers"] = {"large": m, "medium": m, "small": m}
    data["defaults"] = defaults
    with open(sys.argv[3], "w", encoding="utf-8") as f:
        json.dump({"data": data, "if_match": body.get("etag", "")}, f, ensure_ascii=False)
    print("PATCH=ok")
elif mode == "restore":  # <snapshot_json> <fresh_get_json> <out_json> → 还原载荷
    snap = json.load(open(path, encoding="utf-8"))
    fresh = json.load(open(sys.argv[3], encoding="utf-8"))
    with open(sys.argv[4], "w", encoding="utf-8") as f:
        json.dump({"data": snap.get("data") or {}, "if_match": fresh.get("etag", "")},
                  f, ensure_ascii=False)
    print("RESTORE=ok")
elif mode == "pids":  # <state_json> <thread_id> → 该会话 pipeline_id 逐行
    rows = (json.load(open(path, encoding="utf-8")).get("items")) or []
    for r in rows:
        if r.get("thread_id") == sys.argv[3]:
            print(r.get("pipeline_id", ""))
elif mode == "model_of":  # <state_json> <pipeline_id> → 该行 state.llm_model
    rows = (json.load(open(path, encoding="utf-8")).get("items")) or []
    for r in rows:
        if r.get("pipeline_id") == sys.argv[3]:
            print((r.get("state") or {}).get("llm_model", ""))
            break
elif mode == "msgcount":  # <messages_json> → total（messages slot 计数；断言 d
    # 取证键 R22-M2：切前 N→切后 N+2=user+assistant 新轮已落）
    print(json.load(open(path, encoding="utf-8")).get("total", ""))
elif mode == "check":  # <get_json> <模型名> → REST 视图 defaults 齐切核对
    data = (json.load(open(path, encoding="utf-8")).get("data")) or {}
    defaults = data.get("defaults") or {}
    tiers = defaults.get("tiers") or {}
    m = sys.argv[3]
    ok = defaults.get("chat") == m and all(
        tiers.get(t) == m for t in ("large", "medium", "small"))
    print("CHECK=" + ("ok" if ok else "bad"))
    print("NOW=chat:%s,large:%s,medium:%s,small:%s" % (
        defaults.get("chat", ""), tiers.get("large", ""),
        tiers.get("medium", ""), tiers.get("small", "")))
PYEOF
    }
    msw_trap_restore() {
        # 脚本退出兜底：快照还原（纯 REST，fresh etag 防漂移；只在步内还原不
        # 完整时留守触发，正常收尾已 trap - EXIT 解除）
        local fresh="$REPO_ROOT/.zctmp/gm2_msw_tr_f.$$.json"
        local tput="$REPO_ROOT/.zctmp/gm2_msw_tr_p.$$.json"
        curl -s -o "$fresh" --max-time 20 "$BASE_URL/api/v1/plugins/llm_service/config/llm" \
            -H "Authorization: Bearer $MSW_RESTORE_TOK" >/dev/null 2>&1
        msw_py restore "$MSW_SNAPSHOT_FILE" "$fresh" "$tput" >/dev/null 2>&1
        curl -s -o /dev/null --max-time 20 -X PUT \
            "$BASE_URL/api/v1/plugins/llm_service/config/llm" \
            -H "Authorization: Bearer $MSW_RESTORE_TOK" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$tput")" >/dev/null 2>&1
        rm -f "$fresh" "$tput"
        return 0
    }
    msw_put() { # $1=PUT 载荷 json → stdout=http_code
        curl -s -o /dev/null -w '%{http_code}' --max-time 20 -X PUT \
            "$BASE_URL/api/v1/plugins/llm_service/config/llm" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$1")"
    }
    msw_get() { # $1=out_json → stdout=http_code（config/llm GET 落盘）
        curl -s -o "$1" -w '%{http_code}' --max-time 20 \
            "$BASE_URL/api/v1/plugins/llm_service/config/llm" -H "Authorization: Bearer $tok"
    }
    msw_fresh_patch_put() { # $1=模型名 → stdout=ok|<stage>:<code>（switch/bad/
        # switchback 三写点公共路径：PUT 前 fresh GET 取当轮 etag 再 patch+PUT——
        # 初快照 etag 随每次成功 PUT 翻新，复用必撞 if_match 乐观锁 409=R22-M1；
        # restore 步同模式先例，patch 模式本就消费传入 GET 视图的 etag）
        local gcode pcode
        gcode="$(msw_get "$cfg_file")"
        [[ "$gcode" == "200" ]] || { echo "get:$gcode"; return 0; }
        msw_py patch "$cfg_file" "$put_file" "$1" >/dev/null
        pcode="$(msw_put "$put_file")"
        [[ "$pcode" == "200" ]] || { echo "put:$pcode"; return 0; }
        echo "ok"
    }
    msw_msg_count() { # $1=sid → 该会话 messages slot 计数（stdout=整数；非 200/
        # 解析失败输出空=取证键不可用，调用方走兜底）
        local mcode
        mcode="$(curl -s -o "$msgs_file" -w '%{http_code}' --max-time 20 \
            "$BASE_URL/api/v1/sessions/$1/messages" -H "Authorization: Bearer $tok")"
        [[ "$mcode" == "200" ]] || { echo ""; return 0; }
        msw_py msgcount "$msgs_file"
    }
    msw_state() { # $1=out_json → GET /pipelines/state 落盘（会话账）
        curl -s --max-time 20 "$BASE_URL/api/v1/pipelines/state" \
            -H "Authorization: Bearer $tok" -o "$1"
    }
    msw_new_session() { # → sid（空=失败）
        curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}' \
            | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4
    }
    msw_chat() { # $1=sid $2=消息 → stdout=回复体（CJK 走临时文件，message 步同款）
        printf '{"message":"%s","session_id":"%s"}' "$2" "$1" > "$body_file"
        curl -s --max-time "$MODELSWITCH_CHAT_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$body_file")"
    }
    local dev_yaml="files/user_root/config/plugins/llm/llm.yaml"
    yaml_val() { # $1=key → 设备 user llm.yaml 该键值（键文件内唯一：chat/large/
        # medium/small 仅 defaults/tiers 段持有；awk 取首字段对缩进不敏感）
        adb_device_cmd -s "$serial" shell \
            "run-as $PACKAGE awk '\$1==\"$1:\"{print \$2; exit}' $dev_yaml 2>/dev/null" | tr -d '\r\n'
    }
    msw_new_pid() { # $1=thread_id → 该会话最新 pipeline_id（轮询 5×2s 等
        # stage_finalize 会话账快照落账；空=未出现。pid-差集排除参数随 R22-M2
        # 撤销——pipeline 随 thread 稳定，差集法结构性不可判）
        local try now
        for try in 1 2 3 4 5; do
            msw_state "$state_file"
            now="$(msw_py pids "$state_file" "$1" | grep -v '^$' | tail -1)"
            [[ -n "$now" ]] && { echo "$now"; return 0; }
            sleep 2
        done
        return 1
    }
    trap 'msw_trap_restore' EXIT
    # ②模型选定（快照事实；A 缺省=当前 defaults.chat，B 缺省参数值/同源回退）
    local facts a_pick a_name base_name b_pick b_name t_l t_m t_s
    facts="$(msw_py facts "$snap_file" "$MODELSWITCH_MODEL_A" "$MODELSWITCH_MODEL_B")"
    a_pick="$(printf '%s\n' "$facts" | sed -n 's/^A=//p')"
    a_name="$(printf '%s\n' "$facts" | sed -n 's/^ANAME=//p')"
    base_name="$(printf '%s\n' "$facts" | sed -n 's/^BASE=//p')"
    b_pick="$(printf '%s\n' "$facts" | sed -n 's/^B=//p')"
    b_name="$(printf '%s\n' "$facts" | sed -n 's/^BNAME=//p')"
    t_l="$(printf '%s\n' "$facts" | sed -n 's/^TIER_LARGE=//p')"
    t_m="$(printf '%s\n' "$facts" | sed -n 's/^TIER_MEDIUM=//p')"
    t_s="$(printf '%s\n' "$facts" | sed -n 's/^TIER_SMALL=//p')"
    if [[ -z "$a_pick" ]]; then
        trap - EXIT
        emit_result model_switch FAIL "A 解析失败（快照无 defaults.chat；--model-a 显式指定可解）"
        return 1
    fi
    if [[ -z "$b_pick" ]]; then
        trap - EXIT
        emit_result model_switch FAIL "B 不可用（--model-b $MODELSWITCH_MODEL_B 不在设备 models 映射且同 provider 无异于 A=$a_pick 的其他档；--model-b 显式指定真实键可解，大小写敏感）"
        return 1
    fi
    if [[ "$b_pick" != "$MODELSWITCH_MODEL_B" ]]; then
        log "model_switch: B 回退 $MODELSWITCH_MODEL_B → $b_pick（映射缺失或与 A 同档，同 provider 真实键兜底）"
    fi
    # ③旧会话出生+切换前基线轮（断言 d 对象+会话账取证面自证：基线轮
    #   llm_model==BASE=切前 tiers.large/defaults.chat 的解析落地）
    local chain_ok=1 problems="" old_sid base_reply base_pid="" base_model=""
    local base_msg_n="" old_evidence=""
    old_sid="$(msw_new_session)"
    [[ -n "$old_sid" ]] || { chain_ok=0; problems+="old-session-create;"; }
    if [[ "$chain_ok" -eq 1 ]]; then
        base_reply="$(msw_chat "$old_sid" "切换前基线消息，请只回复两个字：收到。不要使用任何工具。")"
        printf '%s' "$base_reply" | grep -q '"content"' || { chain_ok=0; problems+="baseline-chat-no-content;"; }
    fi
    if [[ "$chain_ok" -eq 1 ]]; then
        if base_pid="$(msw_new_pid "$old_sid")"; then
            base_model="$(msw_py model_of "$state_file" "$base_pid")"
            [[ "$base_model" == "$base_name" ]] || problems+="baseline-model(${base_model:-空}!=$base_name);"
        else
            problems+="baseline-pid-missing(会话账无该轮行,取证面不可用);"
        fi
        # 断言 d 取证键基线（R22-M2）：messages slot 计数 N——pipeline 随 thread
        # 稳定、切后无新 pid 可差，改按 slot 计数增量（N→N+2）证新轮落库；基线
        # pid 仍在案，跟进轮后会话账同 pid 行 llm_model 原地翻新=新轮模型读数
        base_msg_n="$(msw_msg_count "$old_sid")"
        [[ "$base_msg_n" =~ ^[0-9]+$ ]] || base_msg_n=""
    fi
    # ④a) 切换：defaults.chat+tiers 三档齐切 → B（坑㊵ 契约，向导同构载荷）
    local sw_ok=1 sw_out
    if [[ "$chain_ok" -eq 1 ]]; then
        sw_out="$(msw_fresh_patch_put "$b_pick")"
        if [[ "$sw_out" != "ok" ]]; then
            sw_ok=0
            problems+="switch-put(${sw_out#*:},stage=${sw_out%%:*};fresh-get+put 非 200 可重跑);"
        else
            # REST 视图回读断言（写面成功≠视图翻新，分开证）
            msw_get "$cfg_file" >/dev/null
            sw_out="$(msw_py check "$cfg_file" "$b_pick")"
            if [[ "$(printf '%s\n' "$sw_out" | sed -n 's/^CHECK=//p')" == "ok" ]]; then
                :
            else
                problems+="switch-verify($(printf '%s\n' "$sw_out" | sed -n 's/^NOW=//p'));"
            fi
        fi
    fi
    # ⑤b) 新会话生效 + ⑥c) 设备文件面 tiers 齐切 + ⑦d) 旧会话跟随
    local new_sid new_pid new_model="" old_reply2 old_model2=""
    local f_chat f_large f_medium f_small
    if [[ "$sw_ok" -eq 1 ]]; then
        new_sid="$(msw_new_session)"
        if [[ -z "$new_sid" ]]; then
            problems+="new-session-create;"
        elif printf '%s' "$(msw_chat "$new_sid" "切换后新会话探针，请只回复两个字：收到。不要使用任何工具。")" | grep -q '"content"'; then
            if new_pid="$(msw_new_pid "$new_sid")"; then
                new_model="$(msw_py model_of "$state_file" "$new_pid")"
                [[ "$new_model" == "$b_name" ]] || problems+="new-session-model(${new_model:-空}!=$b_name);"
            else
                problems+="new-session-pid-missing(会话账无该轮行);"
            fi
        else
            problems+="new-session-chat-no-content;"
        fi
        f_chat="$(yaml_val chat)"
        f_large="$(yaml_val large)"
        f_medium="$(yaml_val medium)"
        f_small="$(yaml_val small)"
        if [[ "$f_chat" == "$b_pick" && "$f_large" == "$b_pick" && "$f_medium" == "$b_pick" && "$f_small" == "$b_pick" ]]; then
            :
        else
            problems+="tiers-file(chat=$f_chat,large=$f_large,medium=$f_medium,small=$f_small,want=$b_pick);"
        fi
        # ⑦d) 旧会话跟随新默认：取证键=messages slot 计数增量定位新轮（R22-M2：
        # pipeline 随 thread 稳定无新 pid 可差）→ 会话账同 pid 行 llm_model 翻新值；
        # logcat 清场收窄窗口=仅本轮对话（兜底取证用，主路径不受影响）
        adb_t 20 -s "$serial" logcat -c >/dev/null 2>&1
        old_reply2="$(msw_chat "$old_sid" "切换后再聊一句，请只回复两个字：正常。不要使用任何工具。")"
        if printf '%s' "$old_reply2" | grep -q '"content"'; then
            local old_n="$base_msg_n" n2="" follow_n="" try
            if [[ "$old_n" =~ ^[0-9]+$ ]]; then
                for try in 1 2 3 4 5; do
                    n2="$(msw_msg_count "$old_sid")"
                    if [[ "$n2" =~ ^[0-9]+$ && "$n2" -ge $((old_n + 2)) ]]; then
                        follow_n="$n2"
                        break
                    fi
                    sleep 2
                done
            fi
            if [[ -n "$follow_n" && -n "$base_pid" ]]; then
                # 新轮已落 slot（user+assistant ≥+2）→ 同 pid 行读 llm_model 原地翻新值
                msw_state "$state_file"
                old_model2="$(msw_py model_of "$state_file" "$base_pid")"
                if [[ "$old_model2" == "$b_name" ]]; then
                    old_evidence="slot计数${old_n}→${follow_n}+state行翻新"
                else
                    problems+="old-session-model(${old_model2:-空}!=$b_name,跟随语义不符);"
                fi
            else
                # 兜底取证（slot 计数不可靠窗口：④-A 修复在飞落库语义过渡）=
                # 内核日志 Calling LLM: model= 行——llm_core 每调用记录实际解析
                # 模型 self._model=model_name 与 llm_model 同源；LLM call succeeded
                # 行与 chat REST 响应体均无模型字段（WsResponse 仅 type/content/
                # session_id/timestamp/degraded/failed）故取此行；窗口内唯一调用
                # 取末行（探针禁工具=单调用；无并发轮）
                local log_line log_model=""
                log_line="$(adb_t 30 -s "$serial" logcat -d -s LingxiKernelHost 2>/dev/null \
                    | tr -d '\r' | grep 'Calling LLM: model=' | tail -1)"
                log_model="$(printf '%s\n' "$log_line" | sed -n 's/.*Calling LLM: model=\([^,]*\),.*/\1/p')"
                if [[ -n "$log_model" ]]; then
                    if [[ "$log_model" == "$b_name" ]]; then
                        old_evidence="内核日志CallingLLM兜底(model=$log_model)"
                    else
                        problems+="old-session-model(${log_model}!=$b_name,跟随语义不符[日志兜底取证]);"
                    fi
                else
                    problems+="old-session-evidence-unavailable(slot计数未增[${old_n:-空}→${n2:-空}]且日志无CallingLLM行);"
                fi
            fi
        else
            problems+="old-session-chat2-no-content;"
        fi
    fi
    # ⑧e) 失效模型探针（不假死+切回恢复硬断言；降级语义探针记录不硬写——现状
    #   依据见函数头 e）
    local bad_sid bad_reply bad_pid="" bad_model="" bad_deg=0 bad_fail=0 bad_note=""
    if [[ "$sw_ok" -eq 1 ]]; then
        sw_out="$(msw_fresh_patch_put "$MODELSWITCH_BAD_MODEL")"
        if [[ "$sw_out" != "ok" ]]; then
            problems+="bad-put(${sw_out#*:},stage=${sw_out%%:*});"
        else
            bad_sid="$(msw_new_session)"
            if [[ -z "$bad_sid" ]]; then
                problems+="bad-session-create;"
            else
                bad_reply="$(msw_chat "$bad_sid" "失效模型探针，请只回复两个字：收到。不要使用任何工具。")"
                if printf '%s' "$bad_reply" | grep -q '"content"'; then
                    # 不假死=上限内返回有 content（真回复/失败直写都算返回）
                    if bad_pid="$(msw_new_pid "$bad_sid")"; then
                        bad_model="$(msw_py model_of "$state_file" "$bad_pid")"
                    fi
                    printf '%s' "$bad_reply" | grep -q '"degraded": *true' && bad_deg=1
                    printf '%s' "$bad_reply" | grep -q '失败' && bad_fail=1
                    if [[ "$bad_model" == "$b_name" || "$bad_model" == "$a_name" ]]; then
                        bad_note="静默回落旧模型(${bad_model}) degraded=${bad_deg} 失败文本=${bad_fail}"
                    elif [[ "$bad_model" == "$MODELSWITCH_BAD_MODEL" ]]; then
                        bad_note="如实上抛映射外模型 degraded=${bad_deg} 失败文本=${bad_fail}"
                    else
                        bad_note="会话账模型未辨识(${bad_model:-空}) degraded=${bad_deg} 失败文本=${bad_fail}"
                    fi
                else
                    problems+="bad-model-hang(>${MODELSWITCH_CHAT_TIMEOUT_S}s-no-content);"
                    bad_note="假死或无 content"
                fi
            fi
        fi
        # ⑨切回 A 恢复
        sw_out="$(msw_fresh_patch_put "$a_pick")"
        if [[ "$sw_out" != "ok" ]]; then
            problems+="switchback-put(${sw_out#*:},stage=${sw_out%%:*});"
        else
            local back_sid back_pid back_model=""
            back_sid="$(msw_new_session)"
            if [[ -n "$back_sid" ]] && printf '%s' "$(msw_chat "$back_sid" "切回后探针，请只回复两个字：收到。不要使用任何工具。")" | grep -q '"content"'; then
                if back_pid="$(msw_new_pid "$back_sid")"; then
                    back_model="$(msw_py model_of "$state_file" "$back_pid")"
                    [[ "$back_model" == "$a_name" ]] || problems+="switchback-model(${back_model:-空}!=$a_name);"
                else
                    problems+="switchback-pid-missing(会话账无该轮行);"
                fi
            else
                problems+="switchback-chat-no-content;"
            fi
        fi
    fi
    # ⑩还原快照（REST PUT，fresh etag 防漂移）+ 双面对照（REST 视图+设备文件）
    local restored=1 fresh_file="$REPO_ROOT/.zctmp/gm2_msw_fresh.$$.json" r_out
    http_code="$(msw_get "$fresh_file")"
    if [[ "$http_code" != "200" ]]; then
        restored=0
        problems+="restore-get($http_code);"
    else
        msw_py restore "$snap_file" "$fresh_file" "$put_file" >/dev/null
        http_code="$(msw_put "$put_file")"
        if [[ "$http_code" != "200" ]]; then
            restored=0
            problems+="restore-put($http_code);"
        else
            msw_get "$cfg_file" >/dev/null
            r_out="$(msw_py check "$cfg_file" "$a_pick")"
            if [[ "$(printf '%s\n' "$r_out" | sed -n 's/^CHECK=//p')" != "ok" ]]; then
                restored=0
                problems+="restore-verify($(printf '%s\n' "$r_out" | sed -n 's/^NOW=//p'));"
            fi
            f_chat="$(yaml_val chat)"
            f_large="$(yaml_val large)"
            f_medium="$(yaml_val medium)"
            f_small="$(yaml_val small)"
            if [[ "$f_chat" == "$a_pick" && "$f_large" == "$t_l" && "$f_medium" == "$t_m" && "$f_small" == "$t_s" ]]; then
                :
            else
                restored=0
                problems+="restore-file(chat=$f_chat,large=$f_large,medium=$f_medium,small=$f_small);"
            fi
        fi
    fi
    if [[ "$restored" -eq 1 ]]; then
        trap - EXIT
        rm -f "$snap_file" "$cfg_file" "$state_file" "$body_file" "$put_file" "$fresh_file" "$msgs_file"
    else
        # 还原不完整：trap 留守（脚本退出再试一次），快照文件留守给兜底用
        rm -f "$cfg_file" "$state_file" "$body_file" "$put_file" "$fresh_file" "$msgs_file"
        log "model_switch: 快照还原不完整，脚本退出时兜底重试（详见 RESULT problems）"
    fi
    if [[ -z "$problems" ]]; then
        emit_result model_switch PASS "模型切换全链绿（A=$a_pick→B=$b_pick 新会话生效=$b_name 旧会话跟随=$b_name[${old_evidence:-无}] tiers文件齐切=yes 失效探针=${bad_note:-跳过} 切回A=$a_name 恢复 快照已还原[REST+文件双对照]）"
        return 0
    fi
    emit_result model_switch FAIL "model_switch 红: ${problems%;} A=$a_pick B=$b_pick 失效探针=${bad_note:-跳过}"
    return 1
}

# G-M2 第 17 步（精细测试队列⑦车道化，2026-10-16 脚本先行；MuMu 实跑待空档不
# 属本刀）：息屏保活自动化——只覆盖机械面（息屏不停服/息屏任务推进/亮屏恢复/
# FGS 现状取证）。
#
# ⚠ 挂账标注：MuMu 模拟器息屏语义与真机省电/Doze 深度行为差异大——电流、Doze
# 深睡窗口、厂商省电杀后台等定谳须真机（aarch64）轮，本车道不覆盖也不假装覆盖
# （真机面归移动版精细测试台账队列⑦「前台服务+息屏三档自适应」现状采集）。
#
# 息屏手段与生效判据（2026-10-16 MuMu Android 15 实跑校准）：
#  - 息屏主=`adb shell input keyevent 223`（KEYCODE_SLEEP，真睡眠语义非 toggle；
#    实测 POWER(26) 在 MuMu 被吞——stay_on_while_plugged_in=2147483647 恒插电
#    且键不达睡眠账，判读恒 Awake），未生效再 POWER 兜底一次（AOSP 真机语义）；
#    醒屏主手段不用 POWER 防「再按一下又睡了」：KEYCODE_WAKEUP(224) 单向醒屏
#    +KEYCODE_MENU(82) 解非安全锁（MuMu 无安全锁屏，menu 等价滑动解锁；实测
#    从 Dozing 醒屏有效），仍未醒兜底再 POWER 一次；
#  - 生效判据=dumpsys power：mWakefulness=Asleep（AOSP 账面）或 Dozing（MuMu
#    Android 15 实测睡眠落点，KEYCODE_SLEEP 后即此值）任一成立=息屏；醒屏=
#    mWakefulness=Awake。本 ROM 无「Display Power: state=」行（显示账字段缺席，
#    旁证可取 dumpsys display mScreenState）。判据字段皆读不到=不可用→FAIL
#    指路实机校准（不跳过不假绿）。
#
# 断言面（断用户可见行为）：
#  a) 息屏不停服：息屏确认后内核 health 连续 --keepalive-health-polls 轮（间隔
#     --keepalive-health-interval-s；失败轮经 adb 探活重连后当场重试一次=MuMu
#     瞬断假阴性口径，adb_is_alive 头注释在案）全 200——proot 栈不被系统杀
#     （保活三件 R5 落地面：FGS+电池白名单引导+厂商自启适配清单
#     docs/working/移动端保活与厂商自启适配清单_20261014.md）。息屏期 idle GC
#     依正常策略卸载宿主不违本断言：a 断的是内核进程存活（health 200），非宿主
#     常驻；
#  b) 息屏任务推进：息屏前 REST 建任务（/ext/task_service/tasks/root，task_run
#     同款端点+缺陷E Workaround 同款内联[十六步函数体零触碰纪律故不抽公共]）并
#     等到非终态快照（息屏前状态）；息屏窗口尾部取中途状态快照（记录面）；亮屏
#     后轮询终态（上限 --keepalive-task-timeout-s）：断言 completed 到达（failed/
#     cancelled 如实红）；亮屏后仍非终态且与息屏前同状态=task-no-progress 红。
#     前→中→后三态对比入 evidence——中途快照已 completed=息屏窗口内推进的强证；
#     息屏前 10s 内已 completed=任务过快撞窗（raced），推进证据弱只记录不红
#     （机械面由 a/c 兜底）；
#  c) 亮屏恢复：KEYCODE_WAKEUP+menu→display 醒态确认→新会话对话真回复
#     degraded=false（允许一次重试=首调缝 R11 在案：息屏期 idle GC 卸载宿主后
#     首调含 respawn 延迟；第二次仍败才红）；
#  d) FGS 现状记录（只记录不断言理想值——通知面现状=AR 轮改过的语义，用户裁定
#     「通知栏不要一直显示」落地面见 app-mobile KernelHostPlugin.java 2026-10-15
#     通知 UX）：息屏窗口内 dumpsys activity services 壳包名+dumpsys notification
#     取证 FGS 服务与通知存活现状。现状语义依据（KernelHostPlugin.java）：FGS
#     挂起条件=local 模式+本机栈在跑+app 已退后台+用户未从通知手动停止
#     （fgs_suppressed）——息屏=Activity onStop=退后台，FGS 应补挂；通知渠道
#     LOW=状态栏无常驻图标、仅抽屉可见，带「停止」按钮；净装态
#     POST_NOTIFICATIONS 未授权时服务在挂但通知零记录（R11 发现(a) 在案）——
#     两计数仅入 evidence，红绿不受其影响。
#
# 前置：$KERNEL_PORT 产品化链驻留（health 200 自查不代拉起，proot_boot 同款）+
# 已配 key（设备 user .env *_API_KEY 计数零落账；纯 pm clear 态预期 FAIL 属产品
# 语义，同 message 步）。
# 还原保证：任何退出路径 trap EXIT 醒屏兜底（WAKEUP+menu），防设备留息屏态污染
# 同实例后续车道（续步虽多不依赖亮屏，保持现场亮屏=既有车道默认态）。
step_screen_keepalive() {
    local serial
    serial="$(resolve_serial)" || { emit_result screen_keepalive FAIL "无可用设备"; return 1; }
    require_adb
    [[ -n "$KERNEL_USER" && -n "$KERNEL_PASS" ]] || {
        emit_result screen_keepalive FAIL "缺 --user/--password（AGENTOS_KERNEL_USER/PASS）"
        return 1
    }
    KEEPALIVE_RESTORE_SERIAL="$serial"
    adb_t 20 -s "$serial" forward "tcp:$KERNEL_PORT" "tcp:$KERNEL_PORT" >/dev/null 2>&1 || {
        emit_result screen_keepalive FAIL "adb forward tcp:$KERNEL_PORT 失败"
        return 1
    }
    # 前置自查①：产品化链驻留（health 200，不代拉起）
    local pre_code
    pre_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$BASE_URL/health" 2>/dev/null)"
    [[ "$pre_code" == "200" ]] || {
        emit_result screen_keepalive FAIL "前置不满足：$KERNEL_PORT 无驻留内核（先跑 proot_boot 产品化链）"
        return 1
    }
    # 前置自查②：已配 key（设备 user .env *_API_KEY 键数；只取计数，key 现值零落账）
    local keycount
    keycount="$(adb_device_cmd -s "$serial" shell \
        "run-as $PACKAGE sh -c 'grep -c _API_KEY files/user_root/.env'" 2>/dev/null | tr -d '\r\n ')"
    [[ "${keycount:-0}" -ge 1 ]] 2>/dev/null || {
        emit_result screen_keepalive FAIL "前置不满足：设备 user .env 无 *_API_KEY（未配 key；纯 pm clear 态预期 FAIL 属产品语义）"
        return 1
    }
    local tok
    tok="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"username\":\"$KERNEL_USER\",\"password\":\"$KERNEL_PASS\"}" \
        | grep -oE '"access_token":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$tok" ]] || {
        emit_result screen_keepalive FAIL "login 失败"
        return 1
    }
    local problems=""
    # ── 工具函数（步内定义；消费 $serial/$tok 动态作用域+KA_*/KEEPALIVE_* 全局
    #    供 trap 兜底消费）──
    ka_disp_state() { # → asleep|awake|unknown（AOSP 双字段任一成立即判；副产
        # KA_PW_LAST=两字段摘要供 evidence）
        local raw
        raw="$(adb_device_cmd -s "$KEEPALIVE_RESTORE_SERIAL" shell "dumpsys power 2>/dev/null" | tr -d '\r')"
        KA_PW_LAST="$(printf '%s\n' "$raw" | grep -m1 -oE 'mWakefulness=[A-Za-z]+')/$(printf '%s\n' "$raw" | grep -m1 -oE 'Display Power: state=[A-Z]+')"
        [[ "$KA_PW_LAST" == "/" ]] && KA_PW_LAST="无判据行"
        # 睡判据：Asleep（AOSP 账面）或 Dozing（MuMu Android 15 实测落点，
        # 2026-10-16 实跑校准——KEYCODE_SLEEP 后 mWakefulness=Dozing）
        if printf '%s\n' "$raw" | grep -qE 'mWakefulness=(Asleep|Dozing)|Display Power: state=OFF'; then
            echo asleep
        elif printf '%s\n' "$raw" | grep -qE 'mWakefulness=Awake|Display Power: state=ON'; then
            echo awake
        else
            echo unknown
        fi
    }
    ka_wake() { # 醒屏兜底：WAKEUP 单向+menu 解锁（不用 POWER 防再按又睡）
        adb_device_cmd -s "$KEEPALIVE_RESTORE_SERIAL" shell \
            "input keyevent 224; sleep 1; input keyevent 82" >/dev/null 2>&1
        return 0
    }
    ka_task_status() { # $1=task_id → status 字符串（空=不可得；task_run 同款抽取）
        curl -s --max-time 10 "$BASE_URL/ext/task_service/tasks/$1" \
            -H "Authorization: Bearer $tok" 2>/dev/null \
            | grep -oE '"status": *"[a-z_]+"' | head -1 | grep -oE '[a-z_]+' | tail -1
    }
    KA_BODY_FILE="$REPO_ROOT/.zctmp/gm2_keepalive.$$.json"
    ka_chat() { # $1=sid $2=消息 → 回复体（CJK 走临时文件，message 步同款）
        printf '{"message":"%s","session_id":"%s"}' "$2" "$1" > "$KA_BODY_FILE"
        curl -s --max-time "$KEEPALIVE_CHAT_TIMEOUT_S" -X POST "$BASE_URL/api/v1/chat" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$KA_BODY_FILE")"
    }
    trap 'ka_wake' EXIT
    # ①基线醒态校准（息屏前须亮；息屏语义=POWER toggle，基线即睡则先醒）
    local base_state
    base_state="$(ka_disp_state)"
    if [[ "$base_state" == "asleep" ]]; then
        ka_wake
        sleep 2
        base_state="$(ka_disp_state)"
    fi
    ka_disp_state >/dev/null # KA_PW_LAST 在 $() 子壳内置值不落父壳——父壳直调一次补取证摘要
    [[ "$base_state" == "awake" ]] || {
        trap - EXIT
        emit_result screen_keepalive FAIL "dumpsys power 判据字段不可读（base=$base_state 摘要=$KA_PW_LAST——AOSP mWakefulness/Display Power 两字段皆未命中，MuMu 定制 ROM 字段形态待实跑校准，如实红报备）"
        return 1
    }
    # ②b 前置：建会话+息屏前发起长动作（建任务+等非终态快照=推进对比基线）
    local sess sid
    sess="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}')"
    sid="$(printf '%s' "$sess" | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    [[ -n "$sid" ]] || {
        trap - EXIT
        emit_result screen_keepalive FAIL "建会话失败: $(printf '%s' "$sess" | head -c 120)"
        return 1
    }
    local body_file task_resp task_id
    body_file="$REPO_ROOT/.zctmp/gm2_keepalive_task.$$.json"
    printf '{"title":"G-M2 screen_keepalive probe","description":"This is a simple direct task. Do it yourself, do NOT delegate to any sub-agent. Goal: create file gm2_keepalive_probe.txt in your current task workspace with exactly one line: G-M2-KEEPALIVE-OK . Then list the workspace directory to confirm the file exists. Finish with task_evaluate and a short report. Do NOT write any long text.","target_type":"agent","target_id":"general_agent_flash","thread_id":"%s","acceptance_criteria":{"files":["gm2_keepalive_probe.txt"]}}' "$sid" \
        > "$body_file"
    task_resp="$(curl -s --max-time 70 -X POST "$BASE_URL/ext/task_service/tasks/root" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
        --data-binary @"$(cygpath -w "$body_file")")"
    task_id="$(printf '%s' "$task_resp" | grep -oE '"id": *"[0-9a-f]+"' | head -1 | grep -oE '[0-9a-f]{8,}' | head -1)"
    if [[ -z "$task_id" ]] && printf '%s' "$task_resp" | grep -q "chat capability"; then
        # 缺陷E Workaround（task_run 同款内联；respawn task_submit 宿主换新握手）
        log "screen_keepalive: 首建命中 chat capability 未注入（缺陷E），respawn task_submit_tool 后重试"
        curl -s --max-time 20 -X PUT "$BASE_URL/api/v1/plugins/task_submit_tool/enabled" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            -d '{"enabled":false}' >/dev/null 2>&1
        sleep 3
        curl -s --max-time 30 -X PUT "$BASE_URL/api/v1/plugins/task_submit_tool/enabled" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            -d '{"enabled":true}' >/dev/null 2>&1
        sleep 5
        task_resp="$(curl -s --max-time 70 -X POST "$BASE_URL/ext/task_service/tasks/root" \
            -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' \
            --data-binary @"$(cygpath -w "$body_file")")"
        task_id="$(printf '%s' "$task_resp" | grep -oE '"id": *"[0-9a-f]+"' | head -1 | grep -oE '[0-9a-f]{8,}' | head -1)"
    fi
    rm -f "$body_file"
    [[ -n "$task_id" ]] || {
        trap - EXIT
        emit_result screen_keepalive FAIL "建任务失败: $(printf '%s' "$task_resp" | head -c 160)"
        return 1
    }
    log "screen_keepalive: 任务已出生 task_id=$task_id，等非终态快照（息屏前基线）"
    local pre_status="" psi raced_note=""
    for ((psi = 1; psi <= 5; psi++)); do
        sleep 2
        pre_status="$(ka_task_status "$task_id")"
        case "$pre_status" in
            "" ) : ;; # 不可得，继续等（任务出生→executor 接手有延迟）
            completed|failed|cancelled|canceled) break ;;
            *) break ;; # pending/running/…=非终态快照到手
        esac
    done
    case "$pre_status" in
        completed) raced_note="息屏前已completed(推进证据弱)" ;;
        "") problems+="task-status-unreadable(息屏前快照不可得);" ;;
    esac
    # ③息屏：主=KEYCODE_SLEEP(223)（真睡眠语义非 toggle；MuMu 实测 POWER 被吞
    # [stay_on_while_plugged_in=2147483647 恒插电+键不达睡眠账]=2026-10-16 实跑
    # 校准），未生效再 POWER(26) 兜底一次（AOSP 真机语义）；重试=动画/延迟余量
    adb_device_cmd -s "$serial" shell "input keyevent 223" >/dev/null 2>&1
    sleep 3
    local off_state
    off_state="$(ka_disp_state)"
    if [[ "$off_state" != "asleep" ]]; then
        adb_device_cmd -s "$serial" shell "input keyevent 26" >/dev/null 2>&1
        sleep 3
        off_state="$(ka_disp_state)"
    fi
    ka_disp_state >/dev/null # 同上：补取证摘要（生效日志行与 FAIL 报文都消费 KA_PW_LAST）
    [[ "$off_state" == "asleep" ]] || {
        ka_wake
        trap - EXIT
        emit_result screen_keepalive FAIL "息屏手段未生效（KEYCODE_SLEEP+POWER 兜底后 display 判读=${off_state} 摘要=$KA_PW_LAST——如实报备不硬写）"
        return 1
    }
    local t_off=$SECONDS
    log "screen_keepalive: 息屏生效（$KA_PW_LAST），health 轮询 ${KEEPALIVE_HEALTH_POLLS}×${KEEPALIVE_HEALTH_INTERVAL_S}s 起"
    # ④a 息屏不停服：health 轮询 N×interval（失败轮 adb 探活重连后当场重试一次）
    local health_ok=0 pi code
    for ((pi = 1; pi <= KEEPALIVE_HEALTH_POLLS; pi++)); do
        sleep "$KEEPALIVE_HEALTH_INTERVAL_S"
        code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$BASE_URL/health" 2>/dev/null)"
        if [[ "$code" != "200" ]]; then
            adb_is_alive "$serial" || adb_reconnect "$serial"
            code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$BASE_URL/health" 2>/dev/null)"
        fi
        [[ "$code" == "200" ]] && health_ok=$((health_ok + 1))
    done
    [[ "$health_ok" -eq "$KEEPALIVE_HEALTH_POLLS" ]] \
        || problems+="health-down-during-off(${health_ok}/${KEEPALIVE_HEALTH_POLLS});"
    # ⑤d FGS 现状取证（息屏窗口内；只记录不断言——通知面现状=AR 轮改过的语义）
    local fgs_svc fgs_notif fgs_line
    fgs_svc="$(adb_device_cmd -s "$serial" shell \
        "dumpsys activity services $PACKAGE 2>/dev/null | grep -cE 'ServiceRecord'" | tr -d '\r\n ')"
    fgs_line="$(adb_device_cmd -s "$serial" shell \
        "dumpsys activity services $PACKAGE 2>/dev/null | grep -m1 -E 'foregroundServiceType|isForeground|AndroidForegroundService'" \
        | tr -d '\r' | tr -s ' ' | head -c 100)"
    fgs_notif="$(adb_device_cmd -s "$serial" shell \
        "dumpsys notification --noredact 2>/dev/null | grep -c 'pkg=$PACKAGE'" | tr -d '\r\n ')"
    [[ "$fgs_svc" =~ ^[0-9]+$ ]] || fgs_svc="?"
    [[ "$fgs_notif" =~ ^[0-9]+$ ]] || fgs_notif="?"
    log "screen_keepalive: FGS 现状（记录不断言）ServiceRecord=${fgs_svc} 通知=${fgs_notif} 行=${fgs_line:-无}"
    # ⑥b 中途状态快照（息屏尾部；已 completed=息屏窗口内推进的强证）+补足窗口
    local mid_status
    mid_status="$(ka_task_status "$task_id")"
    local remain=$((KEEPALIVE_SCREEN_OFF_S - (SECONDS - t_off)))
    (( remain > 0 )) && sleep "$remain"
    # ⑦c 亮屏：WAKEUP+menu→确认；未醒再 POWER 一次（toggle 补救）→仍未醒=wake-failed
    ka_wake
    sleep 2
    local wake_state
    wake_state="$(ka_disp_state)"
    if [[ "$wake_state" != "awake" ]]; then
        adb_device_cmd -s "$serial" shell "input keyevent 26" >/dev/null 2>&1
        sleep 2
        wake_state="$(ka_disp_state)"
        [[ "$wake_state" == "awake" ]] || problems+="wake-failed(${wake_state});"
    fi
    # ⑧b 亮屏后任务终态轮询（息屏推进对比=前→中→后三态；上限内 completed 才绿）
    local post_status="" bi task_flag="running"
    for ((bi = 5; bi <= KEEPALIVE_TASK_TIMEOUT_S; bi += 5)); do
        sleep 5
        post_status="$(ka_task_status "$task_id")"
        case "$post_status" in
            completed) task_flag="completed"; break ;;
            failed|cancelled|canceled) task_flag="failed"; break ;;
        esac
        if ! adb_is_alive "$serial"; then
            adb_reconnect "$serial"
        fi
    done
    if [[ "$task_flag" == "failed" ]]; then
        problems+="task-terminal-${post_status};"
    elif [[ "$task_flag" != "completed" ]]; then
        if [[ -z "$post_status" ]]; then
            problems+="task-timeout(${KEEPALIVE_TASK_TIMEOUT_S}s,状态不可得);"
        elif [[ "$post_status" == "$pre_status" ]]; then
            problems+="task-no-progress(${post_status}同息屏前);"
        else
            problems+="task-timeout(${KEEPALIVE_TASK_TIMEOUT_S}s,状态=${post_status});"
        fi
    fi
    # ⑨c 亮屏恢复对话：新会话真回复 degraded=false（允许一次重试=首调缝/idle
    #    respawn 延迟，第二次仍败才红）
    local c_ok=0 c_try=0 c_reply="" c_sid c_reply_head=""
    c_sid="$(curl -s --max-time 15 -X POST "$BASE_URL/api/v1/sessions" \
        -H "Authorization: Bearer $tok" -H 'Content-Type: application/json' -d '{}' \
        | grep -oE '"thread_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
    if [[ -z "$c_sid" ]]; then
        problems+="wake-session-create;"
    else
        for c_try in 1 2; do
            c_reply="$(ka_chat "$c_sid" "亮屏恢复探针，请只回复两个字：收到。不要使用任何工具。")"
            if printf '%s' "$c_reply" | grep -q '"degraded": *false' \
                && printf '%s' "$c_reply" | grep -q '"content"'; then
                c_ok=1
                break
            fi
            sleep 10
        done
        [[ "$c_ok" -eq 1 ]] || {
            problems+="no-wake-reply;"
            c_reply_head="$(printf '%s' "$c_reply" | head -c 120)"
        }
    fi
    # ⑩收尾：醒屏兜底幂等执行→醒态确认后解除 trap（未醒留守=脚本退出再试一次）
    ka_wake
    if [[ "$(ka_disp_state)" == "awake" ]]; then
        trap - EXIT
    else
        log "screen_keepalive: 醒屏未确认，脚本退出时兜底重试（详见 RESULT）"
    fi
    rm -f "$KA_BODY_FILE"
    if [[ -z "$problems" ]]; then
        emit_result screen_keepalive PASS "息屏保活全链绿（off=${KEEPALIVE_SCREEN_OFF_S}s 判据=$KA_PW_LAST health=${health_ok}/${KEEPALIVE_HEALTH_POLLS}×${KEEPALIVE_HEALTH_INTERVAL_S}s 任务=${pre_status:-不可得}→${mid_status:-不可得}→${post_status:-不可得}${raced_note:+[$raced_note]} 亮屏=${wake_state} 恢复对话=degraded=false[try${c_try}] FGS现状 svc=${fgs_svc} notif=${fgs_notif}[记录不断言]）"
        return 0
    fi
    emit_result screen_keepalive FAIL "screen_keepalive 红: ${problems%;} 判据=$KA_PW_LAST 任务=${pre_status:-不可得}→${post_status:-不可得} 恢复对话头: ${c_reply_head//\"/\'}"
    return 1
}

# ── 调度 ─────────────────────────────────────────────────────────────────

ALL_STEPS=(boot install seed_kernel health login proot_boot message restart_recovery cancel task_run tool_loop idle_firstcall approval upgrade network_recovery model_switch screen_keepalive)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --step) STEPS_SELECTED+=("${2:?--step 需值}"); MODE="steps"; shift 2 ;;
        --all) MODE="all"; shift ;;
        --list) MODE="list"; shift ;;
        --mumu-index) MUMU_INDEX="${2:?--mumu-index 需值}"; shift 2 ;;
        --manager) MUMU_MANAGER="${2:?--manager 需值}"; shift 2 ;;
        --serial) SERIAL="${2:?--serial 需值}"; shift 2 ;;
        --apk) APK="${2:?--apk 需值}"; shift 2 ;;
        --kernel-path) KERNEL_BIN_SRC="${2:?--kernel-path 需值}"; shift 2 ;;
        --package) PACKAGE="${2:?--package 需值}"; shift 2 ;;
        --port) KERNEL_PORT="${2:?--port 需值}"; BASE_URL="http://127.0.0.1:$KERNEL_PORT"; shift 2 ;;
        --user) KERNEL_USER="${2:?--user 需值}"; shift 2 ;;
        --password) KERNEL_PASS="${2:?--password 需值}"; shift 2 ;;
        --timeout-s) BOOT_TIMEOUT_S="${2:?--timeout-s 需值}"; shift 2 ;;
        --recovery-timeout-s) RECOVERY_TIMEOUT_S="${2:?--recovery-timeout-s 需值}"; shift 2 ;;
        --proot-boot-timeout-s) PROOT_BOOT_TIMEOUT_S="${2:?--proot-boot-timeout-s 需值}"; shift 2 ;;
        --task-run-timeout-s) TASK_RUN_TIMEOUT_S="${2:?--task-run-timeout-s 需值}"; shift 2 ;;
        --tool-loop-timeout-s) TOOL_LOOP_TIMEOUT_S="${2:?--tool-loop-timeout-s 需值}"; shift 2 ;;
        --approval-timeout-s) APPROVAL_TIMEOUT_S="${2:?--approval-timeout-s 需值}"; shift 2 ;;
        --upgrade-boot-timeout-s) UPGRADE_BOOT_TIMEOUT_S="${2:?--upgrade-boot-timeout-s 需值}"; shift 2 ;;
        --net-recovery-chat-timeout-s) NETRECOVERY_CHAT_TIMEOUT_S="${2:?--net-recovery-chat-timeout-s 需值}"; shift 2 ;;
        --net-recovery-recover-wait-s) NETRECOVERY_RECOVER_WAIT_S="${2:?--net-recovery-recover-wait-s 需值}"; shift 2 ;;
        --net-recovery-probe-ip) NETRECOVERY_PROBE_IP="${2:?--net-recovery-probe-ip 需值}"; shift 2 ;;
        --idle-firstcall-timeout-s) IDLE_FIRSTCALL_TIMEOUT_S="${2:?--idle-firstcall-timeout-s 需值}"; shift 2 ;;
        --idle-timeout-secs) IDLE_TIMEOUT_SECS="${2:?--idle-timeout-secs 需值}"; shift 2 ;;
        --model-switch-chat-timeout-s) MODELSWITCH_CHAT_TIMEOUT_S="${2:?--model-switch-chat-timeout-s 需值}"; shift 2 ;;
        --model-a) MODELSWITCH_MODEL_A="${2:?--model-a 需值}"; shift 2 ;;
        --model-b) MODELSWITCH_MODEL_B="${2:?--model-b 需值}"; shift 2 ;;
        --model-bad) MODELSWITCH_BAD_MODEL="${2:?--model-bad 需值}"; shift 2 ;;
        --keepalive-screen-off-s) KEEPALIVE_SCREEN_OFF_S="${2:?--keepalive-screen-off-s 需值}"; shift 2 ;;
        --keepalive-health-polls) KEEPALIVE_HEALTH_POLLS="${2:?--keepalive-health-polls 需值}"; shift 2 ;;
        --keepalive-health-interval-s) KEEPALIVE_HEALTH_INTERVAL_S="${2:?--keepalive-health-interval-s 需值}"; shift 2 ;;
        --keepalive-task-timeout-s) KEEPALIVE_TASK_TIMEOUT_S="${2:?--keepalive-task-timeout-s 需值}"; shift 2 ;;
        --keepalive-chat-timeout-s) KEEPALIVE_CHAT_TIMEOUT_S="${2:?--keepalive-chat-timeout-s 需值}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1（--help 看步骤链）" ;;
    esac
done

if [[ "$MODE" == "list" ]]; then
    log "步骤链: ${ALL_STEPS[*]}"
    log "状态: MuMu 口径（MuMuManager CLI 拉起 + adb connect；勿动 MuMu 实例 0）"
    log "      boot/install/seed_kernel/health/login=P0.1 原生内核口径（已绿）；"
    log "      proot_boot=产品化链（壳编排解压/装配/起核，R5）；"
    log "      message/restart_recovery=REST 全链（跑在任一驻留内核上，产品化口径带 --port 9300）"
    log "      cancel=取消传播 WS 时序断言（R1 同刀；前置=message 同款：内核驻留+已配 key）"
    log "      task_run=完整任务执行链 REST 断言（R2 同刀；前置=proot_boot+已配 key；轮询上限 --task-run-timeout-s 默认 300s）"
    log "      tool_loop=agentic loop 依赖链断言（R6 同刀；前置=message 同款：内核驻留+已配 key；chat 同步上限 --tool-loop-timeout-s 默认 300s）"
    log "      idle_firstcall=idle 回收后首调断言（R7 同刀；前置=proot_boot+已配 key；.env 注入+重启+静置 GC+首调 ≤--idle-firstcall-timeout-s 默认 10s+宿主账+还原；阈值 --idle-timeout-secs 默认 15s）"
    log "      approval=审批流断言（R8 同刀；前置=message 同款：内核驻留+已配 key；default 档+写区授权卡+API 批准 permanent+落盘+名单还原；单轮上限 --approval-timeout-s 默认 300s）"
    log "      upgrade=覆盖安装升级断言（R9 同刀，可选步；前置=proot_boot 产品化链驻留；--apk 新包 install -r→versionCode 单调+凭据/探针会话/.env 数据保全+种子对账路径（skip/extract）记录；跨种子场景已实跑转绿=R9-A 修复盖章（R20，2026-10-16）；boot 上限 --upgrade-boot-timeout-s 默认 300s）"
    log "      network_recovery=断网恢复断言（精细测试队列④车道化，2026-10-15 脚本先行，2026-10-16 R22 实跑过=a/b/c 绿+d 红[失败轮不落库立案]；前置=message 同款：proot_boot 驻留+已配 key；断网主手段=iptables OUTPUT 白名单 DROP（网络形态版本敏感 eth0/wlan0，判据在函数头注释），备=飞行模式+svc；生效判据=探针 --net-recovery-probe-ip 默认 223.5.5.5 三对照（断网前可达/断网后不可达/恢复后可达）；断言面=断网态对话如实降级不假死+health 200×2+恢复后自愈真回复 degraded=false+断网轮不幽灵补发；恢复动作幂等+trap EXIT 兜底防设备留断网态；chat 上限 --net-recovery-chat-timeout-s 默认 120s）"
    log "      model_switch=多会话模型切换断言（精细测试队列⑥车道化，2026-10-16 脚本先行，首实跑 FAIL=R22 车道两缺陷立案[M1=PUT 复用初快照 etag 409×2/M2=旧会话 pid-差集法撞 pipeline 随 thread 稳定不可判]，同日两缺陷修复[每次 PUT 前 fresh GET etag+旧会话断言换 messages slot 计数增量定位新轮+同 pid 行 llm_model 翻新值，slot 计数不可靠窗口兜底=内核日志 Calling LLM: model= 行]；实跑现状=修复待下轮 MuMu 实跑复验判达标；前置=message 同款：proot_boot 驻留+已配 key；切换通道=REST 配置面 GET/PUT /api/v1/plugins/llm_service/config/llm（ETag 乐观锁，写恒用户空间），defaults.chat+tiers 三档齐切 A→B（坑㊵ 契约；A 缺省=切前 defaults.chat 现值，B 缺省 minimax-m3.1-flash-preview，--model-a/--model-b 参数化，键名大小写须与 models 真实键一致——ae3525b13 教训）；断言面=新会话生效（/pipelines/state 会话账 llm_model=B 的 model_name）+设备 user llm.yaml 文件面 tiers 齐切+旧会话跟随新默认+失效模型探针不假死（--model-bad 默认 nonexistent-model；降级语义探针记录不硬写，现状依据在函数头注释）+切回 A 恢复；快照测后还原（REST+设备文件双对照）+还原不完整时 trap EXIT 兜底重试；chat 上限 --model-switch-chat-timeout-s 默认 300s）"
    log "      screen_keepalive=息屏保活断言（精细测试队列⑦车道化，2026-10-16 脚本先行+同日 MuMu 实跑判据校准[KEYCODE_SLEEP(223) 主手段=POWER 被 MuMu 吞/stay_on_while_plugged_in=2147483647 恒插电实锤，睡判据落点 mWakefulness=Dozing，醒屏 224+82 实测有效]；机械面车道=息屏不停服/息屏任务推进/亮屏恢复+FGS 现状取证[只记录不断言，通知面现状=AR 轮改过的语义]，真机 Doze/电流定谳挂账须真机轮不属本车道；前置=message 同款：proot_boot 驻留+已配 key；息屏=input keyevent KEYCODE_SLEEP(223) 未生效再 POWER(26) 兜底，生效判据=dumpsys power mWakefulness=Asleep|Dozing 任一[本 ROM 无 Display Power: state= 行]；醒屏=KEYCODE_WAKEUP(224)+menu(82)（兜底 POWER），trap EXIT 醒屏兜底防设备留息屏态；息屏窗口 --keepalive-screen-off-s 默认 75s，health 轮询 --keepalive-health-polls×--keepalive-health-interval-s 默认 4×15s，亮屏后任务终态轮询 --keepalive-task-timeout-s 默认 300s，恢复对话 --keepalive-chat-timeout-s 默认 180s[允许一次重试=首调缝]）"
    exit 0
fi

if [[ "$MODE" == "all" ]]; then
    STEPS_SELECTED=("${ALL_STEPS[@]}")
fi

[[ ${#STEPS_SELECTED[@]} -gt 0 ]] || die "未选步骤（--step <name> 或 --all；--list 看步骤链）"

for step_name in "${STEPS_SELECTED[@]}"; do
    case "$step_name" in
        boot) step_boot ;;
        install) step_install ;;
        seed_kernel) step_seed_kernel ;;
        health) step_health ;;
        login) step_login ;;
        proot_boot) step_proot_boot ;;
        message) step_message ;;
        restart_recovery) step_restart_recovery ;;
        cancel) step_cancel ;;
        task_run) step_task_run ;;
        tool_loop) step_tool_loop ;;
        idle_firstcall) step_idle_firstcall ;;
        approval) step_approval ;;
        upgrade) step_upgrade ;;
        network_recovery) step_network_recovery ;;
        model_switch) step_model_switch ;;
        screen_keepalive) step_screen_keepalive ;;
        *) die "未知步骤: $step_name（--list 看步骤链）" ;;
    esac
done

log "汇总: PASS=$PASS_COUNT FAIL=$FAIL_COUNT"
[[ "$FAIL_COUNT" -eq 0 ]]
