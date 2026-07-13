#!/usr/bin/env bash
# =============================================================================
# AgentOS 0.2 — Docker 镜像验证脚本
# 在 builder 镜像内运行，验证所有 AC（验收标准）
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }

echo ""
echo "========================================"
echo "  AgentOS 0.2 Docker 环境验证"
echo "========================================"
echo ""

# ── AC-01-1: 开发套件完整性 ──
info "AC-01-1: 验证开发套件完整性"

# Rust 版本 ≥ 1.85
RUST_VER=$(rustc --version | grep -oP '\d+\.\d+\.\d+')
RUST_MAJOR=$(echo "$RUST_VER" | cut -d. -f1)
RUST_MINOR=$(echo "$RUST_VER" | cut -d. -f2)
if [ "$RUST_MAJOR" -gt 1 ] || { [ "$RUST_MAJOR" -eq 1 ] && [ "$RUST_MINOR" -ge 85 ]; }; then
    pass "Rust 版本: ${RUST_VER} (≥ 1.85)"
else
    fail "Rust 版本过低: ${RUST_VER} (需要 ≥ 1.85)"
fi

# rustfmt + clippy
cargo fmt --version > /dev/null 2>&1 && pass "rustfmt 可用" || fail "rustfmt 缺失"
cargo clippy --version > /dev/null 2>&1 && pass "clippy 可用" || fail "clippy 缺失"

# Python ≥ 3.11
PYTHON_VER=$(python3 --version 2>&1 | grep -oP '\d+\.\d+\.\d+')
PYTHON_MAJOR=$(echo "$PYTHON_VER" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VER" | cut -d. -f2)
if [ "$PYTHON_MAJOR" -gt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -ge 11 ]; }; then
    pass "Python 版本: ${PYTHON_VER} (≥ 3.11)"
else
    fail "Python 版本过低: ${PYTHON_VER} (需要 ≥ 3.11)"
fi

# Node.js ≥ 20
NODE_VER=$(node --version | grep -oP '\d+\.\d+\.\d+')
NODE_MAJOR=$(echo "$NODE_VER" | cut -d. -f1)
if [ "$NODE_MAJOR" -ge 20 ]; then
    pass "Node.js 版本: ${NODE_VER} (≥ 20)"
else
    fail "Node.js 版本过低: ${NODE_VER} (需要 ≥ 20)"
fi

# npm 可用
npm --version > /dev/null 2>&1 && pass "npm 可用" || fail "npm 缺失"

# 基础开发工具
for tool in git curl wget gcc make pkg-config openssl; do
    command -v "$tool" > /dev/null 2>&1 && pass "$tool 可用" || fail "$tool 缺失"
done

echo ""

# ── AC-01-2: Rust workspace 编译验证 ──
info "AC-01-2: 验证 Rust workspace 编译 (cargo build --release)"
cd /build/kernel
cargo build --release 2>&1
pass "cargo build --release 成功"

# 运行编译后的二进制验证
./target/release/kernel-api 2>&1
pass "内核二进制可执行"

echo ""

# ── AC-01-3: Python venv 验证 ──
info "AC-01-3: 验证 Python venv 创建 + pip install"
python3 -m venv /tmp/venv-verify
/tmp/venv-verify/bin/pip install --no-cache-dir pyyaml 2>&1
/tmp/venv-verify/bin/python -c "import yaml; print('venv + pip install 验证通过')"
rm -rf /tmp/venv-verify
pass "Python venv 创建 + pip install 成功"

echo ""

# ── AC-01-4: Node.js 前端构建验证 ──
info "AC-01-4: 验证 Node.js + npm 前端构建能力"
mkdir -p /tmp/npm-test && cd /tmp/npm-test
# 初始化一个最小 npm 项目
cat > package.json <<'PKG'
{
  "name": "frontend-verify",
  "version": "0.0.1",
  "scripts": {
    "build": "node -e \"console.log('build ok')\""
  }
}
PKG
npm install 2>&1
npm run build 2>&1
cd / && rm -rf /tmp/npm-test
pass "Node.js + npm 前端构建验证成功"

echo ""
echo "========================================"
echo "  所有验证通过！"
echo "========================================"
