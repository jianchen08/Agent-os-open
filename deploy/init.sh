#!/usr/bin/env bash
# AgentOS 部署 venv 供给与配置初始化（deploy/build.sh 之后、首次启动之前执行；
# 插件依赖变更后可重入）。
#
# 1/3 _host 共享宿主 venv 先行：合宿成员（plugin.json 声明 host_group）经 _host
#    共享宿主 spawn，自身 .venv 运行期零消费（venv 去重 ADR 2026-09-07），故
#    逐插件 sync 必须跳过它们；成员依赖并集由 _host/pyproject.toml 承载。
# 2/3 独占宿主插件逐个 uv sync：内核 autoprovision（AGENTOS_PLUGIN_VENV_AUTOPROVISION=1）
#    只建 venv 壳不装插件依赖（2026-10-09 WSL2 实测 spawn 即 pydantic missing），
#    依赖必须按 uv.lock 锁定装齐；--frozen 失败（无锁文件/锁漂移）回退普通 sync。
# 3/3 .env 落位提示：LLM keys 读序 进程环境 > <AGENTOS_USER_ROOT>/.env。
# 4/4 部署回读校验（ADR 2026-11-10-deploy-freshness-verification）：部署位
#    实际字节 vs 随包 deploy/deploy-manifest.json——本次发版的 diff 与二进制
#    产物是否真的落到本机；缺失/哈希不符/关键项命中即红（旧包无清单则跳过）。
#
# 干跑校验：AGENTOS_INIT_DRY_RUN=1 只打印将执行的命令不执行（不动 venv）。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRY_RUN="${AGENTOS_INIT_DRY_RUN:-0}"

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "[init][dry-run] $*"
    else
        "$@"
    fi
}

# ========== 前置工具 ==========
if ! command -v uv >/dev/null 2>&1; then
    echo "[ERROR] uv 未安装：插件 venv 统一经 uv 锁定构建。安装：curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi
# python3 内联解析 plugin.json；Windows Git Bash 的 WindowsApps python3 可能是
# 商店占位 stub（执行即非零退出），探测失败回退 python。
PY_BIN=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "pass" >/dev/null 2>&1; then
        PY_BIN="$cand"
        break
    fi
done
if [ -z "$PY_BIN" ]; then
    echo "[ERROR] python3/python 均不可用：解析 plugin.json 需要（Linux 部署机标配 python3）。" >&2
    exit 1
fi

# ========== 1/3 _host 共享宿主 venv ==========
echo "[init] 1/3 _host 共享宿主 venv（合宿成员依赖并集）..."
run uv sync --project "$REPO_ROOT/plugins/shared/_host"

# ========== 2/3 独占宿主插件逐个 uv sync ==========
echo "[init] 2/3 独占宿主插件逐个 uv sync（host_group 成员跳过）..."
synced=0
skipped=0
# 仅收同时有 plugin.json + pyproject.toml 的目录（pipeline 下两级嵌套一并覆盖）
while IFS= read -r dir; do
    meta="$("$PY_BIN" - "$dir/plugin.json" <<'PYEOF'
import json, sys
m = json.load(open(sys.argv[1], encoding="utf-8"))
print(m.get("id", ""), m.get("host_group") or "")
PYEOF
)"
    plugin_id="${meta%% *}"
    host_group="${meta#* }"
    if [ -n "$host_group" ]; then
        echo "[init]   跳过（合宿成员 $host_group，经 _host spawn）: $plugin_id"
        skipped=$((skipped + 1))
        continue
    fi
    echo "[init]   uv sync: $plugin_id ($dir)"
    if ! run uv sync --frozen --project "$dir"; then
        echo "[init]   --frozen 失败（无锁文件或锁漂移），回退普通 sync: $plugin_id" >&2
        run uv sync --project "$dir"
    fi
    synced=$((synced + 1))
done < <(
    find "$REPO_ROOT/plugins/shared" \
        -type f -name pyproject.toml -exec dirname {} \; \
        | while IFS= read -r d; do [ -f "$d/plugin.json" ] && printf '%s\n' "$d"; done \
        | sort
)
echo "[init]   合计: sync $synced / 跳过 $skipped"

# ========== 3/3 配置落位提示（不自动写，防覆盖既有密钥） ==========
echo "[init] 3/3 配置落位提示："
echo "  - EnvironmentFile：cp deploy/agentos.env.example /etc/agentos/agentos.env 后填两把密钥等必设项（含明文口令，保持 0600）；"
echo "    裸起（deploy/start_server.sh）不读该文件，按同名变量以环境变量注入，逐项对齐即可。"
echo "  - LLM keys：落 <AGENTOS_USER_ROOT>/.env（或经内核设置页写入）；进程环境同名键优先。"
# ========== 4/4 部署回读校验（ADR 2026-11-10-deploy-freshness-verification）=====
echo "[init] 4/4 部署回读校验（发版清单 vs 部署位实际字节）..."
if [ -f "$REPO_ROOT/deploy/deploy-manifest.json" ]; then
    run bash "$REPO_ROOT/deploy/verify_deploy.sh"
else
    echo "[init]   跳过：无 deploy/deploy-manifest.json（旧包/非发布包构建）"
fi
echo "[init] 完成。下一步：systemd（deploy/agentos-kernel.service）或 bash deploy/start_server.sh。"
