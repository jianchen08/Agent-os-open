#!/usr/bin/env bash
# AgentOS 全客户端统一发布管线（2026-10-15 用户裁定，CI车道台账「队列升级」节）。
#
# 一条命令按序构建全部客户端并挂同一版本单源（仓根 VERSION，X.Y.Z，AR-M10 闸执法）：
#   ① desktop  NSIS 装机包（装机维护轮配方 §4 打包链：cargo 内核 → electron:build
#               全闸链 → electron-builder；干净 worktree@HEAD 只烘已提交码）
#   ② mobile   安卓双架构（deploy/build_mobile.sh 统一双架构模式，只调用不重写；
#               versionCode 由其 bump=当前最大+1，versionName 钉 VERSION）
#   ③ cloud    云部署 tar（装机维护轮配方 §7 现役配方：git archive HEAD 干净快照 +
#               frontend npm ci+build + tar 排除构建面；服务器侧 build/init 属部署
#               动作不在本管线，由运维轮消费 tar）
# 全绿后一次翻新全部交付位：release/<版本>/ + 统一 delivery_manifest.json +
# app-mobile/ 双 APK 交付位（--only commit）；任一段失败 = 交付位零改动（旧包全保留）。
#
# 版本单源口径（AR-M10 2026-10-15 落地，取代任务书两行制草案）：versionName/NSIS/云
# tar 名全派生自 VERSION，禁自 bump——bump = 改 VERSION + 五个引用面对齐一个 commit
# （对齐面清单见 scripts/check_version_sync.py）；发布计数 = 安卓 versionCode，由
# build_mobile.sh 双架构轮每发布 bump 一次（本管线 finalize 随包 commit 入账）。
#
# 用法：
#   deploy/release_all.sh                 # 全链发布（段级续跑：已完成段带产物即跳过）
#   deploy/release_all.sh --force         # 同版本重发布（覆盖 release/<版本>/ 后重交付）
#   deploy/release_all.sh --keep-worktree # 保留发布 worktree 与 staging（排障用）
#   deploy/release_all.sh --help
#
# 环境变量：
#   RELEASE_WT_ROOT          发布 worktree 根（缺省 <仓>/../container_e17cc5927dfd_release_wt；
#                            必须在仓外——受管工作区还原会清仓内未跟踪面，mid-run 丢失即重跑）
#   RELEASE_FAULT_INJECT     测试钩子：prep|desktop|mobile|cloud|finalize 任一值 =
#                            在该段起点报错退出（finalize=断言全过后、任何交付写面前），
#                            用于实测「断一端=零翻新」原子语义；发布时必须不设。
#
# 互斥（防双跑，与装机维护轮共存边界）：本脚本全程持有 release/.release-all.lock
# （mkdir 原子锁+pid 存活探测，stale 自动回收）；维护轮打包段动手前发现该锁在即
# repack-deferred 顺延（装机维护轮配方 §4 让位条款），不抢锁不等待。
#
# 退出码：0 成功；2 参数/环境错（锁占用/工具链缺/版本闸红/同版本已发布）；
#         3 构建段或断言失败（交付位零改动）。
# 产物布局：release/<版本>/{lingxi-agentos-x86_64-mumu.apk,
# lingxi-agentos-arm64-phone.apk, lingxi-agentos-setup-<版本>.exe,
# agentos-cloud-<版本>.tar.gz, delivery_manifest.json}（release/ 已 gitignore）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Windows 侧 Git Bash：Python 是原生 Windows 程序，POSIX 路径（/d/...）打不开
# ——MSYS 自动转换在 MSYS2_ARG_CONV_EXCL='*' 段被关掉，故显式转 Windows 风格。
# cygpath 不存在的 POSIX 环境原样透传（Linux CI 路径不变）。
win_path() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -w "$1"
    else
        printf '%s' "$1"
    fi
}
WT_ROOT="${RELEASE_WT_ROOT:-$(dirname "$REPO_ROOT")/container_e17cc5927dfd_release_wt}"
STAGE_ROOT="${RELEASE_STAGE_ROOT:-$(dirname "$WT_ROOT")/$(basename "$WT_ROOT")_stage}"
# staging 在 worktree 外（同仓外理由：受管还原免疫 + 不入 porcelain 检查面 + 清场不拖累 worktree remove）
LOCK_DIR="$REPO_ROOT/release/.release-all.lock"
FORCE=0
KEEP_WT="${RELEASE_KEEP_WT:-0}"
FAULT="${RELEASE_FAULT_INJECT:-}"
VERSION=""      # VERSION 单源值（X.Y.Z）
PRED_CODE=""    # 预期 versionCode = worktree build.gradle 现值+1（build_mobile.sh bump 规则）
BUILD_HEAD=""   # 构建 HEAD（prep 段钉死，全程不漂）
AAPT_BIN=""

usage() { sed -n '2,47p' "$0" | sed 's/^# \{0,1\}//'; }

log() { echo "[release-all] $*" >&2; }
die() { echo "[release-all][ERROR] $*" >&2; exit 2; }
fail() { echo "[release-all][FAIL] $*" >&2; exit 3; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force) FORCE=1; shift ;;
        --keep-worktree) KEEP_WT=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1（--help 看用法）" ;;
    esac
done

release_lock() { rm -rf "$LOCK_DIR"; }
cleanup() {
    release_lock
    if [[ "${RELEASE_ALL_DONE:-0}" == "1" && "$KEEP_WT" != "1" ]]; then
        log "清场：移除 staging 与发布 worktree（--keep-worktree / RELEASE_KEEP_WT=1 可保留）"
        rm -rf "$STAGE_ROOT"
        local j d
        for j in node_modules frontend/node_modules electron/node_modules app-mobile/node_modules; do
            d="$WT_ROOT/$j"
            if [[ -d "$d" ]]; then
                # 先摘 junction 再删树——rm/git 删除对 junction 一律穿通（§4「先 cmd rmdir
                # 再删目录」同款纪律，跳过即删穿主仓依赖面）
                MSYS2_ARG_CONV_EXCL='*' cmd //c "rmdir \"$(cygpath -w "$d")\"" >/dev/null 2>&1 || true
            fi
        done
        git -C "$REPO_ROOT" worktree remove --force "$WT_ROOT" 2>/dev/null || rm -rf "$WT_ROOT"
        git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ── 工具与输入就位（fail-closed）──────────────────────────────────────────

resolve_python() {
    local candidate
    for candidate in python3 python "py -3"; do
        if $candidate -c "import sys, json" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    die "无可用 Python（python3/python/py -3 均失败）"
}
PYTHON_BIN="$(resolve_python)"

resolve_aapt() { # 与 build_mobile.sh 同源逻辑（tools build-tools 版本序最高档）
    if [[ -n "${BUILD_MOBILE_AAPT:-}" ]]; then printf '%s' "$BUILD_MOBILE_AAPT"; return 0; fi
    local bt_root="$REPO_ROOT/.zctmp/tools/android-sdk/build-tools" ver name
    [[ -d "$bt_root" ]] || return 1
    ver="$(ls -1 "$bt_root" 2>/dev/null | sort -V | tail -1)"
    [[ -n "$ver" ]] || return 1
    for name in aapt.exe aapt; do
        [[ -f "$bt_root/$ver/$name" ]] && { printf '%s' "$bt_root/$ver/$name"; return 0; }
    done
    return 1
}

require_cmds() {
    local c
    for c in git node npm cargo wsl tar cp mv; do
        command -v "$c" >/dev/null 2>&1 || die "工具缺失: $c 不在 PATH"
    done
    command -v cargo-ndk >/dev/null 2>&1 || die "cargo-ndk 未安装（移动段 bionic 内核必需）"
    [[ -d "$REPO_ROOT/.zctmp/tools/android-sdk/ndk/27.3.13750724" ]] || die "NDK 缺失: .zctmp/tools/android-sdk/ndk（工具链就绪态见 CI车道台账 G-M0 行）"
    [[ -d "$REPO_ROOT/.zctmp/tools/jdk21/jdk" ]] || die "便携 jdk21 缺失: .zctmp/tools/jdk21/jdk"
}

# ── ⓪ 锁 + 版本单源门（preflight）────────────────────────────────────────

mkdir -p "$(dirname "$LOCK_DIR")"
mkdir "$LOCK_DIR" 2>/dev/null || {
    local_held_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null || echo '?')"
    die "发布锁被占: $LOCK_DIR（holder pid=$local_held_pid；确认无发布在跑后手工 rm -rf 该目录）"
}
echo "$$" > "$LOCK_DIR/pid"
log "发布锁就位（pid=$$）"

[[ -z "$FAULT" || "$FAULT" =~ ^(prep|desktop|mobile|cloud|finalize)$ ]] || die "RELEASE_FAULT_INJECT 只认 prep|desktop|mobile|cloud|finalize: $FAULT"
require_cmds

# ── CI 门禁（用户裁定 2026-10-28：打包/部署通过必须是 CI 通过了的）────────
# 三端产物同烘一个 HEAD（desktop NSIS + cloud tar 均源自 BUILD_HEAD），链首
# 一次取证 ci.yml + env-gate.yml 双 workflow 全绿；未绿/在跑/未跑过一律拦截
# （fail-closed，取证失败亦非零退出）。判定口径见脚本 docstring。
log "ci-gate: 本地取证 $(git -C "$REPO_ROOT" rev-parse --short HEAD) 的改动相关车道（CI 跟随执行空间：分档恒不全量 + 缓存差集只跑没跑过的）…"
"$PYTHON_BIN" "$(win_path "$REPO_ROOT/scripts/check_ci_gate.py")" --kind release --local \
    --cwd "$(win_path "$REPO_ROOT")" \
    || die "CI 门禁未绿——改动相关车道清红后再发布（本地取证，判定细节见上方输出）"

"$PYTHON_BIN" "$(win_path "$REPO_ROOT/scripts/check_version_sync.py")" >/dev/null || die "版本闸红（六面漂移）——先对齐 VERSION 单源再发布"
VERSION="$(head -1 "$REPO_ROOT/VERSION" | tr -d '[:space:]')"
[[ -d "$REPO_ROOT/release/$VERSION" && "$FORCE" != "1" ]] && die "release/$VERSION 已存在（同版本已发布；重发布用 --force）"

BUILD_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)"
log "版本单源: VERSION=$VERSION build_head=$(git -C "$REPO_ROOT" rev-parse --short "$BUILD_HEAD")"

if [[ -n "$FAULT" && "$FAULT" == "prep" ]]; then
    log "[fault-inject] 按RELEASE_FAULT_INJECT=prep 在 prep 段起点失败（原子语义实测钩子）"
    fail "fault-inject: prep"
fi

# ── ① prep：干净 worktree@HEAD + 依赖面（只烘已提交码，WIP 免疫）──────────

wt_valid() { [[ -f "$WT_ROOT/.git" || -d "$WT_ROOT/.git" ]]; }
if wt_valid; then
    log "复用发布 worktree: $WT_ROOT（checkout --detach $BUILD_HEAD）"
    git -C "$WT_ROOT" checkout --detach --force "$BUILD_HEAD" >/dev/null 2>&1 \
        || die "worktree checkout 失败: $WT_ROOT"
    git -C "$WT_ROOT" clean -fd app-mobile/www >/dev/null 2>&1 || true
else
    [[ -e "$WT_ROOT" ]] && die "$WT_ROOT 已存在但不是 worktree（手工清理或设 RELEASE_WT_ROOT）"
    log "创建发布 worktree@HEAD: $WT_ROOT"
    git -C "$REPO_ROOT" worktree add --detach "$WT_ROOT" "$BUILD_HEAD" >/dev/null \
        || die "git worktree add 失败"
fi
mkdir -p "$STAGE_ROOT/logs" "$STAGE_ROOT/desktop" "$STAGE_ROOT/mobile" "$STAGE_ROOT/cloud"

make_junction() { # $1=worktree 内路径 $2=主仓源路径（目录）；junction 零拷贝共享
    # PowerShell 建 junction（pack33 轮实证）：cmd mklink 在 MSYS 下 //c 与
    # MSYS2_ARG_CONV_EXCL 组合会静默退 0 不执行（node_modules 一直靠实目录回退
    # 兜着没暴露），PS New-Item 引号语义干净且可从 LinkType 复核。
    local link="$(cygpath -w "$1")" target="$(cygpath -w "$2")"
    powershell -NoProfile -Command "New-Item -ItemType Junction -Path '$link' -Target '$target' -ErrorAction Stop" >/dev/null 2>&1
}

prepare_node_modules() { # $1 = 仓内相对目录（''=根）；$2 = "install"|"junction-only"
    local rel="$1" mode="${2:-install}"
    local main_dir="$REPO_ROOT${rel:+/$rel}/node_modules" wt_dir="$WT_ROOT${rel:+/$rel}/node_modules"
    if [[ -d "$wt_dir" ]]; then
        log "prep: $rel node_modules 已在位（junction/实目录复用）"
        return 0
    fi
    if [[ -d "$main_dir" ]]; then
        if make_junction "$wt_dir" "$main_dir"; then
            log "prep: $rel node_modules junction → 主仓（同 lock 零拷贝，坑㊸ // 转义豁免在函数内）"
            [[ "$mode" == "junction-only" ]] && return 0
        else
            mkdir -p "$wt_dir"
            log "prep: $rel junction 失败，改实目录"
        fi
    else
        mkdir -p "$wt_dir"
        log "prep: 主仓 $rel node_modules 缺失，worktree 侧 npm install 自愈（坑㊿配方）"
    fi
    # npm install 始终跑：rebuild 只重挂 .bin，修不了被工作区还原削掉的包体（坑㊿教训：
    # rolldown 缺失症状在包不在 bin）；健康树上 --prefer-offline 为秒级 no-op。
    (cd "$WT_ROOT${rel:+/$rel}" && npm install --prefer-offline --no-audit --no-fund --loglevel=error >/dev/null) \
        || die "prep: $rel 依赖面修复失败（npm install）"
}

if [[ -f "$STAGE_ROOT/.done-prep" ]]; then
    recorded_head="$(cat "$STAGE_ROOT/.done-prep")"
    if [[ "$recorded_head" != "$BUILD_HEAD" ]]; then
        die "续跑守卫：staging 属旧 HEAD（${recorded_head:0:12}）而当前 HEAD=${BUILD_HEAD:0:12} 已前移——
旧段产物与新段混烘=审计面损坏；删除 $STAGE_ROOT 后重跑（旧产物作废，交付位本就零改动）"
    fi
    log "prep: 段标记在且 HEAD 一致，跳过（续跑口径）"
else
    [[ ! -f "$WT_ROOT/app-mobile/android/local.properties" ]] && \
        printf 'sdk.dir=%s\n' "$(cygpath -m "$REPO_ROOT/.zctmp/tools/android-sdk")" \
            > "$WT_ROOT/app-mobile/android/local.properties"
    prepare_node_modules ""
    prepare_node_modules "frontend"
    prepare_node_modules "electron" junction-only # .vite 缓存壳无 package.json，禁 npm install（§4 三 junction 教训）
    prepare_node_modules "app-mobile"
    # 离线 venv 供给负载同源（2026-10-26 离线发行裁定，ADR
    # 2026-10-26-offline-venv-payload）：python/wheels/uv.exe 是打包机本地产物
    # （gitignored，组装器 scripts/assemble_offline_payload.py 产出），发布
    # worktree 无静态下载源——junction 主仓 prereqs 零拷贝同源（check_prereqs
    # 硬拦与写透都落主仓，载荷真源仍主仓一份）。
    PREREQS_MAIN="$REPO_ROOT/electron/prereqs"
    if [[ -d "$WT_ROOT/electron/prereqs" && ! -L "$WT_ROOT/electron/prereqs" ]]; then
        # 旧轮残留实目录（check_prereqs 历史下载料）挡 junction——清除；junction
        # 形态（-L）绝不动，rm 穿透 junction 会删主仓负载（红线）
        rm -rf "$WT_ROOT/electron/prereqs"
    fi
    if [[ ! -d "$PREREQS_MAIN" ]]; then
        fail "prep: 主仓 electron/prereqs 缺失——先组装离线负载：python scripts/assemble_offline_payload.py"
    fi
    if [[ -d "$WT_ROOT/electron/prereqs" ]]; then
        log "prep: electron/prereqs junction 已在位，复用"
    else
        make_junction "$WT_ROOT/electron/prereqs" "$PREREQS_MAIN"             || fail "prep: electron/prereqs junction 失败（离线负载 staging）"
        log "prep: electron/prereqs junction → 主仓（离线负载同源）"
    fi
    # gradle daemon 预防性清退：daemon 的 VFS 会漏检 worktree 重检出后的 build.gradle
    # 变更（run2 实测：x86 轮烘出上一发布的 versionCode，aarch64 轮自愈）——发布前
    # 强制下一次构建全新配置
    (cd "$WT_ROOT/app-mobile/android" && JAVA_HOME="$REPO_ROOT/.zctmp/tools/jdk21/jdk" ./gradlew --stop >/dev/null 2>&1) || true
    echo -n "$BUILD_HEAD" > "$STAGE_ROOT/.done-prep"
    log "prep: OK（worktree@$(git -C "$WT_ROOT" rev-parse --short HEAD) + 依赖面 + local.properties + daemon 清退，staging=$STAGE_ROOT）"
fi

# 预期 versionCode：worktree（=HEAD）build.gradle 现值 + 1（build_mobile.sh bump 规则，
# 方案②版本单源双架构轮口径）。 finalize 以 aapt 实测值复核。
PRED_CODE="$("$PYTHON_BIN" - "$(win_path "$WT_ROOT/app-mobile/android/app/build.gradle")" <<'PYEOF'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
codes = re.findall(r"versionCode\s+(\d+)", text)
assert len(codes) == 1, f"versionCode 声明面漂移: {len(codes)} 处"
print(int(codes[0]) + 1)
PYEOF
)" || die "worktree build.gradle versionCode 解析失败"
log "发布计数: versionCode 预期 $PRED_CODE（双架构轮 bump 后）"

# ── 段标记工具（段级续跑：标记在且关键产物在=跳过）────────────────────────

segment_done() { [[ -f "$STAGE_ROOT/.done-$1" ]]; }
mark_done() { date +%s > "$STAGE_ROOT/.done-$1"; }

maybe_fault() { # $1 = 段名
    if [[ -n "$FAULT" && "$FAULT" == "$1" ]]; then
        log "[fault-inject] 按 RELEASE_FAULT_INJECT=$1 在该段起点失败（原子语义实测钩子）"
        fail "fault-inject: $1"
    fi
}

# ── ② desktop：NSIS 装机包（维护轮配方 §4 打包链，worktree 内全闸链）──────

seg_desktop() {
    maybe_fault desktop
    if segment_done desktop && ls "$STAGE_ROOT"/desktop/lingxi-agentos-setup-*.exe >/dev/null 2>&1; then
        log "desktop: 段标记在，跳过"
        return 0
    fi
    # 插件种子新鲜度闸（ADR 2026-11-07-plugin-tiered-single-copy-model 刀 4，
    # 与 ci-gate 同为 fail-closed 取证语义）：包内种子（worktree plugins/shared，
    # electron-builder extraResources 的照搬源）↔ 仓内 BUILD_HEAD 逐插件版本
    # 对账，不等/缺失即拦。置于 desktop 段链首（worktree@BUILD_HEAD 就位后、
    # cargo/NSIS 烘烤前——preflight 段 worktree 尚不存在，而主仓工作树按
    # prep 段「WIP 免疫」语义不作对账面）；--ref 钉 BUILD_HEAD，mid-run HEAD
    # 漂移（混烘）同样被此闸捕获。
    log "desktop: plugin-freshness：包内种子版本 ↔ 仓内 BUILD_HEAD 对账…"
    "$PYTHON_BIN" "$(win_path "$REPO_ROOT/scripts/check_package_plugin_freshness.py")" \
        --pack-root "$(win_path "$WT_ROOT")" \
        --repo "$(win_path "$REPO_ROOT")" \
        --ref "$BUILD_HEAD" \
        || fail "desktop: 插件种子新鲜度闸红（差异表见上）——worktree checkout --force \$BUILD_HEAD 后重跑，勿手补种子"
    log "desktop: cargo build --release --bin agentos-kernel（worktree 冷烘=只烘 HEAD 已提交码）"
    (cd "$WT_ROOT/kernel" && cargo build --release --bin agentos-kernel) \
        || fail "desktop: 内核构建失败"
    [[ -f "$WT_ROOT/kernel/target/release/agentos-kernel.exe" ]] || fail "desktop: 内核 exe 缺失"
    log "desktop: npm run electron:build（cdylibs 链首自编+sanitize/freshness/clean-tree/layout 全闸+NSIS）"
    (cd "$WT_ROOT" && \
        AGENTOS_BUILD_HEAD="$BUILD_HEAD" \
        ELECTRON_MIRROR="https://npmmirror.com/mirrors/electron/" \
        ELECTRON_BUILDER_BINARIES_MIRROR="https://npmmirror.com/mirrors/electron-builder-binaries/" \
        npm run electron:build) || fail "desktop: electron:build 链失败（闸红见上）"
    local installer
    installer="$(find "$WT_ROOT/release" -maxdepth 1 -name "*.exe" -type f | head -1)"
    [[ -n "$installer" ]] || fail "desktop: NSIS 安装器未产出（$WT_ROOT/release/*.exe）"
    cp -f "$installer" "$STAGE_ROOT/desktop/lingxi-agentos-setup-$VERSION.exe"
    mark_done desktop
    log "desktop: OK $STAGE_ROOT/desktop/lingxi-agentos-setup-$VERSION.exe"
}

# ── ③ mobile：安卓双架构（build_mobile.sh 统一模式，只调用不重写）──────────

build_wsl_cdylib() { # $1 = 插件名：worktree HEAD 源码 WSL 编 x86_64 cdylib（坑㊻：
    #                   干净树缺未跟踪 .so，主仓顶层件系陈旧遗留禁复用——R16 口径重编）
    local plugin="$1" so="$WT_ROOT/plugins/shared/$1/libpipeline_$1_native.so"
    local built="$WT_ROOT/plugins/shared/$1/target/release/libpipeline_$1_native.so"
    local wt_wsl
    wt_wsl="$(printf '%s' "$WT_ROOT" | sed -e 's|^/\([a-zA-Z]\)/|/mnt/\L\1/|')"
    log "mobile: WSL 编 x86_64 cdylib $plugin（HEAD 源码，离线缓存）"
    MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- bash -c '
        set -euo pipefail
        export PATH="$HOME/.cargo/bin:$PATH"
        export CARGO_HOME="${R5_WSL_CARGO_HOME:-/mnt/c/Users/jc/.cargo}"
        export CARGO_NET_OFFLINE=true
        cd "'"$wt_wsl"'/plugins/shared/'"$plugin"'" && exec cargo build --release' \
        || fail "mobile: cdylib $plugin WSL 构建失败"
    [[ -f "$built" ]] || fail "mobile: cdylib 产物缺失（target/release）: $built"
    cp -f "$built" "$so" # cargo 产物在 target/，部署面/种子扫插件根（R13/R16 驱动方手工置入的管线化）
    log "mobile: cdylib $plugin OK（$(du -h "$so" | cut -f1)）"
}

seg_mobile() {
    maybe_fault mobile
    if segment_done mobile && [[ -f "$STAGE_ROOT/mobile/lingxi-agentos-x86_64-mumu.apk" \
            && -f "$STAGE_ROOT/mobile/lingxi-agentos-arm64-phone.apk" ]]; then
        log "mobile: 段标记在，跳过"
        return 0
    fi
    local plugin
    for plugin in tool_core spill_guard sensitive_checker; do
        build_wsl_cdylib "$plugin"
    done
    log "mobile: build_mobile.sh 统一双架构（版本单源+原子交付进 staging）"
    # 产物根缺省=私有（staging 下）——共享槽位是各方裸构建的公共写面（run3 实测：
    # 槽位 rootfs staging 被并发轮 rm -rf 重建即互相铲-overlay），管线交付只消费
    # staging+release/<版本>/，槽位非承重面；RELEASE_MOBILE_OUT 显式指回主仓槽位
    # 才写惯例面。输入缓存一次拷贝保温（下载面 ~250MB 不重下）。
    local mo_out="${RELEASE_MOBILE_OUT:-$STAGE_ROOT/mobile-out}"
    if [[ -z "${RELEASE_MOBILE_OUT:-}" && -d "$REPO_ROOT/release/mobile/cache" && ! -d "$mo_out/cache" ]]; then
        mkdir -p "$mo_out"
        export MSYS2_ARG_CONV_EXCL='*'
        robocopy "$(cygpath -w "$REPO_ROOT/release/mobile/cache")" "$(cygpath -w "$mo_out/cache")" \
            /E /NFL /NDL /NJH /R:2 /W:2 /IPG:3 >/dev/null || true
        log "mobile: 输入缓存拷贝 → 私有产物根（$(du -sh "$mo_out/cache" 2>/dev/null | cut -f1)）"
    fi
    BUILD_MOBILE_TOOLS="$REPO_ROOT/.zctmp/tools" \
    BUILD_MOBILE_OUT="$mo_out" \
    BUILD_MOBILE_DELIVERY_DIR="$STAGE_ROOT/mobile" \
        bash "$WT_ROOT/deploy/build_mobile.sh" || fail "mobile: 双架构构建失败（交付位零改动）"
    git -C "$WT_ROOT" checkout -- app-mobile/www >/dev/null 2>&1 || true
    git -C "$WT_ROOT" clean -fd app-mobile/www >/dev/null 2>&1 || true
    mark_done mobile
    log "mobile: OK（双 APK 入 staging + 双架构槽位/manifest 入主仓 release/mobile）"
}

# ── ④ cloud：云部署 tar（维护轮配方 §7 现役配方 ①②③）────────────────────

seg_cloud() {
    maybe_fault cloud
    if segment_done cloud && [[ -f "$STAGE_ROOT/cloud/agentos-cloud-$VERSION.tar.gz" ]]; then
        log "cloud: 段标记在，跳过"
        return 0
    fi
    local src="$STAGE_ROOT/cloud/src"
    rm -rf "$src"
    mkdir -p "$src"
    log "cloud: git archive HEAD 干净快照（半成品禁烘，与云装链同原则）"
    # Windows 路径形态：mingw/native 两系 git 都认（/d/ MSYS 形在 native git 下
    # 报 cannot change to——2026-10-27 cloud 段实证一次，路径形态去歧义）
    git -C "$(cygpath -w "$REPO_ROOT")" archive "$BUILD_HEAD" | tar -x -C "$src"
    [[ -f "$REPO_ROOT/frontend/package-lock.json" ]] || fail "cloud: frontend/package-lock.json 缺失"
    cp -f "$REPO_ROOT/frontend/package-lock.json" "$src/frontend/package-lock.json"
    log "cloud: frontend npm ci + build（快照内，npmmirror 缓存）"
    (cd "$src/frontend" && npm ci --prefer-offline --no-audit --no-fund --loglevel=error \
        && npm run build --loglevel=error) || fail "cloud: 前端构建失败"
    # 部署回读清单（ADR 2026-11-10-deploy-freshness-verification）：对快照实际
    # 部署面（plugins/shared、plugins/sdk、config、skills、frontend/dist、deploy）
    # 逐文件 sha256，随 deploy/ 一起 rsync 到服务器；服务器侧由
    # deploy/verify_deploy.sh / init.sh 回读比对「本次发版是否真的部署到位」。
    log "cloud: 生成部署清单 deploy/deploy-manifest.json（profile=linux）"
    "$PYTHON_BIN" "$(win_path "$REPO_ROOT/deploy/verify_deploy_manifest.py")" build \
        --root "$(win_path "$src")" \
        --out "$(win_path "$src/deploy/deploy-manifest.json")" \
        --profile linux --version "$VERSION" --build-head "$BUILD_HEAD" \
        || fail "cloud: 部署清单生成失败（清单缺失会让服务器侧回读静默降级）"
    [[ -f "$src/deploy/deploy-manifest.json" ]] || fail "cloud: 部署清单未产出"
    log "cloud: tar czf（排除 node_modules/kernel target/__pycache__/.venv/app-mobile）"
    tar -czf "$STAGE_ROOT/cloud/agentos-cloud-$VERSION.tar.gz" \
        --exclude='frontend/node_modules' --exclude='kernel/target' \
        --exclude='__pycache__' --exclude='.venv' --exclude='app-mobile' \
        -C "$src" .
    [[ -f "$STAGE_ROOT/cloud/agentos-cloud-$VERSION.tar.gz" ]] || fail "cloud: tar 未产出"
    mark_done cloud
    log "cloud: OK $STAGE_ROOT/cloud/agentos-cloud-$VERSION.tar.gz（$(du -h "$STAGE_ROOT/cloud/agentos-cloud-$VERSION.tar.gz" | cut -f1)）"
}

# ── ⑤ finalize：三面版本断言 → 产物集装 → 交付位一次翻新（原子语义）───────

apk_version_pair() { # $1 = APK → stdout "code:name"（aapt badging 首行）
    AAPT_BIN="$AAPT_BIN" APK_TARGET="$1" "$PYTHON_BIN" <<'PYEOF'
import os
import re
import subprocess

out = subprocess.run(
    [os.environ["AAPT_BIN"], "dump", "badging", os.environ["APK_TARGET"]],
    capture_output=True, text=True,
).stdout
lines = out.splitlines()
m = re.search(r"versionCode='(\d+)' versionName='([^']+)'", lines[0]) if lines else None
print(f"{m.group(1)}:{m.group(2)}" if m else "")
PYEOF
}

nsis_file_version() { # $1 = 安装器路径（MSYS 形）→ stdout FileVersion（如 0.2.1.0）
    NSIS_TARGET="$(cygpath -w "$1")" powershell -NoProfile -Command \
        '[System.Diagnostics.FileVersionInfo]::GetVersionInfo($env:NSIS_TARGET).FileVersion' \
        | tr -d '\r'
}

tar_version_entry() { # $1 = 云 tar → stdout 内嵌 VERSION 文件值（git archive 自带）
    TAR_TARGET="$1" "$PYTHON_BIN" <<'PYEOF'
import os
import sys
import tarfile

with tarfile.open(os.environ["TAR_TARGET"], "r:gz") as tf:
    for member in tf:
        name = member.name[2:] if member.name.startswith("./") else member.name
        if name == "VERSION":
            fo = tf.extractfile(member)
            print(fo.read().decode("utf-8").strip() if fo else "")
            break
PYEOF
}

tar_has_deploy_manifest() { # $1 = 云 tar → 0 = 内含 deploy/deploy-manifest.json
    TAR_TARGET="$1" "$PYTHON_BIN" <<'PYEOF'
import os
import sys
import tarfile

with tarfile.open(os.environ["TAR_TARGET"], "r:gz") as tf:
    for member in tf:
        name = member.name[2:] if member.name.startswith("./") else member.name
        if name == "deploy/deploy-manifest.json":
            sys.exit(0)
sys.exit(1)
PYEOF
}

seg_finalize() {
    maybe_fault finalize
    local seg
    for seg in prep desktop mobile cloud; do
        segment_done "$seg" || fail "finalize: 段 $seg 未完成（无 .done-$seg 标记）——交付位零改动"
    done
    local apk_x86="$STAGE_ROOT/mobile/lingxi-agentos-x86_64-mumu.apk" \
          apk_arm="$STAGE_ROOT/mobile/lingxi-agentos-arm64-phone.apk" \
          installer="$STAGE_ROOT/desktop/lingxi-agentos-setup-$VERSION.exe" \
          cloud_tar="$STAGE_ROOT/cloud/agentos-cloud-$VERSION.tar.gz"
    for f in "$apk_x86" "$apk_arm" "$installer" "$cloud_tar"; do
        [[ -f "$f" ]] || fail "finalize: 产物缺失: $f"
    done

    # 断言面（发布末全客户端版本严格一致：aapt/NSIS/tar 名三面 + 计数面）
    AAPT_BIN="$(resolve_aapt)" || fail "finalize: aapt 不可得"
    local got expect="$PRED_CODE:$VERSION"
    got="$(apk_version_pair "$apk_x86")"
    [[ "$got" == "$expect" ]] || fail "版本断言: x86_64 APK 实测[$got] 期望[$expect]"
    log "断言 OK: aapt x86_64 = $got"
    got="$(apk_version_pair "$apk_arm")"
    [[ "$got" == "$expect" ]] || fail "版本断言: aarch64 APK 实测[$got] 期望[$expect]"
    log "断言 OK: aapt aarch64 = $got"
    got="$(nsis_file_version "$installer")"
    got="${got%% *}" # 防系统类 FileVersion 尾缀（WinBuild 形）——electron-builder 产出为纯数字段，取首段防御
    # 前缀语义：FileVersion 三段（0.2.2）/四段（0.2.2.0）形态均合法，剥尾段
    # 写法对三段式会误剥成 0.2（2026-10-27 实测拦发）——改前缀匹配。
    [[ "$got" == "$VERSION" || "$got" == "$VERSION".* ]]         || fail "版本断言: NSIS FileVersion 实测[$got] 期望前缀[$VERSION]"
    log "断言 OK: NSIS FileVersion = $got"
    got="$(tar_version_entry "$cloud_tar")"
    [[ "$got" == "$VERSION" ]] || fail "版本断言: 云 tar 内嵌 VERSION 实测[$got] 期望[$VERSION]"
    log "断言 OK: 云 tar VERSION = $got"
    tar_has_deploy_manifest "$cloud_tar" || fail "finalize: 云 tar 缺 deploy/deploy-manifest.json（服务器侧回读校验无清单可用）"
    log "断言 OK: 云 tar 内嵌 deploy/deploy-manifest.json"

    # 集装 release/<版本>/（staging 原子改名进位）
    local final_dir="$STAGE_ROOT/final" target_dir="$REPO_ROOT/release/$VERSION"
    rm -rf "$final_dir"
    mkdir -p "$final_dir"
    cp -f "$apk_x86" "$apk_arm" "$installer" "$cloud_tar" "$final_dir/"
    # 产物路径一律 win_path：合成 key=path 参数 MSYS 不做路径转换，Windows
    # python 吃 /d/ 形会落成「当前盘根 \d\...」幻影路径必报产物缺失（2026-10-27
    # finalize 实证拦发）。
    "$PYTHON_BIN" "$(win_path "$REPO_ROOT/deploy/make_delivery_manifest.py")" \
        --out "$(win_path "$final_dir/delivery_manifest.json")" \
        --semver "$VERSION" --version-code "$PRED_CODE" --build-head "$BUILD_HEAD" \
        --artifact "apk-x86_64-mumu=$(win_path "$final_dir/lingxi-agentos-x86_64-mumu.apk")" \
        --artifact "apk-arm64-phone=$(win_path "$final_dir/lingxi-agentos-arm64-phone.apk")" \
        --artifact "desktop-setup=$(win_path "$final_dir/lingxi-agentos-setup-$VERSION.exe")" \
        --artifact "cloud-tar=$(win_path "$final_dir/agentos-cloud-$VERSION.tar.gz")" \
        || fail "finalize: 统一 delivery_manifest 写出失败"
    [[ -e "$target_dir" && "$FORCE" != "1" ]] && fail "finalize: $target_dir 已存在（--force 重发布）"
    rm -rf "$target_dir"
    mv "$final_dir" "$target_dir"
    log "交付: $target_dir（4 产物 + delivery_manifest.json）"

    # 安卓交付位翻新（staging→mv 一次改名；两包全成才动，旧包保留语义见双架构轮）
    local deliver_x86="$REPO_ROOT/app-mobile/lingxi-agentos-x86_64-mumu.apk" \
          deliver_arm="$REPO_ROOT/app-mobile/lingxi-agentos-arm64-phone.apk"
    cp -f "$apk_x86" "$deliver_x86.staging"
    cp -f "$apk_arm" "$deliver_arm.staging"
    cmp -s "$deliver_x86.staging" "$target_dir/lingxi-agentos-x86_64-mumu.apk" \
        || fail "交付位 digest 不自洽（x86_64）"
    cmp -s "$deliver_arm.staging" "$target_dir/lingxi-agentos-arm64-phone.apk" \
        || fail "交付位 digest 不自洽（aarch64）"
    mv -f "$deliver_x86.staging" "$deliver_x86"
    mv -f "$deliver_arm.staging" "$deliver_arm"
    log "交付: app-mobile/ 双 APK 交付位翻新（v$VERSION($PRED_CODE)）"

    # 计数与包入账（两 commit：bump 先于交付，沿 R16 惯例；--only 圈定不涉他人在飞）
    "$PYTHON_BIN" - "$(win_path "$REPO_ROOT/app-mobile/android/app/build.gradle")" "$PRED_CODE" <<'PYEOF'
import re
import sys

path, new_code = sys.argv[1], sys.argv[2]
text = open(path, encoding="utf-8").read()
new_text, n = re.subn(r"(versionCode\s+)\d+", rf"\g<1>{new_code}", text, count=1)
assert n == 1, f"versionCode 替换 {n} 处（须恰 1 处）"
open(path, "w", encoding="utf-8", newline="").write(new_text)
PYEOF
    git -C "$REPO_ROOT" commit --only -m \
        "build(mobile): versionCode → $PRED_CODE（统一发布管线 v$VERSION，发布计数随包入账）" \
        -- app-mobile/android/app/build.gradle >/dev/null \
        || log "WARN versionCode commit 未落（无变化或被并发提交吸收，继续交付 commit）"
    git -C "$REPO_ROOT" commit --only -m \
        "release: 交付位翻新 v$VERSION($PRED_CODE)（统一发布管线；digest 见 release/$VERSION/delivery_manifest.json）" \
        -- "$deliver_x86" "$deliver_arm" >/dev/null \
        || log "WARN 交付位产物不入 git（ADR 2026-10-16-build-artifacts-out-of-git，APK 为 gitignore 件此 commit 恒败属预期），入账以 manifest+台账为准"
    log "入账: versionCode bump + 交付位两 commit（--only）"

    RELEASE_ALL_DONE=1
    log "═══ 发布完成 v$VERSION($PRED_CODE) head=$(git -C "$REPO_ROOT" rev-parse --short HEAD) ═══"
}

# ── 装配线派发（desktop→mobile→cloud→finalize；desktop 先行=clean-tree 闸在
#    syncweb 改写 www 之前过闸；段级续跑由各段标记自治）─────────────────────
seg_desktop
seg_mobile
seg_cloud
seg_finalize
