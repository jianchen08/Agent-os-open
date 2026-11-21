#!/usr/bin/env bash
# 部署回读校验（Linux 服务器侧）：部署位实际字节 vs 随包 deploy-manifest.json。
#
# 设计：docs/decisions/2026-11-10-deploy-freshness-verification.md
#
# 三段（旧包缺清单时 1/3 与 3/3 跳过，不阻断升级前的历史部署）：
#   1/3 清单回读   deploy/verify_deploy_manifest.py verify——missing / 哈希不符
#                  逐项报出；关键项（plugin.json / frontend index / config
#                  内核配置）命中即退出 1；--strict 追加旧版残留判定。
#   2/3 构建产物   内核二进制 + 3 个 cdylib 存在可执行（服务器 build.sh 现编，
#                  不在清单内，故单独断言）。
#   3/3 运行态     --runtime-base <url> 时读 /health 并比对 version == 清单版本。
#
# 用法：
#   bash deploy/verify_deploy.sh                       # 静态回读（部署后/启动前）
#   bash deploy/verify_deploy.sh --strict              # 追加「旧版残留文件」判定
#   bash deploy/verify_deploy.sh --runtime-base http://127.0.0.1:9100
#   bash deploy/verify_deploy.sh --warn                # 只报不拦（手工维护放行）
# 退出码：0 = 通过/告警放行；1 = 存在未落地项；2 = 参数/环境错。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="${AGENTOS_DEPLOY_MANIFEST:-$REPO_ROOT/deploy/deploy-manifest.json}"

MODE="normal"
RUNTIME_BASE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --strict) MODE="strict"; shift ;;
        --warn) MODE="warn"; shift ;;
        --runtime-base) RUNTIME_BASE="${2:-}"; shift 2 ;;
        --runtime-base=*) RUNTIME_BASE="${1#*=}"; shift ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "[verify-deploy] 未知参数: $1" >&2; exit 2 ;;
    esac
done

PY_BIN=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "pass" >/dev/null 2>&1; then
        PY_BIN="$cand"
        break
    fi
done
if [[ -z "$PY_BIN" ]]; then
    echo "[verify-deploy] FAIL python3/python 均不可用（清单回读依赖）" >&2
    exit 1
fi

FAILED=0

# ---------- 1/3 清单回读 ----------
if [[ -f "$MANIFEST" ]]; then
    echo "[verify-deploy] 1/3 清单回读：$MANIFEST"
    verify_args=(verify --root "$REPO_ROOT" --manifest "$MANIFEST")
    if [[ "$MODE" == "strict" ]]; then
        verify_args+=(--strict)
    fi
    if ! "$PY_BIN" "$REPO_ROOT/deploy/verify_deploy_manifest.py" "${verify_args[@]}"; then
        FAILED=1
    fi
else
    echo "[verify-deploy] 1/3 SKIP 无 deploy/deploy-manifest.json（旧包/非发布包构建）"
fi

# ---------- 2/3 构建产物断言 ----------
echo "[verify-deploy] 2/3 构建产物断言（内核 + cdylib，服务器现编不在清单内）"
KERNEL_BIN=""
for cand in "$REPO_ROOT/kernel-bin/agentos-kernel" "$REPO_ROOT/kernel/target/release/agentos-kernel"; do
    if [[ -x "$cand" ]]; then
        KERNEL_BIN="$cand"
        break
    fi
done
if [[ -n "$KERNEL_BIN" ]]; then
    echo "[verify-deploy] PASS 内核二进制 $KERNEL_BIN"
else
    echo "[verify-deploy] FAIL 内核二进制缺失（先执行 deploy/build.sh）"
    FAILED=1
fi
for so in tool_core sensitive_checker spill_guard; do
    found="$(find "$REPO_ROOT/plugins/shared/$so" -maxdepth 1 -name 'lib*.so' -type f -print -quit 2>/dev/null)"
    if [[ -n "$found" ]]; then
        echo "[verify-deploy] PASS cdylib $so -> $(basename "$found")"
    else
        echo "[verify-deploy] FAIL cdylib 缺失: $so（先执行 deploy/build.sh）"
        FAILED=1
    fi
done

# ---------- 3/3 运行态（可选） ----------
if [[ -n "$RUNTIME_BASE" ]]; then
    echo "[verify-deploy] 3/3 运行态 /health：$RUNTIME_BASE"
    health_version="$(
        "$PY_BIN" - "$RUNTIME_BASE" <<'PYEOF'
import json
import sys
import urllib.request

base = sys.argv[1].rstrip("/")
try:
    # 显式绕过 http_proxy/https_proxy：服务器环境常带全局代理变量，localhost
    # 探活经代理会得到 502（WSL 实测），属探针自身故障面而非服务未起。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"{base}/health", timeout=10) as resp:
        print(json.loads(resp.read().decode("utf-8")).get("version", ""))
except Exception as exc:  # noqa: BLE001 - CLI 面统一降级为退出码
    print(f"ERROR: {exc}")
    sys.exit(1)
PYEOF
    )" || {
        echo "[verify-deploy] FAIL /health 不可达：$health_version"
        FAILED=1
        health_version=""
    }
    if [[ -n "$health_version" ]]; then
        expected_version=""
        if [[ -f "$MANIFEST" ]]; then
            expected_version="$("$PY_BIN" - "$MANIFEST" <<'PYEOF'
import json
import sys

try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("version", ""))
except Exception:  # noqa: BLE001 - 清单损坏已在 1/3 报红，这里只降级为空
    print("")
PYEOF
)"
        fi
        if [[ -n "$expected_version" && "$health_version" != "$expected_version" ]]; then
            echo "[verify-deploy] FAIL 运行内核版本 $health_version ≠ 清单版本 $expected_version（旧进程未重启？）"
            FAILED=1
        else
            echo "[verify-deploy] PASS 运行内核版本 = ${health_version:-?}"
        fi
    fi
else
    echo "[verify-deploy] 3/3 SKIP 未指定 --runtime-base（服务启动后可复验：--runtime-base http://127.0.0.1:9100）"
fi

if [[ "$FAILED" -ne 0 ]]; then
    if [[ "$MODE" == "warn" ]]; then
        echo "[verify-deploy] WARN 存在未落地/缺失项（--warn / AGENTOS_DEPLOY_VERIFY=warn 放行）"
        exit 0
    fi
    echo "[verify-deploy] FAIL 部署位与发版清单不一致（详见上）" >&2
    exit 1
fi
echo "[verify-deploy] PASS 部署位与发版清单一致"
