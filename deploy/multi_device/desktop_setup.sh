#!/usr/bin/env bash
# =============================================================================
# 桌面端统一命名空间锚点配方（Git Bash / MSYS）—— 交付物，待用户知情执行
#
# 契约：ADR 2026-10-18-unified-namespace-fs-mount-and-mcp-transfer
#   D16（路径语言 /<设备名>/ 逐字相同；Git 安装根 junction + 盘根 junction 锚）
#   D17（桌面 = rclone mount(WinFsp) 挂他端 + junction 锚）
#   D20（--read-only = 只读快照轴；断链 = OS 层诚实不可用）
#
# 用法（默认 plan 干跑，只打印将执行的动作，零系统变更）：
#   ./desktop_setup.sh <设备名> --remote <rclone-remote>:<子路径>   # 挂他端领地
#   ./desktop_setup.sh <设备名> --local <本端领地真实目录>          # 本端前缀锚定
#   ./desktop_setup.sh <设备名> --remote <remote>:<path> --apply --read-only
#   ./desktop_setup.sh --check        # 仅体检依赖（WinFsp/rclone/保留名）
#
# 锚点两级（D16）：
#   ① Git 安装根 junction  <Git根>/\<设备名>   → bash 内 /<设备名>/... 逐字读写（验收面）
#   ② 盘根 junction        C:\<设备名>         → OS 层锚（agent 面不感知，D16 分界）
# 两级 junction 均指向同一真实挂载目录（rclone/WinFsp 挂载点或本端领地目录）。
# Git 安装根与盘根的写入需管理员——以管理员 Git Bash 运行 --apply；
# 安装根不可写时的降级：跳过①，bash 内用 /c/<设备名>/... cygdrive 形态
# （D16 留部署配方裁量），盘根级 ② 仍建议补上。
#
# WinFsp 安装策略（agent/后台代装永不自动，需显式知情触发；预带除外——预带的
# 知情同意 = 用户安装灵汐助手这个动作本身。2026-10-18 晚拍板安装器管理员启动：
# NSIS manifest requireAdministrator（nsis.perMachine=true），customInstall 的
# msiexec /qn 天然提权执行，装机即静默装完 WinFsp，零补装）：
#   路径① 装机包预带（默认）：2026-10-18 起在提权安装器内即完成——本脚本与
#          --bootstrap-winfsp 对新包装机不再需要，降级为存量环境修复入口
#   路径② 存量修复：--bootstrap-winfsp 显式触发 winget（或官方 MSI
#          `msiexec /i winfsp-<版>.msi /qn` 提权上下文运行）；设置页修复入口待前端刀并入
# =============================================================================

set -euo pipefail

RCLUBE_PINNED="1.75.2"   # 版本钉选见 README §版本钉选；升级=CVE 驱动 patch 位
RESERVED_NAMES="$(dirname "$0")/reserved_names.txt"

die() { echo "[desktop_setup] 错误：$*" >&2; exit 2; }
plan() { echo "[desktop_setup] 将执行: $*"; }
run() { if [[ "${APPLY:-0}" == "1" ]]; then "$@"; else plan "$*"; fi; }

winpath() { cygpath -w "$1"; }

# ---- 依赖体检 --------------------------------------------------------------
check_winfsp() {
  # WinFsp 2.1 布局（1018 装机实证）：dll/sys 在 SxS\sxs.<ts>\bin\，服务名
  # WinFsp.Launcher（非 winfsp）；旧版顶层 bin\ 形态并存兼容（升级机两形态都认）。
  if [[ -d "/c/Program Files (x86)/WinFsp/bin" ]] || compgen -G "/c/Program Files (x86)/WinFsp/SxS/sxs.*/bin" > /dev/null; then
    echo "[desktop_setup] WinFsp: 已安装（预带/既有安装，安装步跳过）"
    return 0
  fi
  echo "[desktop_setup] WinFsp: 未安装（rclone mount 硬依赖）。两路径择一："
  echo "  ① 装机包预带：重装灵汐助手（2026-10-18 起安装器提权，装机即静默装完）"
  echo "  ② 存量修复：--bootstrap-winfsp（winget/官方 MSI，提权上下文运行）"
  return 1
}

check_rclone() {
  if command -v rclone >/dev/null 2>&1; then
    local v; v="$(rclone version 2>/dev/null | head -1 | awk '{print $2}')"
    echo "[desktop_setup] rclone: ${v}（钉选 ${RCLUBE_PINNED}，不一致请对齐 README §版本钉选）"
    return 0
  fi
  echo "[desktop_setup] rclone: 未安装。安装后请确认版本 = ${RCLUBE_PINNED}"
  return 1
}

if [[ "${1:-}" == "--check" ]]; then
  check_winfsp || true
  check_rclone || true
  exit 0
fi

# ---- 参数解析 --------------------------------------------------------------
DEVICE="${1:?用法见脚本头注释}"; shift
MODE=""; SOURCE=""; APPLY=0; READONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --remote) MODE="remote"; SOURCE="${2:?}"; shift 2 ;;
    --local)  MODE="local";  SOURCE="${2:?}"; shift 2 ;;
    --apply)  APPLY=1; shift ;;
    --read-only) READONLY=1; shift ;;
    --bootstrap-winfsp)
      if check_winfsp; then
        echo "[desktop_setup] WinFsp 已安装（预带/既有），bootstrap 跳过"
      else
        echo "[desktop_setup] WinFsp 静默引导（存量修复入口；需提权上下文运行）..."
        winget install -e --id WinFsp.WinFsp --silent || die "WinFsp 引导失败，请按脚本头注释路径①/②处理"
      fi
      shift ;;
    *) die "未知参数 $1" ;;
  esac
done
[[ -n "$MODE" ]] || die "需要 --remote <remote>:<path> 或 --local <本端领地目录>"

# ---- 保留名校验（D16 一句话机制；注册侧同律）-------------------------------
if [[ -f "$RESERVED_NAMES" ]]; then
  while IFS= read -r name; do
    [[ -z "$name" || "$name" == \#* ]] && continue
    if [[ "${DEVICE,,}" == "${name,,}" ]]; then
      die "设备名 '$DEVICE' 命中保留名清单（reserved_names.txt），注册/锚定拒绝——请改名"
    fi
  done < "$RESERVED_NAMES"
fi

# ---- 目标目录与挂载 --------------------------------------------------------
MOUNT_BASE="$(cygpath -u "${USERPROFILE:?}")/agentos/mounts"
MOUNT_DIR="${MOUNT_BASE}/${DEVICE}"

if [[ "$MODE" == "remote" ]]; then
  check_rclone || die "先装 rclone（版本 ${RCLUBE_PINNED}）再 --apply"
  # 凭据第三面（D19）：rclone 配置只落本端用户空间（%APPDATA%\rclone\rclone.conf），
  # SSH 密钥以文件路径引用，永不入 manifest/注册表/同步面。首次配置：
  #   rclone config create <remote名> sftp host 162.14.69.153 user ubuntu \
  #     key_file ~/.ssh/<专用密钥>
  plan "mkdir -p '$MOUNT_DIR'（WinFsp 目录挂载点要求：NTFS 本地盘空目录）"
  run mkdir -p "$MOUNT_DIR"
  # WinFsp 目录挂载点形态（禁散落盘符——盘符只可作 junction 之下的实现细节）
  # 断链语义（D20）：隧道断/属主离线 → 进程拿 ENOENT/EIO/IO timeout（诚实不可用）
  run rclone mount "$SOURCE" "$(winpath "$MOUNT_DIR")" \
    --vfs-cache-mode writes $( [[ "$READONLY" == "1" ]] && echo --read-only ) \
    --dir-cache-time 10s
  # 常驻面：本命令为前台常驻进程；开机常驻用任务计划程序（ONSTART，最高权限，
  # 同参数）登记，或待装机包把本配方收进 electron 主进程启动链（部署配方裁量）。
elif [[ "$MODE" == "local" ]]; then
  [[ -d "$SOURCE" ]] || die "本端领地目录不存在：$SOURCE"
  MOUNT_DIR="$(cygpath -u "$SOURCE")"   # 本端前缀 = junction 直指真实领地（零网络）
fi

# ---- 两级 junction 锚（D16；需管理员 Git Bash 执行 --apply）----------------
GIT_ROOT_WIN="$(winpath /)"; GIT_ROOT_WIN="${GIT_ROOT_WIN%\\}"   # MSYS / = Git 安装根，去尾反斜杠
JUNCTIONS=(
  "${GIT_ROOT_WIN}\\${DEVICE}"                 # ① bash 内 /<设备名>/... 逐字读写（验收面）
  "C:\\${DEVICE}"                              # ② 盘根锚（OS 层，agent 面不感知）
)
for j in "${JUNCTIONS[@]}"; do
  if [[ -e "$(cygpath -u "$j" 2>/dev/null || echo "$j")" ]]; then
    echo "[desktop_setup] 锚点已存在，跳过: $j"
    continue
  fi
  plan "mklink /J '$j' -> '$(winpath "$MOUNT_DIR")'"
  run env MSYS_NO_PATHCONV=1 cmd /c "mklink /J \"$(winpath "$j" 2>/dev/null || echo "$j")\" \"$(winpath "$MOUNT_DIR")\""
done

echo "[desktop_setup] 完成。bash 验收（D16 验收口径）："
echo "  echo test > /${DEVICE}/多端验收文件 && cat /${DEVICE}/多端验收文件"
echo "  # Windows 原生进程走 OS 层锚：C:\\${DEVICE}\\（agent 面不感知，D16 分界）"
