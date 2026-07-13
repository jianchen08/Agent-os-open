#!/usr/bin/env bash
# WSL2 原生 docker 一键安装脚本(幂等，可重复运行)
#
# 目标：在 WSL2 Ubuntu 装原生 docker-ce，替代 Docker Desktop。
#      干掉 com.docker.backend 这个卡死元凶进程。
#
# 本脚本幂等：已装的步骤会跳过，可安全重复运行。
# 外层 Windows 的 .bat 会调起本脚本，用户无需手动敲命令。
#
# 自动完成:
#   1. 开 systemd(容器服务管理)
#   2. 装 docker-ce + compose 插件(阿里云镜像加速)
#   3. 配 docker 监听 TCP(localhost:2375,供 Windows Agent 连)
#   4. 当前用户加入 docker 组
#   5. 启动 + 验证

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${CYAN}==== $* ====${NC}"; }

# ── 0. 彻底清理上次失败/Docker Desktop 残留(核武器级) ──
# 上次安装失败可能留下 malformed 的 apt 源、空的 GPG key、masked 的 service。
# Docker Desktop 卸载后也可能残留 docker apt 源和 service 文件。
# 这些残留会导致 apt-get update 报 Malformed entry 或 systemctl enable 失败。
# 本脚本是幂等的,下面会重新生成正确配置,删除残留是安全的。
info "清理 docker/apt 相关残留..."
# 删除所有 docker 相关的 apt 源文件(无论是否损坏)
rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker-ce.list 2>/dev/null || true
# 删除旧的 docker GPG key(可能为空/损坏)
rm -f /etc/apt/keyrings/docker.gpg /etc/apt/keyrings/docker-ce.gpg 2>/dev/null || true
# 删除 Docker Desktop 残留的 apt 源
rm -f /etc/apt/sources.list.d/docker-desktop.list 2>/dev/null || true
# unmask docker.service(Docker Desktop 残留会 mask 它)
systemctl unmask docker.service 2>/dev/null || true
systemctl unmask containerd.service 2>/dev/null || true
# 禁用 command-not-found post-invoke 钩子(调 python3,环境损坏会崩溃中断 apt)
if [ -f /etc/apt/apt.conf.d/50command-not-found ]; then
    mv /etc/apt/apt.conf.d/50command-not-found /etc/apt/apt.conf.d/50command-not-found.bak 2>/dev/null || true
fi
ok "残留清理完成"

# ── 1. 开 systemd ──
step "1/5 开启 systemd"
if [ "$(ps -p 1 -o comm=)" = "systemd" ]; then
    ok "systemd 已在运行"
else
    info "写入 /etc/wsl.conf 开启 systemd..."
    tee /etc/wsl.conf > /dev/null << 'EOF'
[boot]
systemd=true
EOF
    echo "NEED_WSL_RESTART"
    echo "NEED_WSL_RESTART" > /tmp/wsl_docker_restart.marker
    exit 100  # 特殊码：需要外层 wsl --shutdown 后重跑
fi

# ── 1.5 网络预检(WSL2 NAT 模式下 DNS 偶发不通,提前发现避免白跑 9 次 curl) ──
info "检查 WSL 网络连通性..."
NET_OK=0
for host in "mirrors.aliyun.com" "mirrors.tuna.tsinghua.edu.cn" "download.docker.com"; do
    if getent hosts "$host" >/dev/null 2>&1 || curl -fsSL --connect-timeout 5 --max-time 8 -o /dev/null "https://${host}" 2>/dev/null; then
        ok "网络正常(可解析 ${host})"
        NET_OK=1
        break
    fi
done
if [ "$NET_OK" = "0" ]; then
    err "WSL 网络不通: 无法解析任何镜像源(aliyun/清华/docker 官方)。"
    err "这通常是 WSL2 NAT 模式下的 DNS 问题,不是脚本 bug。"
    err ""
    err "解决方法(任选其一):"
    err "  1. 在 Windows 执行 wsl --shutdown,等 10 秒后重跑本脚本"
    err "  2. 检查 Windows 是否开了 VPN/代理(WSL2 NAT 模式不走 Windows 代理)"
    err "  3. 手动设 WSL DNS: sudo bash -c 'echo nameserver 8.8.8.8 > /etc/resolv.conf'"
    err "  4. 在 %USERPROFILE%\.wslconfig 加 [wsl2] networkingMode=mirrored"
    exit 1
fi

# ── 2. 装 docker-ce ──
step "2/5 安装 docker-ce"
if command -v dockerd &>/dev/null; then
    ok "docker-ce 已安装: $(dockerd --version | awk '{print $3}')"
else
    info "配置 apt 源(阿里云镜像)..."
    # apt-get update 的 post-invoke 钩子可能因 Python 环境损坏而失败,
    # 但这不影响包索引本身已更新。用 || warn 容忍,继续往下装。
    apt-get update -y || warn "apt-get update 有警告(post-invoke 钩子失败属正常,继续)"
    apt-get install -y ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    # docker apt 源的镜像,与 GPG key 用同一个成功源(避免 aliyun DNS 不通时
    # GPG key 从清华下了,apt 源还写死 aliyun 导致包索引下载失败)。
    DOCKER_MIRRORS="https://mirrors.aliyun.com https://mirrors.tuna.tsinghua.edu.cn https://download.docker.com"
    DOCKER_REPO=""
    # 校验 GPG key 存在且非空(上次下载失败会留空/损坏文件,不能跳过重下)。
    NEED_KEY=1
    if [ -s /etc/apt/keyrings/docker.gpg ]; then
        if gpg --quiet --show-keys /etc/apt/keyrings/docker.gpg >/dev/null 2>&1; then
            NEED_KEY=0
            ok "docker GPG key 已存在且有效"
        else
            warn "docker.gpg 存在但无效(上次下载可能失败),重新下载..."
            rm -f /etc/apt/keyrings/docker.gpg
        fi
    fi
    if [ "$NEED_KEY" = "1" ]; then
        # 下载 docker GPG key,带重试 + 多镜像源回退(国内网络偶发 DNS 不通)。
        # 记录成功的镜像源,后面 apt 源用同一个(保证 GPG key 和包索引同源)。
        GPG_KEY=""
        for attempt in 1 2 3; do
            for mirror in $DOCKER_MIRRORS; do
                info "下载 docker GPG key (尝试 $attempt, 源 ${mirror})..."
                if curl -fsSL --connect-timeout 15 --max-time 30 "${mirror}/docker-ce/linux/ubuntu/gpg" -o /tmp/docker-gpg-key; then
                    GPG_KEY=/tmp/docker-gpg-key
                    DOCKER_REPO="${mirror}/docker-ce/linux/ubuntu"
                    break 2
                fi
                warn "下载失败(${mirror}),尝试下一个源..."
            done
            sleep 3
        done
        if [ -z "$GPG_KEY" ]; then
            err "无法下载 docker GPG key(所有镜像源均失败)。请检查网络后重试。"
            exit 1
        fi
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg < "$GPG_KEY"
        chmod a+r /etc/apt/keyrings/docker.gpg
        rm -f /tmp/docker-gpg-key
        ok "docker GPG key 已配置 (源: ${DOCKER_REPO})"
    fi
    # GPG key 已存在(NEED_KEY=0)时,DOCKER_REPO 还是空,需要探测可用的镜像源。
    if [ -z "$DOCKER_REPO" ]; then
        for mirror in $DOCKER_MIRRORS; do
            if curl -fsSL --connect-timeout 10 --max-time 15 -o /dev/null "${mirror}/docker-ce/linux/ubuntu/dists/focal/Release" 2>/dev/null; then
                DOCKER_REPO="${mirror}/docker-ce/linux/ubuntu"
                break
            fi
        done
        if [ -z "$DOCKER_REPO" ]; then
            err "无法连接任何 docker 镜像源(aliyun/清华/官方均不通)。请检查网络后重试。"
            exit 1
        fi
        ok "docker apt 源选用: ${DOCKER_REPO}"
    fi
    # 写 docker apt 源(先清旧的可能损坏的文件,避免 Malformed entry)。
    rm -f /etc/apt/sources.list.d/docker.list
    # codename 从 /etc/os-release 读(不依赖 lsb_release,后者可能刚装好路径未更新)。
    CODENAME=""
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        CODENAME="${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
    fi
    # lsb_release 作为回退
    if [ -z "$CODENAME" ] && command -v lsb_release >/dev/null 2>&1; then
        CODENAME="$(lsb_release -cs 2>/dev/null)"
    fi
    if [ -z "$CODENAME" ]; then
        err "无法确定 Ubuntu 代号(codename),/etc/os-release 和 lsb_release 均失败。"
        err "请手动设置 /etc/apt/sources.list.d/docker.list"
        exit 1
    fi
    ARCH="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
    echo "deb [arch=${ARCH} signed-by=/etc/apt/keyrings/docker.gpg] ${DOCKER_REPO} ${CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    ok "docker apt 源: ${CODENAME} (${ARCH})"
    # 校验写出的文件格式正确(非空 + 含 deb 开头)。
    if ! head -1 /etc/apt/sources.list.d/docker.list | grep -q '^deb '; then
        err "docker.list 写入异常,内容: $(cat /etc/apt/sources.list.d/docker.list)"
        exit 1
    fi
    apt-get update -y || warn "apt-get update 有警告(post-invoke 钩子失败属正常,继续)"
    info "安装 docker-ce..."
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ok "docker-ce 安装完成"
fi

# ── 3. 配置 docker 监听 TCP(关键:供 Windows Agent 连) ──
step "3/5 配置 docker TCP 监听(localhost:2375)"
# daemon.json 配置：开 TCP 监听，localhost:2375(镜像网络模式下 Windows 可直连)
DAEMON_JSON="/etc/docker/daemon.json"
# 国内镜像加速 + TCP 监听 + 日志限制(防日志膨胀)
mkdir -p /etc/docker
tee "$DAEMON_JSON" > /dev/null << 'EOF'
{
  "hosts": ["unix:///var/run/docker.sock", "tcp://0.0.0.0:2375"],
  "registry-mirrors": [
    "https://docker.m.daocloud.io",
    "https://mirror.ccs.tencentyun.com"
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
# 覆盖 systemd 的默认 -H fd://（否则 hosts 冲突 daemon 起不来）
mkdir -p /etc/systemd/system/docker.service.d
tee /etc/systemd/system/docker.service.d/override.conf > /dev/null << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/dockerd
EOF
systemctl daemon-reload
ok "daemon.json 已配置(TCP:2375 + 镜像加速)"

# ── 4. docker 组 + 启动 ──
step "4/5 启动 docker 服务"
# 把真实的普通用户(UID 1000)加入 docker 组,root 跑时 $USER 不准
REAL_USER=$(awk -F: '$3==1000{print $1}' /etc/passwd)
if [ -n "$REAL_USER" ]; then
    if ! groups "$REAL_USER" | grep -q docker; then
        usermod -aG docker "$REAL_USER" || true
        info "用户 $REAL_USER 已加入 docker 组"
    else
        info "用户 $REAL_USER 已在 docker 组"
    fi
fi
# Docker Desktop 残留会把 docker.service masked(屏蔽),导致 systemctl enable
# 失败("Unit file ... is masked")。unmask 后才能正常启用。
if systemctl is-enabled docker 2>/dev/null | grep -q masked; then
    warn "docker.service 被 masked(通常是 Docker Desktop 残留),执行 unmask..."
    systemctl unmask docker
fi
systemctl enable docker
systemctl restart docker
sleep 2
if systemctl is-active --quiet docker; then
    ok "docker 服务运行中"
else
    err "docker 服务启动失败"
    systemctl status docker --no-pager | tail -15
    exit 1
fi

# ── 5. 验证 ──
step "5/5 验证"
info "docker 版本:"
docker version --format '  Server: {{.Server.Version}}'

info "TCP 监听(确认 2375 开了):"
if ss -tlnp 2>/dev/null | grep -q ":2375"; then
    ok "TCP 2375 正在监听"
else
    warn "TCP 2375 未监听(Windows 可能连不上)"
fi

info "挂载测试(关键:验证能访问 Windows 项目):"
# 用脚本自身所在目录动态推导项目路径（本脚本位于项目根），不写死挂载点。
PROJ="$(cd "$(dirname "$0")" && pwd)"
if docker run --rm -v "${PROJ}:/workspace" alpine sh -c "test -f /workspace/install_wsl_docker.sh && echo MOUNT_OK" 2>/dev/null | grep -q MOUNT_OK; then
    ok "挂载成功，容器能访问 Windows 项目"
else
    warn "挂载测试未通过(项目路径可能不同，不影响 docker 本身)"
fi

echo ""
ok "=========================================="
ok " WSL2 docker-ce 安装完成！"
ok "=========================================="
echo "WSL 侧就绪。Windows 侧设 DOCKER_HOST=tcp://localhost:2375 即可连接。"
echo "WSL_DOCKER_READY"
echo "WSL_DOCKER_READY" > /tmp/wsl_docker_ready.marker
exit 0
