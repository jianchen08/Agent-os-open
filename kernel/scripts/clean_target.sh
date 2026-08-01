#!/usr/bin/env bash
# ── Cargo target 目录瘦身脚本 ─────────────────────────────────────────
#
# 解决问题：Cargo 原生【不会】自动清理旧编译产物。每次 Cargo.lock 变化、
#   切分支、改 feature，都会在 target/debug/deps 留下旧的 .rlib/.pdb/.rmeta，
#   长期堆积（本项目曾涨到 18G，其中 deps 占 15G）。
#
# 本脚本用 cargo-sweep 删除"当前 Cargo.lock 不再引用的历史孤儿产物"，
# 保留最近一次有效编译的产物，不影响调试能力。
#
# 依赖：cargo-sweep（未安装时脚本会提示安装命令）。
#
# 用法：
#   ./scripts/clean_target.sh           # 默认 dry-run（只预览，不删）
#   ./scripts/clean_target.sh --apply   # 真实清理
#   ./scripts/clean_target.sh --help
#
# 机制说明：
#   - --time N  ：删除 N 天前的产物。默认 7，保守且安全。
#   - --all     ：基于 Cargo.lock 只保留当前需要的（更激进，清完等于半个 cargo clean）。
#   推荐日常用 --time 7；大版本升级后用 --all 彻底清一次。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_DIR="$KERNEL_DIR/target"

DAYS=7
APPLY=0
MODE="time"

usage() {
  cat <<EOF
Cargo target 瘦身脚本（清理历史孤儿编译产物）

用法:
  $0 [选项]

选项:
  --apply        真实清理（默认仅 dry-run 预览）
  --time DAYS    删除 N 天前的产物（默认 7）
  --all          基于 Cargo.lock 只保留当前需要的（激进，彻底清旧产物）
  --help, -h     显示帮助

示例:
  $0                      # 预览 7 天前的可清理量
  $0 --apply              # 清理 7 天前的产物
  $0 --apply --time 0     # 清理今天之前的所有产物
  $0 --apply --all        # 只保留当前 Cargo.lock 需要的产物
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)  APPLY=1; shift ;;
    --time)   DAYS="$2"; shift 2 ;;
    --all)    MODE="all"; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

# ── 前置检查 ──────────────────────────────────────────────────────────
if ! command -v cargo-sweep >/dev/null 2>&1; then
  echo "错误：未安装 cargo-sweep。请先执行："
  echo "  cargo install cargo-sweep"
  exit 1
fi

if [[ ! -d "$TARGET_DIR" ]]; then
  echo "target 目录不存在（尚未编译过），无需清理: $TARGET_DIR"
  exit 0
fi

# ── 显示清理前体积 ────────────────────────────────────────────────────
echo "Kernel 目录: $KERNEL_DIR"
echo "清理前 target 体积:"
du -sh "$TARGET_DIR" 2>/dev/null || true
echo

SWEEP_ARGS=()
if [[ "$MODE" == "all" ]]; then
  SWEEP_ARGS+=(--all)
  echo "模式: 基于 Cargo.lock 只保留当前需要的产物（激进）"
else
  SWEEP_ARGS+=(--time "$DAYS")
  echo "模式: 删除 ${DAYS} 天前的产物（保守）"
fi

if [[ "$APPLY" -eq 0 ]]; then
  SWEEP_ARGS=(--dry-run "${SWEEP_ARGS[@]}")
  echo "状态: 【dry-run 预览，不会删除】（加 --apply 真实清理）"
else
  echo "状态: 【真实清理】"
fi
echo

# ── 执行 ──────────────────────────────────────────────────────────────
cd "$KERNEL_DIR"
cargo sweep "${SWEEP_ARGS[@]}" .

echo
echo "清理后 target 体积:"
du -sh "$TARGET_DIR" 2>/dev/null || true
