#!/bin/bash
# 环境阻碍自诊器（skills/skill-env-config-decision）
# 用途：任务执行前或环境类失败后运行，输出结构化阻碍清单（OBSTACLE 行）
#       与已满足面（PASS 行）。agent 据此按技能「阻碍自诊与修复」章制定
#       修复方案——先诊断再动手，禁止不加诊断原地重试。
# 用法：bash env_diagnose.sh [--target wsl|host]  （默认自动判当前执行面）
# 输出协议（agent 可解析）：
#   PASS     <检查项>: <满足描述>
#   OBSTACLE <症状> || ROOT=<根因类别> || FIX=<修复动作> || PERSIST=<一次性|持久层|入库>
#   INFO     <中性事实>
set -u

TARGET="${2:-auto}"
# ── 执行面判定 ──
IN_WSL=0
grep -qi microsoft /proc/version 2>/dev/null && IN_WSL=1
[ "$TARGET" = "wsl" ] && IN_WSL=1
echo "INFO 执行面: WSL=$IN_WSL cwd=$(pwd)"

# ── 检查1：内核二进制（按平台形态） ──
if [ -x kernel/target/release/agentos-kernel ]; then
  echo "PASS 内核二进制: kernel/target/release/agentos-kernel（Linux ELF）"
elif [ -x kernel/target/release/agentos-kernel.exe ]; then
  echo "PASS 内核二进制: agentos-kernel.exe（Windows）"
  [ "$IN_WSL" = "1" ] && echo "OBSTACLE 需在 WSL 内起 Linux 内核但只见 Windows exe || ROOT=平台错配 || FIX=WSL 内 cd kernel && cargo build --release 建 Linux ELF（工具链持久层已含 cargo） || PERSIST=持久层"
else
  echo "OBSTACLE 无内核二进制 || ROOT=未构建 || FIX=cd kernel && cargo build --release（按目标平台） || PERSIST=一次性（产物不入库）"
fi

# ── 检查2：模型密钥供给 ──
KEY_HIT=0
for f in user_root/.env .env; do
  if [ -f "$f" ] && grep -qE "API_KEY|api_key|OLLAMA" "$f" 2>/dev/null; then
    echo "PASS 密钥供给: $f 含 API key"
    KEY_HIT=1
  fi
done
[ "$KEY_HIT" = "0" ] && echo "OBSTACLE 工作区无模型密钥（.env/user_root/.env 均空或缺） || ROOT=密钥按纪律不入库，供给未建 || FIX=从装机 .env（%APPDATA%\\agentos\\.env）抄 MINIMAX_API_KEY/SILICONFLOW_API_KEY 等入 user_root/.env（git 已忽略，勿入库） || PERSIST=持久层（.env 被 git 忽略，工作区还原后需重抄，可写进自愈脚本）"

# ── 检查3：provider 配置 ──
if [ -f config/plugins/llm/llm.yaml ]; then
  echo "PASS provider 配置: config/plugins/llm/llm.yaml 在位"
else
  echo "OBSTACLE 无 llm.yaml（config/plugins/llm/） || ROOT=主模型配置只在装机用户空间（%APPDATA%\\agentos\\config\\plugins\\llm\\） || FIX=从装机抄一份到同相对路径（配合检查2的密钥），本地排除防误入库 || PERSIST=持久层"
fi

# ── 检查4：sidecar 运行环境布局 ──
SAMPLE_VENV=$(ls -d plugins/shared/*/.venv 2>/dev/null | head -1)
if [ -n "$SAMPLE_VENV" ]; then
  if [ -x "$SAMPLE_VENV/bin/python" ] || [ -e "$SAMPLE_VENV/bin" ]; then
    echo "PASS sidecar venv: 含 bin/（Linux 布局）——Linux 内核可拉起"
  elif [ -d "$SAMPLE_VENV/Scripts" ]; then
    echo "PASS sidecar venv: Scripts/（Windows 布局）——Windows 内核可拉起"
    [ "$IN_WSL" = "1" ] && echo "OBSTACLE 在 WSL 内跑 Linux 内核但 venv 仅 Windows 布局 || ROOT=venv 单平台布局 || FIX=WSL 内跑 venv_provision 双平台同建（同 .venv 补 bin/python+lib），装一次持久 || PERSIST=持久层"
  fi
else
  echo "INFO 未发现插件 .venv（内核或走系统 python/PYTHONPATH 通路）"
fi

# ── 检查5：装载耗时预期（drvfs） ──
if [ "$IN_WSL" = "1" ] && [ -d /mnt/d ]; then
  echo "INFO cwd 在 /mnt/d（drvfs）→ 内核装载 96 插件约 5-8 分钟（NTFS 宿主 ~14s），验证脚本等待窗口须 ≥600s"
fi

# ── 检查6：网络与装机可达性 ──
if command -v python3 >/dev/null 2>&1; then
  python3 - <<'PY' 2>/dev/null || true
import urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:9101/api/v1/schema", timeout=3)
    print("PASS 装机 9101 可达（同网段）")
except Exception as e:
    code = getattr(e, "code", None)
    if code:
        print(f"PASS 装机 9101 可达（HTTP {code}=鉴权门，正常）")
    else:
        print("INFO 装机 9101 不可达（WSL NAT 下 127.0.0.1 不通 Windows 宿主属预期；验证勿依赖装机内核，起 scratch 自含内核）")
PY
fi

echo "DONE 按技能「阻碍自诊与修复」章：OBSTACLE 行逐条定修复方案；修复后重跑本脚本直至无 OBSTACLE。"
