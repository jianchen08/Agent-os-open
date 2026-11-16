#!/usr/bin/env bash
# WSL 侧前端测试车道：ext4 内常驻副本跑 vitest。
#
# 为什么：/mnt/d 在 WSL2 里是 9p（DrvFs），元数据操作慢约三个数量级（2000 小文件
# stat：/mnt/d 4.757s vs /tmp 0.007s；vitest import 段在此直跑为 177s）。成熟口径是
# 「Linux 工具的热文件放 ext4，两侧只传 git」——正主留在 D 盘受管工作区不动，本脚本
# 在 WSL home 维护一份常驻 git 副本（~/repos/agentos）：首次克隆一次，之后
# git pull --ff-only 只拉增量，不重复复制；换不同仓库才有各自的一次性克隆。
#
# 依赖安装对齐 CI frontend 车道：pnpm workspace + 根 pnpm-lock.yaml +
# --frozen-lockfile（不同步直接红，禁 fallback），pnpm 版本取根 packageManager 钉。
# node 22 用户级 tarball（pnpm 11 硬要求 ≥22，CI 同 22；免 sudo，一次下载永久复用）。
#
# 用法（Windows 宿主 Git Bash；MSYS_NO_PATHCONV 防止 Git Bash 改写 /mnt 路径）：
#   MSYS_NO_PATHCONV=1 wsl.exe bash /mnt/d/myproject/container_e17cc5927dfd/scripts/run_frontend_tests_wsl.sh
# 参数透传 vitest（如单目录）：
#   MSYS_NO_PATHCONV=1 wsl.exe bash /mnt/d/.../scripts/run_frontend_tests_wsl.sh src/components/chat
# 用法（WSL 内）：bash /mnt/d/.../scripts/run_frontend_tests_wsl.sh
# 配方与决策记录：docs/working/前端WSL测试配方_20261017.md
set -euo pipefail

if [[ "$(uname -s)" != Linux ]]; then
  echo "本脚本须在 Linux/WSL 内运行；Windows 宿主请用: MSYS_NO_PATHCONV=1 wsl.exe bash <本脚本绝对路径>" >&2
  exit 2
fi

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
REPO_SRC=$(dirname "$(dirname "$SCRIPT_PATH")")
DEST="$HOME/repos/agentos"

# —— node 22 用户级工具链（pnpm 11 硬要求；幂等）——
NODE_VER="v22.23.3"
NODE_DIR="$HOME/.local/share/node-dists/node-$NODE_VER-linux-x64"
if [ ! -x "$NODE_DIR/bin/node" ]; then
  mkdir -p "$(dirname "$NODE_DIR")"
  echo "[lane] 引导 node $NODE_VER ..."
  curl -fsSL -o "$(dirname "$NODE_DIR")/node.tar.xz" \
    "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-linux-x64.tar.xz"
  tar -xJf "$(dirname "$NODE_DIR")/node.tar.xz" -C "$(dirname "$NODE_DIR")"
  rm "$(dirname "$NODE_DIR")/node.tar.xz"
fi
export PATH="$NODE_DIR/bin:$PATH"
COREPACK_ENABLE_DOWNLOAD_PROMPT=0 corepack enable >/dev/null 2>&1 || true

# —— 常驻副本：首次一次性克隆（之后永不重复复制，只增量 pull）——
# 三个坑实测（2026-10-17）：①本地路径克隆忽略 --depth（git 警告：--depth is
# ignored in local clones），file:// 前缀才走浅化协商；②--depth 1 单次快照约
# 150MB，但 tags/其他分支的 refs 会把各自完整历史钉成可达——必须 --no-tags +
# --single-branch（实测漏了它们时 .git 膨胀到 2.9-3G，清 refs 后 68M）。
if [ ! -d "$DEST/.git" ]; then
  echo "[lane] 首次克隆 -> $DEST（一次性；单文件 bundle 可加速，见配方文档）"
  time git clone --depth 1 --single-branch --no-tags "file://$REPO_SRC" "$DEST"
fi
cd "$DEST"
git pull --ff-only

# —— 依赖：根 lock 摘要变化或缺失才重装（对齐 CI，禁 fallback 绕过锁定）——
STAMP="$HOME/.cache/agentos-wsl-lane"
mkdir -p "$STAMP"
LOCK_SUM=$(sha256sum pnpm-lock.yaml | cut -d' ' -f1)
if [ ! -d frontend/node_modules ] || [ "$LOCK_SUM" != "$(cat "$STAMP/lock.sha256" 2>/dev/null || true)" ]; then
  echo "[lane] lock 变化/依赖缺失：pnpm install --frozen-lockfile"
  export ELECTRON_SKIP_BINARY_DOWNLOAD=1   # 与 CI electron job 同参：测试不需要 electron 运行时
  time pnpm install --frozen-lockfile
  printf '%s' "$LOCK_SUM" > "$STAMP/lock.sha256"
fi

cd frontend
exec pnpm exec vitest --run "$@"
