#!/bin/bash
# WSL 隔离沙箱 scratch 内核 runner + 自诊断（skills/plugin-dev）
#
# 解决的问题（2026-11-14/15 装机实测三轮定位）：任务在 WSL workspace-only
# 沙箱内做「真内核+真 LLM」验证时反复失败——非架构阻塞，是供给与知识缺位：
#   ① Linux 内核二进制其实存在（kernel/target/release/agentos-kernel）；
#   ② 网络/工作区 llm.yaml/user_root/.env 密钥都在沙箱可见面内；
#   ③ 唯二缺口=裸起内核没加载密钥环境变量 + 不知道 drvfs 装载要 5-8 分钟。
# 本脚本 = 一条命令完成「自检 → 起内核 → 等健康 → 带症状输出」，
# 幂等可重复；重启后重跑即恢复同样状态（持久层语义）。
#
# 用法（Windows 侧）:
#   wsl -d Ubuntu --exec bash -c 'bash /mnt/d/<仓库>/skills/plugin-dev/scripts/wsl_scratch_kernel.sh --wait 600'
# 用法（WSL 侧/沙箱内, cwd=仓库根）:
#   bash skills/plugin-dev/scripts/wsl_scratch_kernel.sh --wait 600
# 退出码: 0=健康可用 1=二进制缺失 3=健康等待超时(见末尾 DIAG)
set -u

SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SELF_DIR/../../.." && pwd)"
cd "$ROOT" || exit 1

WAIT=${WAIT_SEC:-480}
if [ "${1:-}" = "--wait" ] && [ -n "${2:-}" ]; then WAIT=$2; fi

# ① 自检：内核二进制
BIN=kernel/target/release/agentos-kernel
if [ ! -x "$BIN" ]; then
  echo "DIAG[1]: Linux 内核二进制缺失（$BIN）"
  echo "  修复: WSL 内 cd kernel && cargo build --release（工具链持久层已含 cargo）"
  exit 1
fi
echo "OK[1] Linux 内核二进制在位"

# ② 自检：密钥与 provider 配置（沙箱可见面内）
[ -f user_root/.env ] && grep -q "API_KEY\|OLLAMA" user_root/.env \
  && echo "OK[2a] user_root/.env 密钥供给在位" \
  || echo "WARN[2a] user_root/.env 缺失/无密钥 → llm 调用会缺 key（修复：从装机 .env 抄 MINIMAX_API_KEY/SILICONFLOW_API_KEY 等入该文件，勿入库）"
[ -f config/plugins/llm/llm.yaml ] \
  && echo "OK[2b] config/plugins/llm/llm.yaml provider 配置在位" \
  || echo "WARN[2b] config/plugins/llm/llm.yaml 缺失 → 无 provider 可用（修复：从装机 %APPDATA%\\agentos\\config\\plugins\\llm\\llm.yaml 抄一份到同相对路径，勿入库）"

# ③ 环境装载（密钥经 set -a 进内核进程环境）
set -a; source user_root/.env 2>/dev/null; set +a
export AGENTOS_CONFIG_ROOT="$ROOT/config"
export AGENTOS_USER_ROOT="$ROOT/user_root"
export AGENTOS_PLUGINS_DIR="$ROOT/plugins/shared"
export AGENTOS_DB_PATH="${HOME}/agentos-scratch.db"
export AGENTOS_ADMIN_PASSWORD="${AGENTOS_ADMIN_PASSWORD:-scratch-admin}"
export AGENTOS_DISABLE_SELF_EXIT=1

# ④ 幂等起内核（清历史 scratch 实例——只清 WSL 侧，不碰装机 Windows 内核）
pkill -f "target/release/agentos-kernel" 2>/dev/null; sleep 1
nohup "$BIN" > /tmp/scratch_kernel_stdout.log 2>&1 &
echo "BOOT: 内核已后台启动；装载 96 插件走 /mnt/d(drvfs) 约需 5-8 分钟，属预期非卡死"

# ⑤ health 等待（每 10s 探测，带进度）
i=0
while [ "$i" -lt "$WAIT" ]; do
  sleep 10; i=$((i+10))
  code=$(python3 - <<'PY' 2>/dev/null
import urllib.request
try:
    print(urllib.request.urlopen("http://127.0.0.1:9100/health", timeout=4).status)
except Exception as e:
    print(getattr(e, "code", "") or "conn_fail")
PY
)
  echo "[${i}s] health=$code"
  if [ "$code" = "200" ]; then
    echo "READY: scratch 内核健康可用（登录 admin / 密码 $AGENTOS_ADMIN_PASSWORD，端口 9100 WSL 内）"
    echo "验证完成后清理: pkill -f target/release/agentos-kernel"
    exit 0
  fi
done

echo "DIAG[3]: ${WAIT}s 未就绪（装载进行中或端口被占）"
echo "  修复: ①加长等待 bash $0 --wait 900（drvfs 首次装载可达 8-10 分钟）"
echo "        ②查端口占用 pgrep -fa agentos-kernel"
echo "        ③看日志 tail -50 $ROOT/logs/kernel.log.$(date +%Y-%m-%d)"
exit 3
