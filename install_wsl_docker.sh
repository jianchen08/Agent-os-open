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
    exit 100  # 特殊码：需要外层 wsl --shutdown 后重跑
fi

# ── 2. 装 docker-ce ──
step "2/5 安装 docker-ce"
if command -v dockerd &>/dev/null; then
    ok "docker-ce 已安装: $(dockerd --version | awk '{print $3}')"
else
    info "配置 apt 源(阿里云镜像)..."
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg lsb-release
    DOCKER_REPO="https://mirrors.aliyun.com/docker-ce/linux/ubuntu"
    install -m 0755 -d /etc/apt/keyrings
    if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL "${DOCKER_REPO}/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
    fi
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${DOCKER_REPO} $(lsb_release -cs) stable" \
        | tee /etc/apt/sources.list.d/docker.list > /dev/null
    apt-get update -y
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
exit 0
