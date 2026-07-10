#!/usr/bin/env bash
# Agent OS 一键部署脚本 (Linux / macOS)
#
# 职责:
#   阶段1 bootstrap: 探测发行版 → 装 docker(Linux 原生/macOS Docker Desktop) → 装 Python 依赖
#   阶段2 deploy:    build 镜像 → compose up → 启动 Agent → 健康检查
#
# 用法:
#   chmod +x install.sh
#   ./install.sh            # 完整部署(bootstrap + deploy)
#   ./install.sh --deploy   # 跳过 bootstrap(已装好 docker),直接部署
#
# 设计原则: Linux 用原生 docker engine(无后端之争,最稳定);
#           macOS 用 Docker Desktop(单一后端)。两者都不存在 WSL2 的脆弱性。

set -euo pipefail

# ── 颜色与日志 ────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }
step()  { echo -e "\n${CYAN}==== $* ====${NC}"; }

# 项目根目录(脚本所在目录)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

DEPLOY_ONLY=false
[[ "${1:-}" == "--deploy" ]] && DEPLOY_ONLY=true

# ── OS 探测 ────────────────────────────────────────────────────
detect_os() {
    local os_type
    os_type="$(uname -s)"
    case "$os_type" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "macos" ;;
        *)       echo "unknown" ;;
    esac
}

detect_linux_distro() {
    # /etc/os-release 是 LSB 标准的发行版标识
    if [[ -f /etc/os-release ]]; then
        . /etc/os-release
        echo "${ID:-unknown}"  # 如 ubuntu, debian, centos, rhel, fedora
        return
    fi
    echo "unknown"
}

# ── 包管理器探测 ───────────────────────────────────────────────
detect_pkg_manager() {
    local distro="$1"
    case "$distro" in
        ubuntu|debian|linuxmint|pop)
            echo "apt" ;;
        centos|rhel|rocky|almalinux|fedora|amzn)
            # fedora 用 dnf,旧 centos/rhel 用 yum,新 rocky/almalinux 也有 dnf
            if command -v dnf &>/dev/null; then
                echo "dnf"
            else
                echo "yum"
            fi ;;
        *)
            echo "unknown" ;;
    esac
}

# ── 安装 docker ────────────────────────────────────────────────
install_docker_linux() {
    local distro pkg_manager
    distro="$(detect_linux_distro)"
    pkg_manager="$(detect_pkg_manager "$distro")"

    info "发行版: $distro, 包管理器: $pkg_manager"

    case "$pkg_manager" in
        apt)
            info "通过 apt 安装 docker..."
            sudo apt-get update -y
            sudo apt-get install -y docker.io docker-compose-plugin
            ;;
        dnf|yum)
            info "通过 $pkg_manager 安装 docker..."
            sudo $pkg_manager install -y docker docker-compose-plugin
            ;;
        *)
            err "不支持的发行版: $distro"
            err "请手动安装 docker: https://docs.docker.com/engine/install/"
            return 1
            ;;
    esac

    # 启用 docker 服务 + 加入用户组(免 sudo 调 docker)
    sudo systemctl enable docker
    sudo systemctl start docker
    if [[ -n "${USER:-}" ]]; then
        sudo usermod -aG docker "$USER" || true
        warn "已将用户 $USER 加入 docker 组,需要注销重登后才能免 sudo 使用 docker"
    fi
    ok "docker 安装完成"
}

install_docker_macos() {
    if ! command -v brew &>/dev/null; then
        err "未找到 Homebrew,请先安装: https://brew.sh"
        return 1
    fi
    info "通过 Homebrew 安装 Docker Desktop..."
    brew install --cask docker
    warn "Docker Desktop 安装完成,需要手动启动一次 Docker.app 完成初始化"
    warn "启动后重新运行: ./install.sh --deploy"
    return 3010  # 特殊码:需要用户手动启动 Docker.app
}

check_docker_ready() {
    # 检查 docker daemon 是否就绪(带超时,防假死)
    if ! command -v docker &>/dev/null; then
        return 1
    fi
    # docker info 在 daemon 未启动时会失败,不是永久阻塞(Linux 上如此)
    if timeout 30 docker info &>/dev/null; then
        return 0
    fi
    return 1
}

# ── 安装 Python 依赖 ───────────────────────────────────────────
install_python_deps() {
    # 项目要求 Python >=3.10
    local py
    for candidate in python3 python3.12 python3.11 python3.10; do
        if command -v "$candidate" &>/dev/null; then
            py="$candidate"
            break
        fi
    done

    if [[ -z "${py:-}" ]]; then
        err "未找到 Python 3.10+,请先安装"
        err "  Ubuntu/Debian: sudo apt install python3 python3-pip python3-venv"
        err "  CentOS/RHEL:   sudo dnf install python3 python3-pip"
        return 1
    fi

    local version
    version="$($py --version 2>&1 | awk '{print $2}')"
    ok "Python: $version ($py)"

    # 创建虚拟环境(避免污染系统 Python)
    if [[ ! -d ".venv" ]]; then
        info "创建虚拟环境 .venv..."
        $py -m venv .venv
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate

    info "安装 Python 依赖..."
    pip install --upgrade pip -q
    if [[ -f "requirements.txt" ]]; then
        pip install -r requirements.txt -q
    elif [[ -f "pyproject.toml" ]]; then
        pip install -e . -q
    fi
    ok "Python 依赖安装完成"
}

# ── 构建镜像 + 启动服务 ────────────────────────────────────────
deploy_services() {
    step "构建镜像"

    # cua 容器镜像(隔离执行用)
    if [[ -f "docker/agentos/Dockerfile" ]]; then
        info "构建 agentos 镜像(隔离执行运行时)..."
        docker build -t agentos:latest -f docker/agentos/Dockerfile . || {
            err "agentos 镜像构建失败"
            return 1
        }
        ok "agentos 镜像就绪"
    fi

    # 前端 + Redis(compose)
    if [[ -f "docker-compose.yml" ]]; then
        info "构建并启动 compose 服务(frontend + redis)..."
        docker compose up -d --build || {
            err "docker compose 启动失败"
            return 1
        }
        ok "compose 服务已启动"
    fi
}

start_agent() {
    step "启动 Agent"
    # Agent 跑在宿主机,连接本地 redis
    if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -d ".venv" ]]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate
    fi

    export PYTHONPATH=src
    export REDIS_URL="${REDIS_URL:-redis://localhost:6480/0}"

    info "启动 Agent 后端(后台运行)..."
    info "  后端: http://localhost:8988"
    info "  前端: http://localhost:5289"
    info "  日志: agentos.log"
    info "  停止: kill \$(cat agentos.pid) && docker compose down"

    # 后台启动,记录 PID
    nohup python -m channels.websocket.app_factory > agentos.log 2>&1 &
    echo $! > agentos.pid
    ok "Agent 已启动 (PID: $(cat agentos.pid))"
}

health_check() {
    step "健康检查"
    # 等待服务就绪
    info "等待服务启动(最多 30 秒)..."
    local i
    for i in $(seq 1 30); do
        if curl -sf http://localhost:5289 >/dev/null 2>&1; then
            ok "前端服务就绪"
            break
        fi
        sleep 1
        [[ $i -eq 30 ]] && warn "前端服务未在 30 秒内就绪,请检查 agentos.log"
    done

    if docker compose ps -q redis >/dev/null 2>&1; then
        ok "Redis 服务就绪"
    else
        warn "Redis 容器未运行"
    fi

    echo ""
    ok "部署完成!"
    echo ""
    echo "  后端: http://localhost:8988"
    echo "  前端: http://localhost:5289"
    echo "  停止: kill \$(cat agentos.pid) && docker compose down"
}

# ── 主流程 ──────────────────────────────────────────────────────
main() {
    echo ""
    echo "========================================"
    echo "   Agent OS 一键部署"
    echo "========================================"
    echo "项目目录: $ROOT"
    echo ""

    local os_type
    os_type="$(detect_os)"

    if [[ "$os_type" == "unknown" ]]; then
        err "不支持的操作系统: $(uname -s)"
        err "支持: Linux, macOS"
        err "Windows 请使用 install_cn.bat"
        exit 1
    fi

    info "操作系统: $os_type"

    # ── 阶段 1: bootstrap ──
    if [[ "$DEPLOY_ONLY" == false ]]; then
        step "阶段 1/2: 安装环境 (bootstrap)"

        # 1.1 docker
        info "[1/3] 检查 Docker..."
        if check_docker_ready; then
            ok "Docker 已就绪"
        else
            case "$os_type" in
                linux)  install_docker_linux ;;
                macos)
                    install_docker_macos
                    rc=$?
                    if [[ $rc -eq 3010 ]]; then
                        warn "请启动 Docker.app 后重新运行: ./install.sh --deploy"
                        exit 0
                    fi
                    ;;
            esac
            # 装完 docker 可能需要重新登录才能免 sudo,这里用 sudo 兜底检查
            if ! sudo docker info &>/dev/null 2>&1; then
                err "docker 安装后仍未就绪,请检查服务状态"
                exit 1
            fi
            ok "Docker 已就绪"
        fi

        # 1.2 Python 依赖
        info "[2/3] 检查 Python 环境..."
        install_python_deps

        # 1.3 构建镜像
        info "[3/3] 镜像准备在阶段 2 进行"
    fi

    # ── 阶段 2: deploy ──
    step "阶段 2/2: 部署服务 (deploy)"

    if ! check_docker_ready; then
        err "Docker 未就绪,无法部署"
        if [[ "$os_type" == "linux" ]]; then
            err "请检查: sudo systemctl status docker"
        fi
        exit 1
    fi

    deploy_services || exit 1
    start_agent
    health_check
}

main "$@"
