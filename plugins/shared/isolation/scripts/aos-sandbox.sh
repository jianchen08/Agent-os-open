#!/usr/bin/env bash
# AgentOS wsl_native 沙箱包装器（bubblewrap profile）。
# 设计真值：docs/decisions/2026-11-10-wsl-native-workspace-only-sandbox.md
#
# 装载形态（isolation_config.yaml 的 providers.wsl_native.sandbox_cmd）：
#   wsl -d <distro> --cd <ws> --exec [env k=v ...] \
#       bash -c 'exec "$HOME/.agentos/bin/aos-sandbox" "$@"' -- bash -c <cmd>
# 本脚本把收到的 argv 放进 bwrap 沙箱后 exec。工作空间取当前工作目录
# （provider 的 --cd 已指到 workspace），因此不需要任何按任务生成的配置。
#
# 沙箱文件系统面：
#   - 发行版根只读（工具链可直接复用）；
#   - /mnt 整体换成空 tmpfs（宿主 Windows 盘 C/D/E/F 全部不可见），
#     工作空间按真实路径挂回可写；
#   - 工具链/依赖缓存目录可写 carve-out（ADR 2026-11-07 工具链持久层）；
#   - 秘密目录与 docker socket 读面遮蔽（挂载收窄挡不住这两条，见 ADR）。
#
# 环境变量（均可选）：
#   AOS_SANDBOX_BWRAP     bwrap 路径；缺省按 PATH → ~/.agentos/bin → /usr/local/bin → /usr/bin
#   AOS_SANDBOX_RW_DIRS   额外可写目录，冒号分隔（叠加在缺省工具链目录之上）
#   AOS_SANDBOX_DENY      额外读面遮蔽路径，冒号分隔（目录按 tmpfs 掩、文件按 /dev/null 掩）
#   AOS_SANDBOX_DRY_RUN   非空：只打印 bwrap argv 不执行（测试与排障用）
#
# 失败语义 fail-closed：bwrap 缺失 / 工作目录不可解析 / 工作目录是根 → exit 126，
# 命令不执行（隔离不可用时不静默落回宿主）。
set -u

_die() {
  echo "aos-sandbox: $*" >&2
  exit 126
}

WS="$(pwd -P 2>/dev/null)" || _die "无法解析当前工作目录"
[ -n "$WS" ] || _die "当前工作目录为空"
[ "$WS" != "/" ] || _die "拒绝在文件系统根启动沙箱（工作目录=/）"

HOME_DIR="${HOME:-/root}"

resolve_bwrap() {
  if [ -n "${AOS_SANDBOX_BWRAP:-}" ]; then
    # 显式指定时必须真的可执行：照抄一个坏路径会让 bash 报 "No such file or
    # directory"，丢掉「是隔离供给缺失」这个信息。
    [ -x "$AOS_SANDBOX_BWRAP" ] || return 1
    printf '%s\n' "$AOS_SANDBOX_BWRAP"
    return 0
  fi
  if command -v bwrap >/dev/null 2>&1; then
    command -v bwrap
    return 0
  fi
  local candidate
  for candidate in "$HOME_DIR/.agentos/bin/bwrap" /usr/local/bin/bwrap /usr/bin/bwrap; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

BWRAP="$(resolve_bwrap)" || _die "未找到可执行的 bubblewrap（bwrap），隔离供给缺失：请先运行 scripts/install_wsl_sandbox.py"

# 工具链与依赖缓存可写目录：发行版 home 即工具链持久层（ADR 2026-11-07），
# 根只读会把 cargo registry / rustup / npm 全局装的写面全堵死。
RW_DIRS="$HOME_DIR/.cargo $HOME_DIR/.rustup $HOME_DIR/.nvm $HOME_DIR/go $HOME_DIR/.local $HOME_DIR/.cache $HOME_DIR/.npm $HOME_DIR/.config $HOME_DIR/.gitconfig"
if [ -n "${AOS_SANDBOX_RW_DIRS:-}" ]; then
  RW_DIRS="$RW_DIRS $(printf '%s' "$AOS_SANDBOX_RW_DIRS" | tr ':' ' ')"
fi

# 读面遮蔽：秘密不该给 agent 读（挂载收窄只解决写面）。
DENY_PATHS="$HOME_DIR/.ssh $HOME_DIR/.aws $HOME_DIR/.gnupg $HOME_DIR/.docker $HOME_DIR/.config/gh $HOME_DIR/.netrc $HOME_DIR/.git-credentials $HOME_DIR/.bash_history"
if [ -n "${AOS_SANDBOX_DENY:-}" ]; then
  DENY_PATHS="$DENY_PATHS $(printf '%s' "$AOS_SANDBOX_DENY" | tr ':' ' ')"
fi

args=(
  "$BWRAP"
  --ro-bind / /
  --proc /proc
  --dev /dev
  --tmpfs /tmp
  --tmpfs /mnt
  --bind "$WS" "$WS"
  --cap-drop ALL
  --die-with-parent
  --chdir "$WS"
)

# 可写 carve-out 必须在 --tmpfs /mnt 之后（/mnt 之外、互不重叠，顺序只对
# 同一路径的遮蔽有意义）。按 readlink -f 的真实路径挂：home 里的目录可能是
# 指进宿主盘的 symlink（实测 ~/.aws -> /mnt/c/...），对 symlink 直接挂会
# 在建挂载点时解析失败。
for dir in $RW_DIRS; do
  [ -e "$dir" ] || continue
  real="$(readlink -f "$dir" 2>/dev/null)" || continue
  [ -n "$real" ] && [ "$real" != "/" ] || continue
  args+=(--bind "$real" "$real")
done

# 读面遮蔽排在可写 carve-out 之后：~/.config/gh 这类嵌套秘密要能盖住
# 上一条挂上去的可写父目录。悬空 symlink（指向已被遮蔽的 /mnt）本来就读不到，
# 跳过即可——强行 tmpfs 会报 "Can't mount tmpfs ... No such file or directory"。
for path in $DENY_PATHS; do
  [ -e "$path" ] || continue
  real="$(readlink -f "$path" 2>/dev/null)" || continue
  [ -n "$real" ] && [ "$real" != "/" ] || continue
  case "$real" in
  /mnt/*) continue ;;
  esac
  if [ -d "$real" ]; then
    args+=(--tmpfs "$real")
  else
    args+=(--ro-bind /dev/null "$real")
  fi
done

# docker daemon 通路：不掩它，挂载收窄形同虚设（agent 可 docker run -v 带盘）。
# 只能掩真身 /run/docker.sock——/var/run 是 symlink，bwrap 建不出挂载点。
if [ -S /run/docker.sock ]; then
  args+=(--ro-bind /dev/null /run/docker.sock)
fi

if [ -n "${AOS_SANDBOX_DRY_RUN:-}" ]; then
  printf '%s\n' "${args[@]}"
  exit 0
fi

exec "${args[@]}" -- "$@"
