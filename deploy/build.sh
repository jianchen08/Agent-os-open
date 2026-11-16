#!/usr/bin/env bash
# AgentOS 容器化编译脚本（Linux 部署产物：内核 + 3 个 cdylib 插件）。
#
# 宿主机无需 Rust 工具链：全部编译在 rust:1.88 容器内完成（版本与
# rust-toolchain.toml 钉版一致）。产物落回仓库布局，供 start_server.sh /
# systemd unit 直接消费：
#   - kernel/target/release/agentos-kernel
#   - plugins/shared/{tool_core,sensitive_checker,spill_guard}/lib*_native.so（拍平布局）
#
# 为什么 cdylib 必须在这里编译：内核经 native-loader dlopen 消费 .so，
# Windows 侧产物（.dll）在 Linux 上不可加载——部署机产物必须是 Linux 侧编译
# 结果；无 Rust 环境的部署机由本脚本经容器补齐（2026-10-09 WSL2/ext4 实测）。
#
# 缓存与幂等：cargo 增量产物与 crates.io 下载缓存放命名卷
# （agentos-deploy-target / agentos-deploy-cargo-home），重复执行只做增量
# 编译；每次 cp 均为整文件覆盖，脚本可安全重入。
#
# 前端静态产物（frontend/dist）不在本脚本范围——需 Node/pnpm，见
# docs/guides/deployment.md「极简部署四步」。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE="rust:1.88"
TARGET_VOLUME="agentos-deploy-target"
CARGO_HOME_VOLUME="agentos-deploy-cargo-home"

command -v docker >/dev/null 2>&1 || {
    echo "[ERROR] docker 不可用：本脚本经容器编译（宿主机无需 Rust）。请先安装并启动 Docker daemon。" >&2
    exit 1
}

# 仓库经 -v 挂进容器 /io，编译产物落命名卷 /target（CARGO_TARGET_DIR），
# 编译完成后由短容器从卷内 cp 回仓库布局。
# 用法：container_run <io 内工作目录> <命令...>——首个参数映射为容器 -w；
# docker 旗标必须在镜像（$IMAGE）之前，调用点只传 io 目录与命令。
# cargo-home 卷只挂 /registry 子目录：整卷盖 /usr/local/cargo 会把镜像
# 自带的 cargo/rustup 工具链一并遮蔽（实测 exec 不可达）。
container_run() {
    local io_dir="$1"
    shift
    docker run --rm \
        -v "$REPO_ROOT":/io \
        -v "$REPO_ROOT/kernel":/kernel \
        -v "$TARGET_VOLUME":/target \
        -v "$CARGO_HOME_VOLUME":/usr/local/cargo/registry \
        -e CARGO_TARGET_DIR=/target \
        -w "/io$io_dir" \
        "$IMAGE" "$@"
}

echo "[build] 内核编译中（rust:1.88 容器，增量缓存卷 $TARGET_VOLUME）..."
container_run /kernel cargo build --release --bin agentos-kernel

mkdir -p "$REPO_ROOT/kernel/target/release"
docker run --rm -v "$TARGET_VOLUME":/target -v "$REPO_ROOT":/io "$IMAGE" \
    sh -c "cp /target/release/agentos-kernel /io/kernel/target/release/agentos-kernel && chmod 755 /io/kernel/target/release/agentos-kernel"
echo "[build] 内核就绪: kernel/target/release/agentos-kernel"

# cdylib 插件：插件目录 | cdylib 包名（产物名 lib<包名下划线>.so，
# 内核 native-loader 按本平台映射解析，见 loader 的 resolve_artifact）。
CDYLIBS=(
    "plugins/shared/tool_core|pipeline-tool-core-native"
    "plugins/shared/sensitive_checker|pipeline-sensitive-checker-native"
    "plugins/shared/spill_guard|pipeline-spill-guard-native"
)

for entry in "${CDYLIBS[@]}"; do
    dir="${entry%%|*}"
    pkg="${entry##*|}"
    so_name="lib${pkg//-/_}.so"
    echo "[build] cdylib 编译中: $dir ($so_name)..."
    container_run "/$dir" cargo build --release
    container_run / cp "/target/release/$so_name" "/io/$dir/$so_name"
    # cp 保持 cargo 的 755/644 权限；属主为容器 root，宿主 chmod 非致命
    chmod 644 "$REPO_ROOT/$dir/$so_name" 2>/dev/null || true
    echo "[build] cdylib 就绪: $dir/$so_name"
done

echo "[build] 全部完成。下一步：bash deploy/init.sh（venv 供给与配置初始化）。"
