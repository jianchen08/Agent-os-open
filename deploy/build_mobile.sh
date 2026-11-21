#!/usr/bin/env bash
# AgentOS 手机端构建配方（Phase 1 G-M0 车道，方案 §Phase1 1.0 车道先行 / §5.1 打包缝）。
#
# 一条命令出 kernel + APK + rootfs 三产物 + digest manifest（缺省=双架构一次全打
# 统一版本防漂移，2026-10-15 用户裁定；--arch 定单档，CI/调试用）；
# 单一配方源禁三套漂移；二进制不入仓（digest 锁，产物落 release/mobile/，已 gitignore）。
#
# 三构建步：
#   ① kernel  cargo-ndk 交叉编译 release（rustup android targets / NDK @ .zctmp/tools，
#             勿重复安装）；产物按 jniLibs 形态要求另行复制为 libagentos.so（apk 步）。
#             同刀编 seedx（首启种子解包器，R5 起）→ libseedx.so。
#   ② APK     复用现役 app-mobile 构建链：jniLibs(libagentos.so + libproot.so +
#             libseedx.so) ← kernel/proot 步产物；rootfs 种子（.tar.zst）拷入
#             android assets（noCompress，预压缩不再 deflate）→ syncweb（frontend
#             dist + shell-config 注入 + cap sync，增 LINGXI_API_BASE 单点注入实现
#             server.url 双模式 cloud|local）→ gradlew assembleDebug（JAVA_HOME 指
#             便携 jdk21 嵌套 jdk/ 子目录，现役配方）；--server-mode cloud|local 参数化。
#   ③ rootfs  Ubuntu base + 预 sync 全插件 site-packages 快照 + linux-gnu 内核
#             （WSL 编，R5 起：栈内 glibc cdylib 插件 tool_core/spill_guard/
#             sensitive_checker 在主对话管道链上，bionic 内核无法 in-process 加载
#             glibc .so——全栈进 proot 与 P0.2 实证口径同构）+ PBS cpython 基座
#             （条目级注入保真符号链接）+ 硬链接去重 + zstd 压缩出 .tar.zst：
#             uv sync --python-platform 钉死（坑⑳）；plugins/sdk 必进部署面（坑⑯）；
#             快照选取规则与 deploy/init.sh 同律（_host + 独占宿主插件，合宿成员
#             跳过）；部署面排除构建垃圾（target/ .mypy_cache .venv-* node_modules *.dll
#             .cache，.cache 另有 overlay 现清双保险——移动端存储优化 2026-11-19）；
#             config 副本脱敏 + 产物 key 断言（坑㊳配套：真 key 永不进 APK，
#             deploy/sanitize_seed_config.py 两面，违约 fail-closed 拦截）。
#
# 用法：
#   deploy/build_mobile.sh                                      # 统一双架构一次全打（缺省，无 --arch）：
#                                                               #   版本单源一次定型（versionCode=build.gradle
#                                                               #   当前最大+1 写回；versionName 钉 VERSION 面
#                                                               #   不动，AR-M10 口径）→ x86_64 与 aarch64 双档
#                                                               #   均本机内核口径（local:9300——2026-10-23 用户令
#                                                               #   「装上就能用」：连接=一条，装上即本机档）连续
#                                                               #   构建（共享同一版本）→
#                                                               #   aapt 双包版本断言严格一致 → 全绿才原子交付：
#                                                               #   app-mobile/ 双交付位 + 顶层 release/mobile/
#                                                               #   delivery_manifest.json（版本/构建 HEAD/双架构
#                                                               #   kernel+apk+rootfs digest 体积）；
#                                                               #   任一架构失败=交付位零改动（旧包保留）+报错。
#                                                               #   bump 不自动 commit：交付轮随包 --only 入账，
#                                                               #   构建失败可 git checkout -- 还原后重跑。
#                                                               #   worktree 出包：BUILD_MOBILE_DELIVERY_DIR 指
#                                                               #   主仓 app-mobile（缺省=本树 app-mobile）。
#   deploy/build_mobile.sh --arch aarch64                       # 单架构全量（CI/调试；不 bump 不交付）+ manifest + verify
#   deploy/build_mobile.sh --arch aarch64 --server-mode cloud   # 单架构出云档调试包（双架构缺省已全 local，cloud 属定制）
#   deploy/build_mobile.sh --arch x86_64 --step apk             # 只重打 APK（复用既有 kernel/rootfs
#                                                               #   产物；Java/壳面修复轮口径，防重跑
#                                                               #   rootfs 步把工作区在飞改动打进种子）
#   deploy/build_mobile.sh --arch x86_64 --step kernel          # 只编 bionic 内核+seedx（jniLibs 面）
#   deploy/build_mobile.sh --arch x86_64 --step kernel-linux    # 只 WSL 编 glibc 面（内核；
#                                                             #   aarch64 档交叉编内核+3 cdylib，
#                                                             #   rootfs 步自动换入种子）
#   deploy/build_mobile.sh --arch x86_64 --step rootfs          # 只重打种子（内核换刀口径；部署面取
#                                                               #   当前树，干净树要求由驱动方保证）
#   deploy/build_mobile.sh --arch x86_64 --manifest-only \
#       --kernel K --proot P --rootfs R                         # 仅对外部已有产物出 manifest+verify
#   deploy/build_mobile.sh --arch aarch64 --verify-only --manifest M
#
# 产物布局（$OUT/$ARCH/ 下）：kernel/agentos-kernel | kernel-linux/agentos-kernel
# （glibc，随种子）| kernel-linux/seedx | apk/app-debug.apk |
# rootfs/rootfs_seed.tar.zst | artifacts_manifest.json
# 输入缓存（$OUT/cache/，跨次构建复用不重下）：ubuntu-base-<debarch>.tar.gz /
# uv-<debarch>.tar.gz / proot-<debarch> / pbs-python-<debarch>.tar.gz（缺时优先
# 复用 .zctmp/p0/ 历史件，否则自动下载：ubuntu-base 走 TUNA，其余走 GitHub
# Release（版本钉死）+ ghfast 代理兜底=坑⑨配方）。
#
# 注意：构建会改写 app-mobile/www/（被跟踪面）——APK 落位后由构建驱动方
# `git checkout -- app-mobile/www && git clean -fd app-mobile/www` 还原工作区；
# 产物一律不 commit。
#
# 退出码：0 成功；2 参数错；3 构建步/交付失败（缺前置/构建红/双包版本不一致）；1 manifest 校验不过。

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
MANIFEST_PY="$REPO_ROOT/deploy/artifacts_manifest.py"
TOOLS_ROOT="${BUILD_MOBILE_TOOLS:-$REPO_ROOT/.zctmp/tools}"
NDK_HOME="${ANDROID_NDK_HOME:-$TOOLS_ROOT/android-sdk/ndk/27.3.13750724}"
UV_VERSION="${BUILD_MOBILE_UV_VERSION:-0.13.0}"        # P0.2 设备线实证版本，钉死保 digest 可复现
SNAPSHOT_PYTHON="${BUILD_MOBILE_PYTHON:-3.12}"         # 快照解释器钉值（P0.2 设备线 3.12.15 同线）
# 种子最终压缩（materialize 重写遍决定成品级别）：zstd 解压耗时与级别基本无关，
# 级别只影响构建耗时，故取 19（level 3→19 实测 415MB→325MB，种子内 17 个 venv
# site-packages 高度同构，长窗口收益落在这里）。
SEED_ZSTD_LEVEL="${BUILD_MOBILE_SEED_ZSTD_LEVEL:-19}"
SEED_ZSTD_THREADS="${BUILD_MOBILE_SEED_ZSTD_THREADS:-4}"
PBS_TAG="${BUILD_MOBILE_PBS_TAG:-20261013}"            # python-build-standalone 发布 tag
PBS_PY="${BUILD_MOBILE_PBS_PY:-3.12.15}"               # 与 SNAPSHOT_PYTHON 小版本对齐
PROOT_VERSION="${BUILD_MOBILE_PROOT_VERSION:-5.5.0}"   # proot-me 静态构建（x86_64）
PROOT_VERSION_AARCH64="${BUILD_MOBILE_PROOT_VERSION_AARCH64:-5.3.0}" # aarch64 静态件最新止于 5.3.0（版本 skew 如实记录）

ARCH=""
OUT_ROOT="${BUILD_MOBILE_OUT:-$REPO_ROOT/release/mobile}"
VERSION="" # manifest 版本标签：full 模式缺省=解析值 v<name>(<code>)，--version 显式覆盖；其余模式缺省 dev-local
VERSION_EXPLICIT=0
MODE="full" # full | manifest-only | verify-only | step
STEP_ONLY="" # MODE=step 时：只跑指定单步（现役 apk——Java/壳面修复重打包，复用既有 kernel/proot/rootfs 产物）
SERVER_MODE="local" # 缺省本机内核口径（2026-10-23 用户令「装上就能用」；cloud 需 --server-mode 显式定制）
MOBILE_KERNEL_PORT="9300"
SERVER_MODE_EXPLICIT=0 # 双架构模式口径=全 local（2026-10-23 用户令装上即用），显式传参即拒（单架构 CI/调试不受限）
PORT_EXPLICIT=0
ARG_KERNEL=""
ARG_PROOT=""
ARG_ROOTFS=""
ARG_MANIFEST=""
# 统一双架构轮的定型版本（bump_version_code/resolve_version 填；两轮共用+防御断言基准）
RESOLVED_CODE=""
RESOLVED_NAME=""
AAPT_BIN="" # full 模式时 resolve_aapt 就位（aapt 双包版本断言，方案④）
DUAL_DELIVERY_DIR="${BUILD_MOBILE_DELIVERY_DIR:-$REPO_ROOT/app-mobile}" # 双架构交付位根（worktree 出包指主仓 app-mobile）

usage() {
    sed -n '2,57p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
    echo "[build-mobile][ERROR] $*" >&2
    exit 2
}

log() {
    # 日志走 stderr：resolve_* 系函数经命令替换捕获 stdout 取路径，stdout 必须纯净
    echo "[build-mobile] $*" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --arch) ARCH="${2:?--arch 需值}"; shift 2 ;;
        --out) OUT_ROOT="${2:?--out 需值}"; shift 2 ;;
        --version) VERSION="${2:?--version 需值}"; VERSION_EXPLICIT=1; shift 2 ;;
        --server-mode) SERVER_MODE="${2:?--server-mode 需值}"; SERVER_MODE_EXPLICIT=1; shift 2 ;;
        --port) MOBILE_KERNEL_PORT="${2:?--port 需值}"; PORT_EXPLICIT=1; shift 2 ;;
        --manifest-only) MODE="manifest-only"; shift ;;
        --verify-only) MODE="verify-only"; shift ;;
        --step) MODE="step"; STEP_ONLY="${2:?--step 需值（现役 apk）}"; shift 2 ;;
        --kernel) ARG_KERNEL="${2:?--kernel 需路径}"; shift 2 ;;
        --proot) ARG_PROOT="${2:?--proot 需路径}"; shift 2 ;;
        --rootfs) ARG_ROOTFS="${2:?--rootfs 需路径}"; shift 2 ;;
        --manifest) ARG_MANIFEST="${2:?--manifest 需路径}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数: $1（--help 看用法）" ;;
    esac
done

# aarch64/x86_64 换元：真机口径 aarch64；模拟器冒烟=x86_64（P0 双路径，§Phase 0）。
# 三元组→jniLibs ABI→deb 架构→uv 平台钉值，一档换元全表在此（禁散落复制）。
set_arch_vars() { # $1 = arch：换元表 + 本档产物路径定型（双架构轮每轮调用重定型）
    ARCH="$1"
    case "$ARCH" in
        aarch64)
            TARGET_TRIPLE="aarch64-linux-android"; ABI="arm64-v8a"
            DEBARCH="arm64"; UV_PLATFORM="aarch64-unknown-linux-gnu"
            ELF_MACHINE="183" ;; # ELF e_machine：183=aarch64（错架构 glibc 面入种子=真机必坏，build 期断言）
        x86_64)
            TARGET_TRIPLE="x86_64-linux-android"; ABI="x86_64"
            DEBARCH="amd64"; UV_PLATFORM="x86_64-unknown-linux-gnu"
            ELF_MACHINE="62" ;; # 62=x86-64
        *) die "--arch 只认 aarch64|x86_64: $ARCH" ;;
    esac
    OUT_DIR="$OUT_ROOT/$ARCH"
    KERNEL_BIN="$OUT_DIR/kernel/agentos-kernel"
    KERNEL_LINUX_BIN="$OUT_DIR/kernel-linux/agentos-kernel"
    SEEDX_BIN="$OUT_DIR/kernel/seedx"
    APK_FILE="$OUT_DIR/apk/app-debug.apk"
    ROOTFS_SEED="$OUT_DIR/rootfs/rootfs_seed.tar.zst"
    MANIFEST="${ARG_MANIFEST:-$OUT_DIR/artifacts_manifest.json}"
}
case "$SERVER_MODE" in
    cloud|local) ;;
    "") die "--server-mode 只认 cloud|local" ;;
    *) die "--server-mode 只认 cloud|local: $SERVER_MODE" ;;
esac

# 模式×arch 依赖：full 无 --arch=统一双架构一次全打（缺省）；其余模式必须单档。
if [[ "$MODE" != "full" && -z "$ARCH" ]]; then
    die "--arch 必填（aarch64|x86_64 换元；full 模式缺省即双架构统一打包）"
fi
if [[ -n "$ARCH" ]]; then
    set_arch_vars "$ARCH"
fi
[[ -n "$VERSION" ]] || VERSION="dev-local" # full 模式在版本定型处覆盖为解析值标签

# Python 探测：候选逐一试跑（Windows 下 python3 可能命中 Microsoft Store 存根——
# 存在但执行即败，退出 49，必须真跑一次才算数）；PYTHON_BIN 环境变量显式覆盖。
resolve_python() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        echo "$PYTHON_BIN"
        return 0
    fi
    local candidate
    for candidate in python3 python "py -3"; do
        if $candidate -c "import sys, json" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done
    die "无可用 Python（python3/python/py -3 均失败）；用 PYTHON_BIN 显式指定"
}
PYTHON_BIN="$(resolve_python)"

# ── ⓪ 版本单源（方案②）+ aapt 版本断言（方案④）+ 原子交付（方案③）──────
#
# 版本面口径（AR-M10 1cc385d55 起）：versionName 钉根 VERSION（build.gradle 与
# VERSION 一致性由 scripts/check_version_sync.py 闸执法），versionCode=Android
# 单调构建号归打包轮自管——本脚本双架构轮取「当前最大+1」一次定型写回，
# versionName 不动；bump 不自动 commit（交付轮随包 --only 入账）。

parse_gradle_version() { # stdout: "<code> <name>"（与 syncweb.mjs/check_version_sync.py 同一声明面）
    "$PYTHON_BIN" - "$(win_path "$REPO_ROOT/app-mobile/android/app/build.gradle")" <<'PYEOF'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
codes = re.findall(r"versionCode\s+(\d+)", text)
names = re.findall(r'versionName\s+"([^"]+)"', text)
if len(codes) != 1 or len(names) != 1:
    print(f"[build-mobile][ERROR] build.gradle 声明面漂移：versionCode={len(codes)}处 "
          f"versionName={len(names)}处（须各恰一处，与 syncweb/版本闸同口径）", file=sys.stderr)
    sys.exit(2)
print(codes[0], names[0])
PYEOF
}

resolve_version() { # 单架构 full：以树内现值定型（不 bump 不写回）
    local pair
    pair="$(parse_gradle_version)" || die "build.gradle 版本解析失败（见上）"
    RESOLVED_CODE="${pair%% *}"
    RESOLVED_NAME="${pair#* }"
    log "版本单源：现值 versionCode=$RESOLVED_CODE versionName=$RESOLVED_NAME（单架构不 bump）"
}

bump_version_code() { # 双架构交付轮：versionCode=当前最大+1 一次写回（versionName 钉 VERSION 面不动）
    local pair new_code
    pair="$(parse_gradle_version)" || die "build.gradle 版本解析失败（见上）"
    new_code="$(( ${pair%% *} + 1 ))"
    if ! BUMP_GRADLE="$(win_path "$REPO_ROOT/app-mobile/android/app/build.gradle")" \
            BUMP_NEW_CODE="$new_code" "$PYTHON_BIN" <<'PYEOF'
import os
import re
import sys

path = os.environ["BUMP_GRADLE"]
text = open(path, encoding="utf-8").read()
new_text, n = re.subn(r"(versionCode\s+)\d+", rf"\g<1>{os.environ['BUMP_NEW_CODE']}", text, count=1)
if n != 1:
    print(f"[build-mobile][ERROR] versionCode 替换 {n} 处（须恰 1 处），拒绝写回", file=sys.stderr)
    sys.exit(2)
open(path, "w", encoding="utf-8", newline="").write(new_text)
PYEOF
    then
        die "versionCode bump 写回失败（见上）——交付位零改动"
    fi
    RESOLVED_CODE="$new_code"
    RESOLVED_NAME="${pair#* }"
    log "版本单源：versionCode bump → $new_code，versionName=$RESOLVED_NAME（双架构共用；未 commit，交付轮随包 --only 入账）"
}

assert_version_unchanged() { # 两轮解析值一致（防御）：build.gradle 被外力改写（工作区还原/并发轮）即拦截
    local pair
    pair="$(parse_gradle_version)" || {
        log "FAIL 版本复解失败（见上）——交付位零改动"
        exit 3
    }
    if [[ "$pair" != "$RESOLVED_CODE $RESOLVED_NAME" ]]; then
        log "FAIL 版本漂移拦截：build.gradle 现值[$pair] ≠ 本轮定型[$RESOLVED_CODE $RESOLVED_NAME]——交付位零改动，排查外力改写后重跑"
        exit 3
    fi
}

resolve_aapt() { # build-tools 版本序取最高档 aapt（aapt.exe/aapt 兼容）；BUILD_MOBILE_AAPT 显式覆盖
    if [[ -n "${BUILD_MOBILE_AAPT:-}" ]]; then
        printf '%s' "$BUILD_MOBILE_AAPT"
        return 0
    fi
    local bt_root="$TOOLS_ROOT/android-sdk/build-tools" ver name
    [[ -d "$bt_root" ]] || return 1
    ver="$(ls -1 "$bt_root" 2>/dev/null | sort -V | tail -1)"
    [[ -n "$ver" ]] || return 1
    for name in aapt.exe aapt; do
        [[ -f "$bt_root/$ver/$name" ]] && { printf '%s' "$bt_root/$ver/$name"; return 0; }
    done
    return 1
}

apk_version_pair() { # $1 = APK → stdout "code:name"（aapt badging 首行；空=解析失败）
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

assert_apk_versions() { # 方案④：逐包 aapt 版本=本轮定型值（双包同断即严格一致）；不匹配返回非零
    local apk got expect="$RESOLVED_CODE:$RESOLVED_NAME"
    for apk in "$@"; do
        got="$(apk_version_pair "$apk")"
        if [[ "$got" != "$expect" ]]; then
            log "FAIL APK 版本断言: $apk 实测[${got:-空}] 期望[$expect]（双包版本严格一致口径）"
            return 1
        fi
        log "aapt 版本断言 OK: $(basename "$apk") = $got"
    done
}

write_delivery_manifest() { # $1=x86_64 交付 APK  $2=aarch64 交付 APK → $OUT_ROOT/delivery_manifest.json
    DELIVERY_OUT="$OUT_ROOT/delivery_manifest.json" \
    DELIVERY_DIR="$OUT_ROOT" \
    DELIVERY_HEAD="$(git -C "$REPO_ROOT" rev-parse HEAD)" \
    DELIVERY_CODE="$RESOLVED_CODE" DELIVERY_NAME="$RESOLVED_NAME" \
    APK_X86="$1" APK_ARM="$2" \
    SLOT_X86="$OUT_ROOT/x86_64" SLOT_ARM="$OUT_ROOT/aarch64" \
    "$PYTHON_BIN" <<'PYEOF'
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

def entry(path: Path, base: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    try:
        rel = path.relative_to(base).as_posix()
    except ValueError:
        rel = str(path)
    return {"path": rel, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}

delivery_dir = Path(os.environ["DELIVERY_DIR"])
arches = {}
for arch, apk_env, slot_env, mode in (
    ("x86_64", "APK_X86", "SLOT_X86", "local"),
    ("aarch64", "APK_ARM", "SLOT_ARM", "local"),
):
    slot = Path(os.environ[slot_env])
    arches[arch] = {
        "server_mode": mode,  # 双档均本机内核口径（2026-10-23 用户令「装上就能用」，接替 R10/R14 交付惯例）
        "apk": entry(Path(os.environ[apk_env]), delivery_dir),
        "kernel": entry(slot / "kernel" / "agentos-kernel", delivery_dir),
        "rootfs": entry(slot / "rootfs" / "rootfs_seed.tar.zst", delivery_dir),
        "artifacts_manifest": entry(slot / "artifacts_manifest.json", delivery_dir),
    }
manifest = {
    "schema_version": 1,
    "delivered_at": datetime.now(UTC).isoformat(timespec="seconds"),
    "build_head": os.environ["DELIVERY_HEAD"],
    "version": {"version_name": os.environ["DELIVERY_NAME"], "version_code": int(os.environ["DELIVERY_CODE"])},
    "arches": arches,
}
out = Path(os.environ["DELIVERY_OUT"])
out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
PYEOF
}

finalize_dual_delivery() { # 双架构全绿后调用：aapt 断言 → staging 双拷 → 原子改名 → delivery_manifest
    local final_x86="$DUAL_DELIVERY_DIR/lingxi-agentos-x86_64-mumu.apk" \
          final_arm="$DUAL_DELIVERY_DIR/lingxi-agentos-arm64-phone.apk" \
          staging_x86="$DUAL_DELIVERY_DIR/.lingxi-agentos-x86_64-mumu.apk.staging" \
          staging_arm="$DUAL_DELIVERY_DIR/.lingxi-agentos-arm64-phone.apk.staging"
    log "交付前断言：aapt 双包版本严格一致（方案④）"
    assert_apk_versions "$OUT_ROOT/x86_64/apk/app-debug.apk" "$OUT_ROOT/aarch64/apk/app-debug.apk" || {
        log "FAIL 双包版本不一致——交付位零改动（旧包全保留）"
        exit 3
    }
    mkdir -p "$DUAL_DELIVERY_DIR"
    cp -f "$OUT_ROOT/x86_64/apk/app-debug.apk" "$staging_x86"
    cp -f "$OUT_ROOT/aarch64/apk/app-debug.apk" "$staging_arm"
    mv -f "$staging_x86" "$final_x86" # 双拷全成才改名=交付位一次翻新（原子语义）
    mv -f "$staging_arm" "$final_arm"
    if ! write_delivery_manifest "$final_x86" "$final_arm"; then
        log "FAIL delivery_manifest 写出失败（交付包已就位，manifest 缺失=本轮不可审计，请排查后重跑 --manifest-only 面）"
        exit 3
    fi
    log "交付 OK: $final_x86（server-mode=local 口径）"
    log "交付 OK: $final_arm（server-mode=local 口径）"
    log "交付 OK: $OUT_ROOT/delivery_manifest.json（version=$RESOLVED_NAME($RESOLVED_CODE) head=$(git -C "$REPO_ROOT" rev-parse --short HEAD)）"
    log "收尾提醒：build.gradle versionCode bump 与交付包 commit 由驱动方 --only 入账（产物面 delivery_manifest 在 release/ 已 gitignore）"
}

# ── ① kernel：cargo-ndk 交叉编译（bionic 内核 + seedx 解包器）────────────

step_kernel() {
    [[ -d "$NDK_HOME" ]] || { log "step kernel: FAIL NDK 不存在: $NDK_HOME"; return 2; }
    case ":$PATH:" in *":$HOME/.cargo/bin:"*) ;; *) PATH="$HOME/.cargo/bin:$PATH" ;; esac
    command -v cargo >/dev/null 2>&1 || { log "step kernel: FAIL cargo 不在 PATH"; return 2; }
    command -v cargo-ndk >/dev/null 2>&1 || { log "step kernel: FAIL cargo-ndk 未安装"; return 2; }
    log "step kernel: cargo ndk -t $TARGET_TRIPLE build --release（增量；kernel+seedx）"
    if ! (cd "$REPO_ROOT/kernel" && ANDROID_NDK_HOME="$NDK_HOME" \
            cargo ndk -t "$TARGET_TRIPLE" build --release --bin agentos-kernel --bin seedx); then
        log "step kernel: FAIL cargo ndk 构建失败（target=$TARGET_TRIPLE）"
        return 3
    fi
    local src seedx_src
    src="$REPO_ROOT/kernel/target/$TARGET_TRIPLE/release/agentos-kernel"
    seedx_src="$REPO_ROOT/kernel/target/$TARGET_TRIPLE/release/seedx"
    if [[ ! -f "$src" || ! -f "$seedx_src" ]]; then
        log "step kernel: FAIL 产物缺失: $src / $seedx_src"
        return 3
    fi
    mkdir -p "$(dirname "$KERNEL_BIN")" "$(dirname "$SEEDX_BIN")"
    cp -f "$src" "$KERNEL_BIN"
    chmod 755 "$KERNEL_BIN" 2>/dev/null || true
    cp -f "$seedx_src" "$SEEDX_BIN"
    chmod 755 "$SEEDX_BIN" 2>/dev/null || true
    log "step kernel: OK $KERNEL_BIN（$(du -h "$KERNEL_BIN" | cut -f1)）+ seedx（$(du -h "$SEEDX_BIN" | cut -f1)）"
}

# ── ①b kernel-linux：WSL 编 glibc 内核（随种子进 proot；架构=宿主 linux 三元组）──
#
# 依存裁定（R5）：主对话管道链上的 tool_core/spill_guard/sensitive_checker 是
# in-process glibc cdylib 插件——bionic 内核无法加载 glibc .so，纯 native 内核
# 架构在对话路径必断；全栈进 proot（内核=glibc 二进制）与 P0.2 实证口径同构。
# WSL 通道：离线复用 Windows 侧 cargo 缓存（CARGO_HOME 挂 /mnt/c + offline；
# WSL NAT 对 crates 镜像持续超时，R5 实测）。
# aarch64 档（R10）：WSL 宿主原生= x86_64，直编产物错架构——glibc 面（内核 +
# 3 cdylib）一律 aarch64-unknown-linux-gnu 交叉（工具链：WSL rustup target +
# gcc-aarch64-linux-gnu）；cdylib 交叉产物 stash 到 kernel-linux/cdylib/，rootfs
# 步换入种子面。x86_64 档维持宿主原生直编（cdylib .so 取工作区现役产物）。
assert_elf_machine() { # $1 = ELF 文件  $2 = 期望 e_machine（62=x86-64 183=aarch64）
    local file="$1" expect="$2" got
    got="$("$PYTHON_BIN" - "$(win_path "$file")" <<'PYEOF'
import struct
import sys

with open(sys.argv[1], "rb") as f:
    hdr = f.read(20)
if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
    print("not-elf")
else:
    print(struct.unpack_from("<H", hdr, 18)[0])
PYEOF
)"
    if [[ "$got" != "$expect" ]]; then
        log "FAIL ELF 架构断言: $file e_machine=$got 期望=$expect"
        return 1
    fi
}

step_kernel_linux_x86_64() {
    # WSL 侧路径随 $REPO_ROOT 参数化（与 aarch64 交叉变体同律）——硬编码主仓
    # 绝对路径时 worktree 出包必错树（build 主仓源码、cp worktree 产物必缺）。
    local repo_wsl linux_bin
    repo_wsl="$(printf '%s' "$REPO_ROOT" | sed -e 's|^/\([a-zA-Z]\)/|/mnt/\L\1/|')"
    linux_bin="$repo_wsl/kernel/target/release/agentos-kernel"
    log "step kernel-linux: WSL cargo build --release --bin agentos-kernel（增量，离线缓存）"
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
        wsl -d Ubuntu -- bash -c '
        set -euo pipefail
        export PATH="$HOME/.cargo/bin:$PATH"
        export CARGO_HOME="${R5_WSL_CARGO_HOME:-/mnt/c/Users/jc/.cargo}"
        export CARGO_NET_OFFLINE=true
        cd "'"$repo_wsl"'/kernel"
        exec cargo build --release --bin agentos-kernel'
    if ! MSYS_NO_PATHCONV=1 wsl -d Ubuntu -- test -f "$linux_bin"; then
        log "step kernel-linux: FAIL WSL 产物缺失"
        return 3
    fi
    mkdir -p "$(dirname "$KERNEL_LINUX_BIN")"
    cp -f "$REPO_ROOT/kernel/target/release/agentos-kernel" "$KERNEL_LINUX_BIN"
    chmod 755 "$KERNEL_LINUX_BIN" 2>/dev/null || true
    if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
        log "step kernel-linux: OK $KERNEL_LINUX_BIN（$(du -h "$KERNEL_LINUX_BIN" | cut -f1)，glibc linux-gnu）"
    else
        log "step kernel-linux: OK $KERNEL_LINUX_BIN"
    fi
}

step_kernel_linux_aarch64() {
    local repo_wsl cdylib_out plugin so_name src
    repo_wsl="$(printf '%s' "$REPO_ROOT" | sed -e 's|^/\([a-zA-Z]\)/|/mnt/\L\1/|')"
    cdylib_out="$OUT_DIR/kernel-linux/cdylib"
    # 实测：bash -c 复合脚本参数（首字符非 /）不经 MSYS 参数路径转换，内嵌
    # /mnt/... 原样达 WSL；坑㉔ 的污染通道是环境变量携路径，此处不适用。
    if ! wsl -d Ubuntu -- bash -c \
            'command -v aarch64-linux-gnu-gcc >/dev/null && export PATH="$HOME/.cargo/bin:$PATH" && rustup target list --installed 2>/dev/null | grep -qx aarch64-unknown-linux-gnu'; then
        log "step kernel-linux: FAIL WSL 缺 aarch64 交叉工具链（apt install gcc-aarch64-linux-gnu + rustup target add aarch64-unknown-linux-gnu）"
        return 2
    fi
    rm -rf "$cdylib_out"
    log "step kernel-linux: WSL 交叉编 aarch64 glibc 面（3 cdylib 先行=工具链 fail-fast，后内核；离线缓存）"
    wsl -d Ubuntu -- bash -c '
        set -euo pipefail
        export PATH="$HOME/.cargo/bin:$PATH"
        export CARGO_HOME="${R5_WSL_CARGO_HOME:-/mnt/c/Users/jc/.cargo}"
        export CARGO_NET_OFFLINE=true
        export CARGO_BUILD_TARGET=aarch64-unknown-linux-gnu
        export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
        # cc/rustc 环境变量只走下划线拼写——bash export 标识符不允许连字符
        export CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc
        export AR_aarch64_unknown_linux_gnu=aarch64-linux-gnu-ar
        cd "'"$repo_wsl"'/plugins/shared/tool_core" && cargo build --release
        cd "'"$repo_wsl"'/plugins/shared/spill_guard" && cargo build --release
        cd "'"$repo_wsl"'/plugins/shared/sensitive_checker" && cargo build --release
        cd "'"$repo_wsl"'/kernel" && cargo build --release --bin agentos-kernel'
    for plugin in tool_core spill_guard sensitive_checker; do
        so_name="libpipeline_${plugin}_native.so"
        src="$REPO_ROOT/plugins/shared/$plugin/target/aarch64-unknown-linux-gnu/release/$so_name"
        if [[ ! -f "$src" ]]; then
            log "step kernel-linux: FAIL cdylib 交叉产物缺失: $src"
            return 3
        fi
        mkdir -p "$cdylib_out/$plugin"
        cp -f "$src" "$cdylib_out/$plugin/$so_name"
    done
    if [[ ! -f "$REPO_ROOT/kernel/target/aarch64-unknown-linux-gnu/release/agentos-kernel" ]]; then
        log "step kernel-linux: FAIL WSL 交叉内核产物缺失"
        return 3
    fi
    mkdir -p "$(dirname "$KERNEL_LINUX_BIN")"
    cp -f "$REPO_ROOT/kernel/target/aarch64-unknown-linux-gnu/release/agentos-kernel" "$KERNEL_LINUX_BIN"
    chmod 755 "$KERNEL_LINUX_BIN" 2>/dev/null || true
    log "step kernel-linux: OK $KERNEL_LINUX_BIN（$(du -h "$KERNEL_LINUX_BIN" | cut -f1)，glibc aarch64 交叉）+ cdylib 三件（$cdylib_out）"
}

step_kernel_linux() {
    if ! command -v wsl >/dev/null 2>&1; then
        log "step kernel-linux: FAIL wsl 不可用（linux-gnu 内核无法构建）"
        return 2
    fi
    if [[ "$ARCH" == "aarch64" ]]; then
        step_kernel_linux_aarch64 || return $?
    else
        step_kernel_linux_x86_64 || return $?
    fi
    assert_elf_machine "$KERNEL_LINUX_BIN" "$ELF_MACHINE" || {
        log "step kernel-linux: FAIL glibc 内核架构错位（错架构入种子=真机必坏，fail-closed）"
        return 3
    }
}

# ── ② APK：现役 app-mobile 构建链（jniLibs + 种子资产 + syncweb + gradle）──

# proot 静态二进制就位（缓存 → GitHub Release + ghfast 兜底=坑⑨配方）。
# x86_64=proot-me v5.5.0 官方静态件；aarch64 官方静态件最新止于 v5.3.0（版本
# skew 如实记录，真机复验轮再评估重编/换源）。
resolve_proot() {
    local cache="$1" dest urls url
    dest="$cache/proot-$DEBARCH"
    [[ -f "$dest" ]] && { echo "$dest"; return 0; }
    if [[ "$DEBARCH" == "amd64" ]]; then
        urls=(
            "https://github.com/proot-me/proot/releases/download/v$PROOT_VERSION/proot"
            "https://ghfast.top/https://github.com/proot-me/proot/releases/download/v$PROOT_VERSION/proot"
        )
    else
        urls=(
            "https://github.com/proot-me/proot/releases/download/v$PROOT_VERSION_AARCH64/proot-v$PROOT_VERSION_AARCH64-aarch64-static"
            "https://ghfast.top/https://github.com/proot-me/proot/releases/download/v$PROOT_VERSION_AARCH64/proot-v$PROOT_VERSION_AARCH64-aarch64-static"
        )
    fi
    for url in "${urls[@]}"; do
        log "step apk: 下载 proot: $url"
        if curl -fL --retry 2 --retry-delay 2 -o "$dest.tmp" "$url"; then
            mv -f "$dest.tmp" "$dest"
            echo "$dest"
            return 0
        fi
        rm -f "$dest.tmp"
    done
    return 1
}

# PBS cpython（linux-gnu）就位：uv python install 无 --platform（主机侧只能装
# 宿主平台），改 python-build-standalone Release 直下——基座解释器随种子进
# /opt/agentos/python，venv 装配后布局打包期预烤（materialize_seed.py 烘焙），
# bin 符号链接与 pyvenv.cfg 都指向此基座。
resolve_pbs_python() {
    local cache="$1" dest urls url
    dest="$cache/pbs-python-$DEBARCH.tar.gz"
    [[ -f "$dest" ]] && { echo "$dest"; return 0; }
    if [[ "$DEBARCH" == "arm64" ]]; then
        urls=(
            "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_PY+$PBS_TAG-aarch64-unknown-linux-gnu-install_only.tar.gz"
            "https://ghfast.top/https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_PY+$PBS_TAG-aarch64-unknown-linux-gnu-install_only.tar.gz"
        )
    else
        urls=(
            "https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_PY+$PBS_TAG-x86_64-unknown-linux-gnu-install_only.tar.gz"
            "https://ghfast.top/https://github.com/astral-sh/python-build-standalone/releases/download/$PBS_TAG/cpython-$PBS_PY+$PBS_TAG-x86_64-unknown-linux-gnu-install_only.tar.gz"
        )
    fi
    for url in "${urls[@]}"; do
        log "step rootfs: 下载 PBS cpython $PBS_PY: $url"
        if curl -fL --retry 2 --retry-delay 2 -o "$dest.tmp" "$url"; then
            mv -f "$dest.tmp" "$dest"
            echo "$dest"
            return 0
        fi
        rm -f "$dest.tmp"
    done
    return 1
}

step_apk() {
    if [[ ! -f "$KERNEL_BIN" || ! -f "$SEEDX_BIN" ]]; then
        log "step apk: FAIL 前置 kernel/seedx 产物缺失（先跑 kernel 步）: $KERNEL_BIN / $SEEDX_BIN"
        return 2
    fi
    if [[ ! -f "$KERNEL_LINUX_BIN" ]]; then
        log "step apk: FAIL 前置 kernel-linux 产物缺失（先跑 kernel_linux 步）: $KERNEL_LINUX_BIN"
        return 2
    fi
    command -v npm >/dev/null 2>&1 || { log "step apk: FAIL npm 不在 PATH"; return 2; }
    if [[ ! -d "$TOOLS_ROOT/jdk21/jdk" ]]; then
        log "step apk: FAIL 便携 jdk21 缺失: $TOOLS_ROOT/jdk21/jdk"
        return 2
    fi
    # kernel + seedx → jniLibs；proot 静态件 → jniLibs（整目录清后置，防跨 arch 陈旧 .so 混装）
    local cache="$OUT_ROOT/cache" proot_bin
    mkdir -p "$cache"
    proot_bin="$(resolve_proot "$cache")" || { log "step apk: FAIL proot 就位失败"; return 3; }
    local jnilibs="$REPO_ROOT/app-mobile/android/app/src/main/jniLibs"
    rm -rf "$jnilibs"
    mkdir -p "$jnilibs/$ABI"
    cp -f "$KERNEL_BIN" "$jnilibs/$ABI/libagentos.so"
    cp -f "$proot_bin" "$jnilibs/$ABI/libproot.so"
    cp -f "$SEEDX_BIN" "$jnilibs/$ABI/libseedx.so"
    # proot 运行依赖（Termux proot-ng 构建 DT_NEEDED）：随 jniLibs 落盘，
    # spawn 期经 LD_LIBRARY_PATH=nativeLibraryDir 解析（壳侧 env 单点注入）。
    # talloc 以 libtalloc.so 落盘（AGP jniLibs 只收 lib*.so 结尾；proot 二进制的
    # DT_NEEDED 已补丁为 libtalloc.so，见 cache 生成侧）。
    local talloc_lib="$cache/libtalloc-$DEBARCH.so" shmem_lib="$cache/libandroid-shmem-$DEBARCH.so"
    if [[ "$ABI" == "arm64-v8a" ]]; then
        cp -f "$talloc_lib" "$jnilibs/$ABI/libtalloc.so"
        cp -f "$shmem_lib" "$jnilibs/$ABI/libandroid-shmem.so"
        cp -f "$cache/proot-loader-$DEBARCH" "$jnilibs/$ABI/libproot_loader.so"
        # 16KB 页设备合规修补（HyperOS「Android 应用兼容性」弹窗修复，2026-11-01）：
        # Termux loader 是 ET_EXEC（PROOT_LOADER env → 内核 binfmt_elf 直接 execve
        # 装载），其 PT_LOAD congruence mod 16KB 失配 → offset-remap 修补（幂等，
        # vaddr/e_entry 零改动）。修补或全库校验失败一律断构建（fail-closed）——
        # 不合规 loader 上真机即弹窗+内核映射错位面。
        "$PYTHON_BIN" "$REPO_ROOT/deploy/elf_16k_align.py" patch "$jnilibs/$ABI/libproot_loader.so" \
            || { log "step apk: FAIL libproot_loader 16KB 修补失败"; return 3; }
        "$PYTHON_BIN" "$REPO_ROOT/deploy/elf_16k_align.py" verify "$jnilibs/$ABI" \
            || { log "step apk: FAIL jniLibs 16KB 完整性校验未过"; return 3; }
    fi
    # rootfs 种子（zstd 预压缩）→ android assets（noCompress=zst 已配 gradle）
    if [[ ! -f "$ROOTFS_SEED" ]]; then
        log "step apk: FAIL 种子缺失（先跑 rootfs 步）: $ROOTFS_SEED"
        return 2
    fi
    mkdir -p "$REPO_ROOT/app-mobile/android/app/src/main/assets"
    cp -f "$ROOTFS_SEED" "$REPO_ROOT/app-mobile/android/app/src/main/assets/rootfs_seed.tar.zst"
    # apiBase 双模式：cloud=capacitor.config.json 真值原样（syncweb 缺省行为）；
    # local=本机内核回环（LINGXI_API_BASE 环境变量单点注入，见 syncweb.mjs）。
    local api_base_env=()
    if [[ "$SERVER_MODE" == "local" ]]; then
        api_base_env=("LINGXI_API_BASE=http://127.0.0.1:$MOBILE_KERNEL_PORT")
    fi
    log "step apk: syncweb（frontend dist + shell-config 注入 + cap sync，server-mode=$SERVER_MODE）"
    if ! (cd "$REPO_ROOT/app-mobile" && env "${api_base_env[@]}" npm run syncweb); then
        log "step apk: FAIL syncweb 链失败"
        return 3
    fi
    # JDK 解析：tools/jdk21/jdk 符号链接可能被并行 WSL 侧构建改指/悬空
    # （2026-11-19 打包轮实证：链接指 ../jdk21-linux/ 且目标缺失）——悬空回退
    # Windows JDK（jdk-win32）。
    local jdk_home="$TOOLS_ROOT/jdk21/jdk"
    [[ -d "$jdk_home/bin" ]] || jdk_home="$TOOLS_ROOT/jdk21/jdk-win32"
    [[ -f "$jdk_home/bin/java.exe" ]] || { log "step apk: FAIL 无可用 Windows JDK（jdk 与 jdk-win32 均缺 bin/java.exe）"; return 3; }
    log "step apk: gradlew assembleDebug（JAVA_HOME=$jdk_home）"
    # 换 arch 重打包前清陈旧 APK 输出与 native 合并中间层：AGP 增量打包对
    # "上一颗更大/含他 ABI .so"的 outputs 不截断，残留字节直接进新包（实测
    # x86_64 重打含 arm64 陈旧尾巴 69MB vs 干净 40MB）
    rm -rf "$REPO_ROOT/app-mobile/android/app/build/outputs/apk/debug" \
        "$REPO_ROOT/app-mobile/android/app/build/intermediates/jniLibs" \
        "$REPO_ROOT/app-mobile/android/app/build/intermediates/merged_native_libs" \
        "$REPO_ROOT/app-mobile/android/app/build/intermediates/stripped_native_libs" \
        "$REPO_ROOT/app-mobile/android/app/build/intermediates/assets"
    if ! (cd "$REPO_ROOT/app-mobile/android" && JAVA_HOME="$jdk_home" \
            ./gradlew assembleDebug --console=plain); then
        log "step apk: FAIL gradle assembleDebug"
        return 3
    fi
    local built="$REPO_ROOT/app-mobile/android/app/build/outputs/apk/debug/app-debug.apk"
    if [[ ! -f "$built" ]]; then
        log "step apk: FAIL APK 产物缺失: $built"
        return 3
    fi
    mkdir -p "$(dirname "$APK_FILE")"
    cp -f "$built" "$APK_FILE"
    log "step apk: OK $APK_FILE（$(du -h "$APK_FILE" | cut -f1)，jniLibs=$ABI + seed 资产）"
}

# ── ③ rootfs：Ubuntu base + 预 sync site-packages 快照 + tar ────────────

# 输入件就位：缓存命中 → .zctmp/p0 历史件复用 → 下载（TUNA / GitHub+ghfast 兜底）。
resolve_ubuntu_tar() {
    local cache="$1" dest="$cache/ubuntu-base-$DEBARCH.tar.gz" legacy
    [[ -f "$dest" ]] && { echo "$dest"; return 0; }
    for legacy in "$REPO_ROOT/.zctmp/p0/ubuntu-base-$DEBARCH.tar.gz" \
                  "$REPO_ROOT/.zctmp/p0/ubuntu-base-$DEBARCH.tar.xz"; do
        if [[ -f "$legacy" ]]; then
            log "step rootfs: 复用历史件 $legacy"
            cp -f "$legacy" "$dest"
            echo "$dest"
            return 0
        fi
    done
    local url="https://mirrors.tuna.tsinghua.edu.cn/ubuntu-cdimage/ubuntu-base/releases/24.04/release/ubuntu-base-24.04.5-base-$DEBARCH.tar.gz"
    log "step rootfs: 下载 ubuntu-base（TUNA）: $url"
    if curl -fL --retry 3 --retry-delay 2 -o "$dest" "$url"; then
        echo "$dest"
        return 0
    fi
    rm -f "$dest"
    return 1
}

resolve_uv_tar() {
    local cache="$1" dest="$cache/uv-$DEBARCH.tar.gz" urls url
    [[ -f "$dest" ]] && { echo "$dest"; return 0; }
    if [[ "$DEBARCH" == "amd64" && -f "$REPO_ROOT/.zctmp/p0/uv-linux.tar.gz" ]]; then
        log "step rootfs: 复用历史件 .zctmp/p0/uv-linux.tar.gz"
        cp -f "$REPO_ROOT/.zctmp/p0/uv-linux.tar.gz" "$dest"
        echo "$dest"
        return 0
    fi
    urls=(
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_PLATFORM.tar.gz"
        "https://ghfast.top/https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$UV_PLATFORM.tar.gz"
    )
    for url in "${urls[@]}"; do
        log "step rootfs: 下载 uv $UV_VERSION: $url"
        if curl -fL --retry 2 --retry-delay 2 -o "$dest" "$url"; then
            echo "$dest"
            return 0
        fi
        rm -f "$dest"
    done
    return 1
}

# editable SDK 链接改写：主机侧 uv 装出的 __editable__* 内嵌主机绝对路径，
# 设备端不可达——改写指向部署面规范路径（坑⑯）。设备路径硬编码在带引号
# heredoc 内：经环境变量传 Windows python 会被 MSYS 环境变量路径转换污染成
# C:/Program Files/Git/... （实测）。
fix_sdk_editable_pth() { # $1 = 目标 site-packages
    HOST_SDK_SRC="$REPO_ROOT/plugins/sdk/src" \
    SNAPSHOT_SP="$1" \
    "$PYTHON_BIN" - <<'PYEOF'
import os
import pathlib

sp = pathlib.Path(os.environ["SNAPSHOT_SP"])
host = os.environ["HOST_SDK_SRC"].replace("\\", "/")
dev = "/opt/agentos/plugins/sdk/src"
rewritten = 0
for entry in sp.glob("__editable__*"):
    if not entry.is_file():
        continue
    text = entry.read_text(encoding="utf-8", errors="replace")
    new = text.replace(host, dev).replace(host.replace("/", "\\"), dev)
    if new != text:
        entry.write_text(new, encoding="utf-8")
        rewritten += 1
print(f"[build-mobile] step rootfs:   editable-rewrite={rewritten} ({sp.name})")
PYEOF
}

# 单插件快照：优先 --frozen（锁真值），失败时 staging 影子副本回退（锁在影子
# 目录现生成，不触碰仓内 uv.lock）；产出 <name>/lib/python<py>/site-packages。
snapshot_one_venv() { # $1 = 插件目录  $2 = stage  $3 = venv 临时区
    local dir="$1" stage="$2" vtmp="$3"
    local name sp_src
    name="$(basename "$dir")"
    log "step rootfs: uv sync 快照 $name（--python-platform $UV_PLATFORM 钉死=坑⑳配方）"
    if UV_PROJECT_ENVIRONMENT="$(win_path "$vtmp/$name")" UV_PYTHON="$SNAPSHOT_PYTHON" \
            uv sync --frozen --project "$(win_path "$dir")" --python-platform "$UV_PLATFORM" --quiet; then
        :
    else
        local shadow="$vtmp/shadow/plugins/shared/$name" # 影子副本放 vtmp（overlay 之外，防泄入种子）
        log "step rootfs:   $name --frozen 失败（无锁/锁漂移），staging 影子副本回退（不动仓内锁）"
        rm -rf "$shadow"
        # 影子树须带 sdk 源：插件 pyproject 的 tool.uv.sources 以 ../../sdk 相对映射
        if [[ ! -d "$vtmp/shadow/plugins/sdk" ]]; then
            mkdir -p "$vtmp/shadow/plugins"
            (cd "$REPO_ROOT/plugins" && tar cf - --exclude='__pycache__' --exclude='*.pyc' \
                --exclude='*.egg-info' sdk) | (cd "$vtmp/shadow/plugins" && tar xf -)
        fi
        mkdir -p "$(dirname "$shadow")"
        (cd "$dir" && tar cf - --exclude='.venv' --exclude='.venv-*' --exclude='__pycache__' \
            --exclude='*.pyc' --exclude='target' --exclude='.mypy_cache' --exclude='node_modules' .) \
            | (mkdir -p "$shadow" && cd "$shadow" && tar xf -)
        if ! UV_PROJECT_ENVIRONMENT="$(win_path "$vtmp/$name")" UV_PYTHON="$SNAPSHOT_PYTHON" \
                uv sync --project "$(win_path "$shadow")" --python-platform "$UV_PLATFORM" --quiet; then
            log "step rootfs:   $name 影子回退仍失败"
            return 1
        fi
    fi
    sp_src="$vtmp/$name/Lib/site-packages" # 主机侧 uv 的 Windows 布局
    [[ -d "$sp_src" ]] || sp_src="$vtmp/$name/lib/python$SNAPSHOT_PYTHON/site-packages"
    if [[ ! -d "$sp_src" ]]; then
        log "step rootfs:   $name 快照产出缺失（site-packages 未生成）"
        return 1
    fi
    local sp_dst="$2/opt/agentos/venvs/$name/lib/python$SNAPSHOT_PYTHON/site-packages"
    mkdir -p "$sp_dst"
    (cd "$sp_src" && tar cf - .) | (cd "$sp_dst" && tar xf -)
    fix_sdk_editable_pth "$sp_dst" || return 1
    log "step rootfs:   $name OK（$(du -sh "$sp_dst" | cut -f1)）"
}

step_rootfs() {
    command -v uv >/dev/null 2>&1 || { log "step rootfs: FAIL uv 不在 PATH"; return 2; }
    command -v curl >/dev/null 2>&1 || { log "step rootfs: FAIL curl 不在 PATH"; return 2; }
    [[ -f "$KERNEL_LINUX_BIN" ]] || { log "step rootfs: FAIL kernel-linux 缺失（先跑 kernel_linux 步）: $KERNEL_LINUX_BIN"; return 2; }
    local cache="$OUT_ROOT/cache"
    mkdir -p "$cache"
    local ubuntu_tar uv_tar pbs_tar
    ubuntu_tar="$(resolve_ubuntu_tar "$cache")" || { log "step rootfs: FAIL ubuntu-base 就位失败"; return 3; }
    uv_tar="$(resolve_uv_tar "$cache")" || { log "step rootfs: FAIL uv(linux) 就位失败"; return 3; }
    pbs_tar="$(resolve_pbs_python "$cache")" || { log "step rootfs: FAIL PBS cpython 就位失败"; return 3; }

    # overlay 树（seedroot）：不落盘解包 ubuntu——符号链接与权限位经
    # pack_rootfs_seed.py 在 tar 条目层保真（Windows 宿主 fs 两者皆不可重建）。
    local stage="$OUT_DIR/rootfs/staging"
    local overlay="$stage/seedroot"
    rm -rf "$stage"
    mkdir -p "$overlay/etc" "$overlay/usr/local/bin" "$overlay/opt/agentos"
    printf 'nameserver 8.8.8.8\n' > "$overlay/etc/resolv.conf" # 坑⑲：ubuntu-base 原值指向 Android netd 不可达

    # linux-gnu 内核 → overlay 根（栈内 glibc cdylib 插件的宿主，全栈进 proot）
    cp -f "$KERNEL_LINUX_BIN" "$overlay/opt/agentos/agentos-kernel"

    # 部署面：plugins（shared + sdk=坑⑯）+ config + skills + deploy；venv 不复制
    # （由快照承载）；构建垃圾不入种子（R5 拆账：target/ 1.28GB + .venv-hindsight
    # 575MB + .mypy_cache 275MB，合计部署面 2.17GB→~350MB）；*.dll=Windows cdylib
    # 设备无用；node_modules（gitignored 产物，dsh_adapter runtime 含指向宿主绝对
    # 路径的符号链接，tar 不可重建也无设备价值）排除；.cache=构建/下载缓存
    # （设备端零价值；运行期真机 root/.cache 膨胀另由 start_stack.sh 启动清场，
    # 见移动端存储优化台账 2026-11-19）；deploy/remote=部署机真值面
    # （fmin systemd 单元/本地 mcp 真值与审计日志）不进种子（2026-11-18 个人信息
    # 出仓处置，assert-seed 禁入断言兜底）。
    log "step rootfs: 部署面拷贝（plugins/shared + plugins/sdk + config + skills + deploy，构建垃圾与 deploy/remote 真值面排除）"
    (cd "$REPO_ROOT" && tar cf - \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
        --exclude='.venv-*' --exclude='target' --exclude='.mypy_cache' \
        --exclude='.cache' \
        --exclude='*.dll' \
        --exclude='node_modules' \
        --exclude='deploy/remote' \
        plugins/shared plugins/sdk config skills deploy) \
        | (cd "$overlay/opt/agentos" && tar xf -)
    local deploy_item
    for deploy_item in plugins/shared plugins/sdk config skills deploy; do
        if [[ ! -d "$overlay/opt/agentos/$deploy_item" ]]; then
            log "step rootfs: FAIL 部署面拷贝不完整（$deploy_item 缺失；符号链接/IO 错误见上）"
            return 3
        fi
    done
    # .cache 现清双保险：tar 排除漏网（嵌套面/未来新增部署项带出）时，打包前对
    # overlay 现场再清一遍（幂等；无 .cache 时零操作零日志）。此类目录是构建/
    # 下载缓存，种子内零价值、纯占装机体积。
    local cache_dir cache_swept=0
    while IFS= read -r cache_dir; do
        rm -rf "$cache_dir"
        cache_swept=$((cache_swept + 1))
    done < <(find "$overlay/opt/agentos" -type d -name .cache 2>/dev/null)
    [[ "$cache_swept" -gt 0 ]] && log "step rootfs: overlay .cache 现清 $cache_swept 处（排除清单双保险）"

    # 桌面专属 external MCP 不进移动种子（设计公理 2026-11-01：能在打包期做好
    # 的就在打包期做好）。windows-mcp 命令面=uvx，安卓 proot 永不可 spawn——
    # 留守种子只产出「生逢失败的插件」：discovery/G2 tick 每 60s 烧一轮注定
    # 失败的观测+复验重注册（R-2 设备端首启日志零校验类字样，移动版精细测试
    # 台账 2026-11-01 P0 立案节定年：2026-09-18 config/mcp.json 一行接入后
    # 入种子）。两面同清：受管插件目录（loader 每轮 discover 从 config/mcp.json
    # 再生，只删目录会复活）+ config/mcp.json 条目本体。
    local mcp_json="$overlay/opt/agentos/config/mcp.json"
    if [[ -f "$mcp_json" ]] && grep -q 'windows-mcp' "$mcp_json"; then
        if ! "$PYTHON_BIN" - "$(win_path "$mcp_json")" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
servers = data.get("mcpServers")
if isinstance(servers, dict) and servers.pop("windows-mcp", None) is not None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"pruned windows-mcp: {path}")
PY
        then
            log "step rootfs: FAIL windows-mcp 出种子失败（config/mcp.json 清洗错误见上）"
            return 3
        fi
    fi
    if [[ -d "$overlay/opt/agentos/plugins/shared/mcp_windows_mcp" ]]; then
        rm -rf "$overlay/opt/agentos/plugins/shared/mcp_windows_mcp"
        log "step rootfs: 桌面专属受管插件 mcp_windows_mcp 已出种子面"
    fi

    # aarch64 种子面 glibc 换装：部署面拷来的 cdylib .so 是工作区宿主架构产物
    # （x86_64，R9 补种口径）——错架构 cdylib 入 aarch64 种子=设备端插件装载必坏，
    # 一律以 kernel-linux 步交叉产物覆盖（缺失即 fail-closed，禁架构混装）。
    if [[ "$ARCH" == "aarch64" ]]; then
        local cdy_plugin cdy_so cdy_src
        for cdy_plugin in tool_core spill_guard sensitive_checker; do
            cdy_so="libpipeline_${cdy_plugin}_native.so"
            cdy_src="$OUT_DIR/kernel-linux/cdylib/$cdy_plugin/$cdy_so"
            if [[ ! -f "$cdy_src" ]]; then
                log "step rootfs: FAIL aarch64 cdylib 缺失（先跑 kernel-linux 步）: $cdy_src"
                return 2
            fi
            cp -f "$cdy_src" "$overlay/opt/agentos/plugins/shared/$cdy_plugin/$cdy_so"
        done
        log "step rootfs: aarch64 cdylib 三件已换入种子面"
    fi

    # 配置脱敏（坑㊳配套：真 key 永不进 APK）：对 overlay 副本清明文 key
    # （占位符/掩码/示例值/空值/指针引用保留），只动副本不触仓内 config；
    # yaml 解析失败 fail-closed 拦截。
    log "step rootfs: 配置脱敏（overlay config 副本，明文 key 清零）"
    if ! "$PYTHON_BIN" "$(win_path "$REPO_ROOT/deploy/sanitize_seed_config.py")" sanitize \
            --config-dir "$(win_path "$overlay/opt/agentos/config")"; then
        log "step rootfs: FAIL 配置脱敏失败（见上）"
        return 3
    fi

    # uv（linux 二进制）入 rootfs：设备端按锁修复/补 sync 的工具底座
    mkdir -p "$stage/uvsrc"
    tar -xzf "$uv_tar" -C "$stage/uvsrc"
    local uvbin
    uvbin="$(find "$stage/uvsrc" -type f -name uv | head -1)"
    if [[ -z "$uvbin" ]]; then
        log "step rootfs: FAIL uv 二进制未在包内找到: $uv_tar"
        return 3
    fi
    cp -f "$uvbin" "$overlay/usr/local/bin/uv"

    # 预 sync 快照：选取规则与 deploy/init.sh 同律（_host + 有 plugin.json 且非
    # host_group 的插件；合宿成员依赖并集已并入 _host，自身 .venv 零消费）。
    local vtmp="$stage/venvs-tmp"
    mkdir -p "$vtmp"
    local -a snapshot_failed=()
    local -a snapshot_dirs=()
    snapshot_dirs+=("$REPO_ROOT/plugins/shared/_host")
    while IFS= read -r dir; do
        local host_group
        host_group="$("$PYTHON_BIN" - "$(win_path "$dir/plugin.json")" <<'PYEOF'
import json
import sys

meta = json.load(open(sys.argv[1], encoding="utf-8"))
print(meta.get("host_group") or "")
PYEOF
)"
        [[ -n "$host_group" ]] && continue
        snapshot_dirs+=("$dir")
    done < <(find "$REPO_ROOT/plugins/shared" -maxdepth 2 -type f -name pyproject.toml -exec dirname {} \; \
        | while IFS= read -r d; do [[ -f "$d/plugin.json" ]] && printf '%s\n' "$d"; done | sort)
    local dir
    for dir in "${snapshot_dirs[@]}"; do
        snapshot_one_venv "$dir" "$overlay" "$vtmp" || snapshot_failed+=("$(basename "$dir")")
    done
    if [[ ${#snapshot_failed[@]} -gt 0 ]]; then
        log "step rootfs: FAIL 快照失败插件: ${snapshot_failed[*]}"
        return 3
    fi
    cat > "$overlay/opt/agentos/venvs/README.txt" <<EOF
预 sync site-packages 快照（deploy/build_mobile.sh rootfs 步产物，勿手改）
- 布局：<plugin>/lib/python$SNAPSHOT_PYTHON/site-packages（解释器钉值 $SNAPSHOT_PYTHON）
- 平台：构建期 uv --python-platform $UV_PLATFORM 钉死（坑⑳：proot 内 uname 谎报架构，
  设备端禁止按 uname 重装；修复/补包用 /usr/local/bin/uv 且显式带平台钉值）
- editable SDK 已改写指向 /opt/agentos/plugins/sdk/src（坑⑯：sdk 必在部署面）
- 消费：materialize_seed.py 打包期烘焙为装配后布局 opt/agentos/plugin-venvs/<id>/
  （登记处，start_stack.sh 经 AGENTOS_PLUGIN_VENVS_DIR 指向）；本目录为烘焙源，
  保留回滚兼容，设备端不再执行 mobile_init.sh
EOF
    # 打包前断言：editable pth 必须已改写为设备规范路径（MSYS 污染/漏改写即拦截）
    local pth_file
    pth_file="$(ls "$overlay/opt/agentos/venvs/_host/lib/python$SNAPSHOT_PYTHON/site-packages/"__editable__*.pth 2>/dev/null | head -1)"
    if [[ -z "$pth_file" ]] || ! grep -q '^/opt/agentos/plugins/sdk/src$' "$pth_file"; then
        log "step rootfs: FAIL editable pth 未指向 /opt/agentos/plugins/sdk/src（$pth_file）"
        return 3
    fi

    # 打包：ubuntu 原包流式保真（符号链接/权限位逐条目过）+ overlay 注入 +
    # PBS cpython 条目级注入 + 跨 venv 硬链接去重 + zstd 压缩
    mkdir -p "$(dirname "$ROOTFS_SEED")"
    log "step rootfs: pack（ubuntu 流式保真 + overlay + PBS 注入 + dedup + zstd）→ $ROOTFS_SEED"
    "$PYTHON_BIN" "$(win_path "$REPO_ROOT/deploy/pack_rootfs_seed.py")" \
        --ubuntu "$(win_path "$ubuntu_tar")" --overlay "$(win_path "$overlay")" \
        --out "$(win_path "$ROOTFS_SEED")" \
        --inject-tar "$(win_path "$pbs_tar")" --inject-prefix opt/agentos/python \
        --dedup --zstd
    # 种子打包期物化+烘焙（设计公理=移动端零装配零校验；真机实证：跨 venv
    # 硬链接在 untrusted_app 域不可 link()=seedx 首启解包必炸；resolv 8.8.8.8
    # 大陆网络不可达；venv 运行期装配 17 个 ≈1-2min）。hardlink 物化为实体
    # 文件 + resolv 写定国内 DNS + venv 装配后布局预烤 plugin-venvs/ + 发现
    # 缓存预烤（T4/R-2 层 2：内核 boot 对账通过即跳过扫描/G2 复验），幂等。
    log "step rootfs: 种子物化（hardlink→实体文件）+resolv 写定+venv 预烤+发现缓存预烤+快照丢弃"
    if ! "$PYTHON_BIN" "$(win_path "$REPO_ROOT/deploy/materialize_seed.py")" --seed "$(win_path "$ROOTFS_SEED")" \
        --bake-discovery-cache --drop-snapshots --zstd-level "$SEED_ZSTD_LEVEL" --zstd-threads "$SEED_ZSTD_THREADS"; then
        log "step rootfs: FAIL 种子物化/烘焙失败（hardlink 残留或 venv 快照缺失）"
        return 3
    fi
    # 种子关键条目断言（zst 无法经宿主 tar 直读，走 python zstandard 流式验）；
    # 断言吃在物化+烘焙后的最终产物上（物化步先于本断言，烘焙缺失即拦分发）。
    # ELF 架构面同刀断言（glibc 内核+3 cdylib 必须与 --arch 档同 e_machine）。
    # cdylib 三件在 required=缺失即拦（R10 坏种子立案：650ae568 干净树出包缺三件
    # 而 elf_paths 只验"见到才查架构"，缺失静默放行=内核 G2 拒载主对话不可用）。
    # 显式接返回值：run_full_pipeline 处于 if ! 上下文 set -e 失效（坑㉒同族），
    # 裸命令失败不拦截=断言 fail-open（R27 出包轮实测：worktree 缺 cdylib 三件
    # 打了 FAIL 仍放行到 APK 步）。
    if ! SEED_ELF_MACHINE="$ELF_MACHINE" \
    "$PYTHON_BIN" - "$(win_path "$ROOTFS_SEED")" <<'PYEOF'
import os
import struct
import sys
import tarfile

import zstandard

seed = sys.argv[1]
em_expect = int(os.environ.get("SEED_ELF_MACHINE", "0") or 0)
required = [
    "bin",                       # ubuntu 顶层符号链接（bin→usr/bin）
    "usr/bin/env",
    "opt/agentos/plugins/sdk/pyproject.toml",
    "opt/agentos/plugin-venvs/_host/lib/python3.12/site-packages/",  # 装配面 site-packages 根（丢弃快照后唯一来源）
    "opt/agentos/plugin-venvs/",                    # 烘焙登记处（装配后布局预烤）
    "opt/agentos/plugin-venvs/_host/pyvenv.cfg",    # 烘焙骨架（合宿宿主键）
    "usr/local/bin/uv",
    "opt/agentos/agentos-kernel",
    "opt/agentos/plugins/shared/tool_core/libpipeline_tool_core_native.so",
    "opt/agentos/plugins/shared/spill_guard/libpipeline_spill_guard_native.so",
    "opt/agentos/plugins/shared/sensitive_checker/libpipeline_sensitive_checker_native.so",
    "opt/agentos/deploy/mobile_init.sh",
    "opt/agentos/deploy/start_stack.sh",
    "opt/agentos/python/bin/python3.12",
]
elf_paths = {
    "opt/agentos/agentos-kernel",
    "opt/agentos/plugins/shared/tool_core/libpipeline_tool_core_native.so",
    "opt/agentos/plugins/shared/spill_guard/libpipeline_spill_guard_native.so",
    "opt/agentos/plugins/shared/sensitive_checker/libpipeline_sensitive_checker_native.so",
}
found = set()
elf_bad = []
elf_seen = set()
with zstandard.open(seed, "rb") as f:
    with tarfile.open(fileobj=f, mode="r|") as tf:
        for m in tf:
            name = m.name[2:] if m.name.startswith("./") else m.name
            for req in required:
                if name == req or (req.endswith("/") and name.startswith(req)):
                    found.add(req)
            if em_expect and name in elf_paths and name not in elf_seen:
                fo = tf.extractfile(m)
                hdr = fo.read(20) if fo is not None else b""
                elf_seen.add(name)
                if len(hdr) < 20 or hdr[:4] != b"\x7fELF" or \
                        struct.unpack_from("<H", hdr, 18)[0] != em_expect:
                    elf_bad.append(name)
missing = [r for r in required if r not in found]
if missing or elf_bad:
    print(f"[build-mobile] step rootfs: FAIL 关键条目缺失={missing} ELF 架构错位={elf_bad}",
          file=sys.stderr)
    sys.exit(3)
msg = f"关键条目 {len(required)}/{len(required)} 全中"
if em_expect:
    msg += f"；ELF 架构 {len(elf_seen)}/{len(elf_paths)} 面 e_machine={em_expect} 过"
print(f"[build-mobile] step rootfs: {msg}")
PYEOF
then
    log "step rootfs: FAIL 种子关键条目/ELF 架构断言未过（缺失即拦）——交付位零改动"
    return 3
fi
    # 产物级 key 断言（坑㊳配套 G-M0 关口）：对打包后的种子复跑同一判定——
    # 产物内不得出现真 key，违约即拦截分发（退出码 3）
    log "step rootfs: 产物 key 断言（config 面零明文）"
    if ! "$PYTHON_BIN" "$(win_path "$REPO_ROOT/deploy/sanitize_seed_config.py")" assert-seed --seed "$(win_path "$ROOTFS_SEED")"; then
        log "step rootfs: FAIL 产物内检出明文 key（种子不得分发）"
        return 3
    fi
    rm -rf "$stage"
    log "step rootfs: OK $ROOTFS_SEED（$(du -h "$ROOTFS_SEED" | cut -f1)）"
}

# 相对路径规范化为绝对路径：manifest 内相对路径按契约相对 manifest 目录解析
# （可搬移产物包语义），构建期 cwd 相对路径直接入库会在验面错位——入口先定型。
abspath() {
    local dir name
    dir="$(dirname "$1")"
    name="$(basename "$1")"
    (cd "$dir" 2>/dev/null && printf '%s/%s' "$(pwd)" "$name")
}

step_manifest() {
    local k p r
    if [[ "$MODE" == "manifest-only" ]]; then
        [[ -n "$ARG_KERNEL" && -n "$ARG_PROOT" && -n "$ARG_ROOTFS" ]] ||
            die "--manifest-only 需 --kernel/--proot/--rootfs 指明已有产物路径"
        k="$(abspath "$ARG_KERNEL")" || die "kernel 产物路径不可达: $ARG_KERNEL"
        p="$(abspath "$ARG_PROOT")" || die "proot 产物路径不可达: $ARG_PROOT"
        r="$(abspath "$ARG_ROOTFS")" || die "rootfs 产物路径不可达: $ARG_ROOTFS"
    else
        k="$KERNEL_BIN"
        # proot=apk 步实际就位的缓存件（resolve_proot 命中缓存零下载）；此前
        # 误写 APK 路径=proot 条目从未被真验（verify 自洽假绿），20261015 修正
        p="$(resolve_proot "$OUT_ROOT/cache")" ||
            die "proot 产物不可达（先跑 --step apk 或 full）: $OUT_ROOT/cache"
        r="$ROOTFS_SEED"
    fi
    log "step manifest: 写 $MANIFEST（arch=$ARCH version=$VERSION）"
    # 值同样传给 Windows 程序：POSIX 路径作参数值须 win_path（54cc27143 同律；
    # 漏网致 --artifact 三值被解析成 \d\... 而 manifest 假红，APK 本体无损）
    "$PYTHON_BIN" "$(win_path "$MANIFEST_PY")" write \
        --out "$(win_path "$MANIFEST")" --arch "$ARCH" --version "$VERSION" \
        --artifact "kernel=$(win_path "$k")" --artifact "proot=$(win_path "$p")" \
        --artifact "rootfs=$(win_path "$r")"
}

step_verify() {
    local target="$1"
    log "step verify: 校验 $target（digest 不匹配=拒绝分发/启动，退出码非零）"
    "$PYTHON_BIN" "$(win_path "$MANIFEST_PY")" verify --manifest "$(win_path "$target")"
}

run_full_pipeline() { # 单 arch 全链：kernel→kernel-linux→rootfs→apk→manifest→verify
    # 坑㉒同律：调用处为 || 上下文时 set -e 对整函数失效，子步失败经返回值显式串接
    local failed=0
    step_kernel || failed=1
    [[ "$failed" -eq 0 ]] && { step_kernel_linux || failed=1; }
    [[ "$failed" -eq 0 ]] && { step_rootfs || failed=1; }
    [[ "$failed" -eq 0 ]] && { step_apk || failed=1; }
    if [[ "$failed" -ne 0 ]]; then
        log "full 构建失败（上方 step 日志，arch=$ARCH）——修复后重跑；已有外部产物可走 --manifest-only"
        return 3
    fi
    step_manifest || return 3
    step_verify "$MANIFEST" || return 1
}

case "$MODE" in
    verify-only)
        [[ -n "$ARG_MANIFEST" ]] || die "--verify-only 需 --manifest 指明待验 manifest"
        step_verify "$MANIFEST"
        ;;
    manifest-only)
        step_manifest
        step_verify "$MANIFEST"
        ;;
    step)
        case "$STEP_ONLY" in
            apk)
                # 单步前置断言（复用既有产物，缺件即退——禁静默半新半旧产物组合）
                [[ -f "$KERNEL_BIN" && -f "$SEEDX_BIN" ]] ||
                    die "--step apk 需既有 kernel 产物（先跑 full 或 --step kernel）: $KERNEL_BIN"
                [[ -f "$ROOTFS_SEED" ]] ||
                    die "--step apk 需既有 rootfs 种子（先跑 full）: $ROOTFS_SEED"
                step_apk
                step_manifest
                step_verify "$MANIFEST"
                ;;
            kernel)
                step_kernel
                ;;
            kernel-linux)
                step_kernel_linux
                ;;
            rootfs)
                # 种子面重打（内核换刀口径）：部署面取当前 $REPO_ROOT——工作区
                # 在飞改动会入种子，正确性缝=R7 apk 单步同款；干净树要求由
                # 驱动方保证（如 git worktree 干净检出后在本仓外跑本步）。
                [[ -f "$KERNEL_LINUX_BIN" ]] ||
                    die "--step rootfs 需既有 kernel-linux 产物（先跑 full 或 --step kernel-linux）: $KERNEL_LINUX_BIN"
                step_rootfs
                step_manifest
                step_verify "$MANIFEST"
                ;;
            *) die "--step 只认 apk|kernel|kernel-linux|rootfs（产物未变的面勿重跑，防工作区在飞改动入种子）" ;;
        esac
        ;;
    full)
        if [[ -z "$ARCH" ]]; then
            # ── 统一双架构一次全打（缺省，方案①）：版本单源循环外一次定型（方案②，
            #    杜绝两次调用间 HEAD/版本漂移）→ x86_64→aarch64 连续构建 → 全绿才
            #    原子交付（方案③）；任一架构失败 fail-fast=交付位零改动（旧包全保留）。
            if [[ "$SERVER_MODE_EXPLICIT" -eq 1 || "$PORT_EXPLICIT" -eq 1 ]]; then
                die "双架构模式口径=双档全 local（2026-10-23 用户令「装上就能用」）；单架构定制走 --arch + --server-mode/--port（如 cloud 云档调试包）"
            fi
            AAPT_BIN="$(resolve_aapt)" ||
                die "aapt 不可得（$TOOLS_ROOT/android-sdk/build-tools 无 build-tools；用 BUILD_MOBILE_AAPT 显式指定）——aapt 版本断言（方案④）必需"
            bump_version_code
            [[ "$VERSION_EXPLICIT" -eq 1 ]] || VERSION="v${RESOLVED_NAME}(${RESOLVED_CODE})"
            for arch_round in x86_64 aarch64; do
                set_arch_vars "$arch_round"
                assert_version_unchanged # 两轮解析值一致（防御，方案②）
                SERVER_MODE="local" # 双档均本机内核口径（2026-10-23 用户令「装上就能用」；接替 R10/R14 的 x86_64=local/aarch64=cloud 惯例）
                log "═══ 双架构轮 $ARCH（server-mode=$SERVER_MODE，version=$RESOLVED_NAME($RESOLVED_CODE)）═══"
                if ! run_full_pipeline; then
                    log "双架构构建失败（arch=$ARCH）——交付位零改动（旧包全保留）；build.gradle bump（versionCode=$RESOLVED_CODE）留树未 commit，修复后重跑或 git checkout -- app-mobile/android/app/build.gradle 还原"
                    exit 3
                fi
            done
            finalize_dual_delivery
        else
            # ── 单架构 full（CI/调试）：树内现值构建，不 bump 不交付。
            resolve_version
            AAPT_BIN="$(resolve_aapt)" ||
                die "aapt 不可得（$TOOLS_ROOT/android-sdk/build-tools 无 build-tools；用 BUILD_MOBILE_AAPT 显式指定）——APK 版本断言必需"
            [[ "$VERSION_EXPLICIT" -eq 1 ]] || VERSION="v${RESOLVED_NAME}(${RESOLVED_CODE})"
            run_full_pipeline || exit $?
            assert_apk_versions "$APK_FILE" || exit 3 # 单包版本挂载断言（方案④的单元面）
        fi
        ;;
esac
