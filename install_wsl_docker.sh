#!/usr/bin/env bash
# WSL2 Ubuntu 装原生 docker engine 脚本
#
# 作用：卸掉对 Docker Desktop 的依赖，在 WSL2 里装原生 docker-ce。
#       干掉 com.docker.backend 这个卡死元凶进程。
#
# 用法(在 WSL2 Ubuntu 终端里执行，需要能输入 sudo 密码):
#   wsl -d Ubuntu
#   cd /mnt/d/myproject/container_224042d3b925
#   bash install_wsl_docker.sh
#
# 脚本做的事:
#   1. 开启 systemd(容器服务管理需要，改 /etc/wsl.conf)
#   2. 加 Docker 官方 apt 源(国内镜像加速)
#   3. 装 docker-ce + docker-compose-plugin
#   4. 当前用户加入 docker 组(免 sudo 调 docker)
#   5. 启动 docker 服务
#   6. 验证 docker 能用 + 能挂载 /mnt/d
#
# 注意：开 systemd 后需要重启 Ubuntu(wsl --shutdown)才生效。
#       脚本会在需要重启时提示，重启后重新跑本脚本即可。

set -e

# ── 颜色 ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${CYAN}==== $* ====${NC}"; }

# ── 步骤 1：开启 systemd ──
step "1/6 开启 systemd"
if [ "$(ps -p 1 -o comm=)" = "systemd" ]; then
    ok "systemd 已在运行"
else
    info "systemd 未运行，写入 /etc/wsl.conf ..."
    sudo tee /etc/wsl.conf > /dev/null << 'EOF'
[boot]
systemd=true
EOF
    warn "systemd 配置已写入，但需要重启 Ubuntu 才生效。"
    warn "请在 Windows 执行: wsl --shutdown"
    warn "然后重新打开 Ubuntu，再次运行: bash install_wsl_docker.sh"
    warn "脚本会从步骤 2 继续。"
    exit 0
fi

# ── 步骤 2：加 Docker apt 源 ──
step "2/6 配置 Docker apt 源(国内镜像加速)"
# 先装依赖
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg lsb-release

# 用阿里云镜像加速 Docker 官方源(国内拉得快)
DOCKER_REPO="https://mirrors.aliyun.com/docker-ce/linux/ubuntu"
sudo install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    info "添加 Docker GPG key (阿里云镜像)..."
    curl -fsSL "${DOCKER_REPO}/gpg" | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
fi

# 加源
SOURCE_CONTENT="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] ${DOCKER_REPO} $(lsb_release -cs) stable"
echo "$SOURCE_CONTENT" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -y
ok "Docker 源已配置"

# ── 步骤 3：装 docker-ce ──
step "3/6 安装 docker-ce + compose 插件"
if command -v dockerd &>/dev/null; then
    ok "docker-ce 已安装: $(dockerd --version)"
else
    info "安装 docker-ce(可能需要几分钟下载)..."
    sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ok "docker-ce 安装完成: $(dockerd --version)"
fi

# ── 步骤 4：当前用户加入 docker 组 ──
step "4/6 配置免 sudo docker"
if ! groups | grep -q docker; then
    sudo usermod -aG docker "$USER"
    warn "已将 $USER 加入 docker 组，需重新登录 WSL 后生效(或用 newgrp docker)"
else
    ok "$USER 已在 docker 组"
fi

# ── 步骤 5：启动 docker 服务(systemd)──
step "5/6 启动 docker 服务"
sudo systemctl enable docker
sudo systemctl start docker
sleep 2

if sudo systemctl is-active --quiet docker; then
    ok "docker 服务已启动"
else
    err "docker 服务启动失败，查看: sudo systemctl status docker"
    exit 1
fi

# ── 步骤 6：验证 ──
step "6/6 验证 docker 能用 + 挂载 /mnt/d"

info "docker 版本:"
sudo docker version --format '{{.Server.Version}}'

info "测试 hello-world(验证 daemon 正常):"
if sudo docker run --rm hello-world 2>&1 | grep -q "Hello from Docker"; then
    ok "docker daemon 正常工作"
else
    warn "hello-world 测试未通过(可能是网络拉取慢，不影响 docker 本身)"
fi

info "测试挂载 /mnt/d(关键:验证能访问 Windows 项目):"
TEST_FILE="/mnt/d/myproject/container_224042d3b925/install_wsl_docker.sh"
if [ -f "$TEST_FILE" ]; then
    sudo docker run --rm -v "/mnt/d/myproject/container_224042d3b925:/workspace" alpine \
        sh -c "ls /workspace/install_wsl_docker.sh && echo 'MOUNT_OK'" 2>&1 | grep MOUNT_OK \
        && ok "挂载 /mnt/d 成功，容器能访问 Windows 项目" \
        || err "挂载测试失败"
else
    warn "项目路径不存在或脚本未在该路径，跳过挂载测试"
fi

echo ""
ok "=========================================="
ok " docker-ce 安装完成！"
ok "=========================================="
echo ""
echo "下一步:"
echo "  1. 在 Windows 设 DOCKER_HOST 环境变量连到这里(见文档)"
echo "  2. 卸载 Docker Desktop(控制面板卸载)"
echo "  3. 重启 WSL2 让 docker 组生效: wsl --shutdown 后重开 Ubuntu"
echo ""
warn "注意: 卸载 Docker Desktop 前，先确认本脚本的验证全部通过。"
