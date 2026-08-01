#!/usr/bin/env bash
# channel_api 4c/批次3/批次5 迁移端到端验证脚本（活系统）。
#
# 用途：在本地起好 内核 :9100 + channel_api sidecar + 前端 dev 后，跑此脚本
#       核验所有已迁移域（19 个有消费域）的 /ext/channel_api/<域>/** 端点。
#
# 这是 M5（:8988 退役）的前置门槛：本脚本全绿后，M5 才可安全退役。
#
# 设计：数据驱动——读 channel_api/plugin.json 的 http_endpoints，按 (method,path)
#       模板实例化一个探测请求。带 path-param 的端点用占位值替换；GET/DELETE 无 body，
#       POST/PUT 带 {} body。逐个探测，统计通过/失败。
#
# 用法：
#     bash scripts/verify_channel_api_4c_ext.sh [KERNEL_BASE_URL]
#     # 默认 KERNEL_BASE_URL=http://localhost:9100
#
# 退出码：0=全部通过；1=有失败项。
#
# [来源: docs/working/channel_api_migration_plan.md §批次4 4c/批次3/批次5 验收]

set -uo pipefail

BASE="${1:-http://localhost:9100}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_JSON="${SCRIPT_DIR}/../plugins/shared/system/channel_api/plugin.json"
PASS=0
FAIL=0
FAILED_CASES=()

# Python（Windows 原生）不认 /d/... 路径，转 Windows 路径。无 cygpath 时退回原值（Linux/Mac）。
PLUGIN_JSON_FOR_PYTHON="$PLUGIN_JSON"
if command -v cygpath >/dev/null 2>&1; then
    PLUGIN_JSON_FOR_PYTHON="$(cygpath -w "$PLUGIN_JSON")"
fi

if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    GREEN=""; RED=""; YELLOW=""; CYAN=""; RESET=""
fi

if [[ ! -f "$PLUGIN_JSON" ]]; then
    echo "${RED}找不到 plugin.json: $PLUGIN_JSON${RESET}" >&2
    exit 1
fi

# 从 plugin.json 提取 (method, path) 对。Python 解析 JSON 最稳。
ENDPOINTS=()
while IFS= read -r line; do
    line="${line%$'\r'}"  # 去 Windows 回车符（Python print 在 Windows 输出 \r\n）
    [[ -n "$line" ]] && ENDPOINTS+=("$line")
done < <(
    python -c "
import json
d = json.load(open(r'$PLUGIN_JSON_FOR_PYTHON', encoding='utf-8'))
for e in d.get('http_endpoints', []):
    print(e['method'] + '\t' + e['path'])
" 2>/dev/null
)

if [[ ${#ENDPOINTS[@]} -eq 0 ]]; then
    echo "${RED}plugin.json 无 http_endpoints，或 python 解析失败${RESET}" >&2
    exit 1
fi

# 把模板 path（含 {param}）实例化成可请求的具体 path。
# {container_task_id}/{task_id}/... → "probe-<seq>"；{config_path:path} → "models/llm"（多段）
instanciate() {
    local p="$1"
    # 单段 param {xxx} -> probe-id
    p="${p//\{container_task_id\}/probe-ct}"
    p="${p//\{task_id\}/probe-task}"
    p="${p//\{trigger_id\}/probe-trig}"
    p="${p//\{request_id\}/probe-req}"
    p="${p//\{review_id\}/probe-rev}"
    p="${p//\{scene_id\}/probe-scene}"
    p="${p//\{memory_id\}/probe-mem}"
    p="${p//\{model_id\}/probe-model}"
    p="${p//\{provider_id\}/probe-prov}"
    p="${p//\{artifact_id\}/probe-art}"
    p="${p//\{annotation_id\}/probe-ann}"
    p="${p//\{module_id\}/probe-mod}"
    p="${p//\{record_id\}/probe-rec}"
    p="${p//\{user_id\}/probe-user}"
    p="${p//\{session_id\}/probe-sess}"
    p="${p//\{execution_id\}/probe-exec}"
    p="${p//\{project_id\}/probe-proj}"
    p="${p//\{ac_id\}/probe-ac}"
    p="${p//\{item_id\}/probe-item}"
    p="${p//\{name\}/probe-name}"
    p="${p//\{phase\}/prepare}"
    # 多段通配 {config_path:path} -> models/llm
    p="${p//\{config_path:path\}/models/llm}"
    # 兜底：剩余的 {xxx} 单段
    p="$(printf '%s' "$p" | sed 's|{[^/}]*}|probe-x|g')"
    printf '%s' "$p"
}

# check <method> <templated_path>
# 预期：路由注册探测——成功路由到 handler 即算"路由在"。
#   GET/DELETE：期望非 404（handler 返回 200/400/500 都算路由注册成功；仅 404=路由未注册）。
#   POST/PUT/PATCH：带 {} body，同理期望非 404。
#   例外：asr/artifacts upload/media-review 等 multipart 端点非 multipart 会 400（仍非 404，算注册成功）。
check() {
    local method="$1" tpath="$2"
    local ipath; ipath="$(instanciate "$tpath")"
    local url="${BASE}${ipath}"
    local code
    # 注意：curl -w '%{http_code}' 即使连接失败也会输出 "000"（并 exit 7）。
    # 不用 `|| echo 000`（会叠加成 000\n000）。取首行作 code，连接失败=000。
    case "$method" in
        GET|DELETE)
            code=$(curl -s --max-time 15 -o /tmp/_ext_resp.json -w '%{http_code}' -X "$method" "$url" 2>/dev/null)
            ;;
        POST|PUT|PATCH)
            code=$(curl -s --max-time 15 -o /tmp/_ext_resp.json -w '%{http_code}' -X "$method" \
                "$url" -H 'Content-Type: application/json' --data '{}' 2>/dev/null)
            ;;
        *)
            code=$(curl -s --max-time 15 -o /tmp/_ext_resp.json -w '%{http_code}' -X "$method" "$url" 2>/dev/null)
            ;;
    esac
    local body; body=$(head -c 120 /tmp/_ext_resp.json 2>/dev/null)

    if [[ "$code" == "404" ]]; then
        printf "  ${RED}✗${RESET} [%s] %s %s  -> 路由未注册（404）\n" "$code" "$method" "$tpath"
        printf "      响应: %s\n" "$body"
        FAIL=$((FAIL+1))
        FAILED_CASES+=("$method $tpath -> $code (404 路由未注册)")
    elif [[ "$code" == "000" ]]; then
        printf "  ${RED}✗${RESET} [000] %s %s  -> 连接失败（内核 :9100 未起？）\n" "$method" "$tpath"
        FAIL=$((FAIL+1))
        FAILED_CASES+=("$method $tpath -> 000 (连接失败)")
    else
        printf "  ${GREEN}✓${RESET} [%s] %s %s\n" "$code" "$method" "$tpath"
        PASS=$((PASS+1))
    fi
}

TOTAL=${#ENDPOINTS[@]}
echo "${CYAN}channel_api 迁移端到端验证（KERNEL=$BASE）${RESET}"
echo "数据源: $PLUGIN_JSON"
echo "探测端点数: ${TOTAL}（plugin.json http_endpoints 全量）"
echo "判定: 非 404 = 路由已注册（handler 返回任意状态都算注册成功）；404 = 路由未注册（失败）"
echo "================================================================"

PREV_DOM=""
i=0
for ep in "${ENDPOINTS[@]}"; do
    i=$((i+1))
    method="${ep%%$'\t'*}"
    tpath="${ep#*$'\t'}"
    # 域名（path 第 2 段）作分组标题
    dom="${tpath#/ext/channel_api/}"
    dom="${dom%%/*}"
    # 每个域第一行打标题
    if [[ "$dom" != "$PREV_DOM" ]]; then
        echo ""
        echo "${YELLOW}── 域: ${dom} ──${RESET}"
        PREV_DOM="$dom"
    fi
    check "$method" "$tpath"
done

echo ""
echo "================================================================"
printf "结果：${GREEN}%d 通过${RESET}，${RED}%d 失败${RESET}（共 %d 端点）\n" "$PASS" "$FAIL" "$TOTAL"
if [[ "$FAIL" -gt 0 ]]; then
    printf "${RED}失败项（均为 404=路由未注册）：${RESET}\n"
    for c in "${FAILED_CASES[@]}"; do printf "  - %s\n" "$c"; done
    echo ""
    echo "${YELLOW}提示：${RESET}"
    echo "  - 确认内核 :9100 已起（agentos-kernel）"
    echo "  - 确认 channel_api sidecar 已被内核加载（plugin.json http_endpoints 生效）"
    echo "  - 确认 dispatcher 已注册全部 ${TOTAL} 条 /ext/channel_api/** 路由（看内核启动日志）"
    echo "  - 404 = 路由未注册（sidecar 未就绪 / plugin.json 未加载 / 路径模板不符）"
    exit 1
fi
echo "${GREEN}全部通过——所有 ${TOTAL} 个端点路由已注册，M5（:8988 退役）前置门槛已满足。${RESET}"
exit 0
