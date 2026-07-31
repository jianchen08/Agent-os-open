#!/usr/bin/env bash
# channel_api 4c 迁移端到端验证脚本（活系统）。
#
# 用途：在本地起好 内核 :9100 + channel_api sidecar + 前端 dev 后，跑此脚本
#       核验 9 个 4c 域的新 /ext/channel_api/<域>/** 端点返回 200（或预期状态），
#       并抽检旧 /api/v1/<对应> 已不应再被前端使用（前端 baseURL 已切，但旧端点
#       在 :8988 退役前可能仍可达——本脚本只验新路径，旧路径 404 核验留 M5 退役时）。
#
# 这是 M5（:8988 退役）的前置门槛：本脚本全绿后，M5 才可安全退役。
#
# 用法：
#     bash scripts/verify_channel_api_4c_ext.sh [KERNEL_BASE_URL]
#     # 默认 KERNEL_BASE_URL=http://localhost:9100
#
# 退出码：0=全部通过；1=有失败项。
#
# [来源: docs/working/channel_api_migration_plan.md §批次4 4c 验收]

set -uo pipefail

BASE="${1:-http://localhost:9100}"
PASS=0
FAIL=0
FAILED_CASES=()

# 颜色
if [[ -t 1 ]]; then
    GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
    GREEN=""; RED=""; YELLOW=""; RESET=""
fi

# check <描述> <预期HTTP状态码> <METHOD> <path> [post_data]
check() {
    local desc="$1" expect="$2" method="$3" path="$4" data="${5:-}"
    local url="${BASE}${path}"
    local code body
    if [[ -n "$data" ]]; then
        code=$(curl -s -o /tmp/_4c_resp.json -w '%{http_code}' \
            -X "$method" "$url" \
            -H 'Content-Type: application/json' \
            --data "$data" 2>/dev/null || echo 000)
    else
        code=$(curl -s -o /tmp/_4c_resp.json -w '%{http_code}' \
            -X "$method" "$url" 2>/dev/null || echo 000)
    fi
    body=$(head -c 200 /tmp/_4c_resp.json 2>/dev/null)
    if [[ "$code" == "$expect" ]]; then
        printf "  ${GREEN}✓${RESET} [%s] %s %s  -> %s\n" "$code" "$method" "$path" "$desc"
        PASS=$((PASS+1))
    else
        printf "  ${RED}✗${RESET} [%s, 期望 %s] %s %s  -> %s\n" "$code" "$expect" "$method" "$path" "$desc"
        printf "      响应: %s\n" "$body"
        FAIL=$((FAIL+1))
        FAILED_CASES+=("$desc ($method $path -> $code, 期望 $expect)")
    fi
}

echo "${YELLOW}channel_api 4c 端到端验证（KERNEL=$BASE）${RESET}"
echo "================================================================"
echo ""
echo "${YELLOW}[1/9] config 域${RESET}"
check "LLM 配置"            200 GET  "/ext/channel_api/config/llm"
check "LLM providers"       200 GET  "/ext/channel_api/config/llm/providers"
check "LLM models"          200 GET  "/ext/channel_api/config/llm/models"
check "LLM defaults"        200 GET  "/ext/channel_api/config/llm/defaults"
check "上下文窗口配置"      200 GET  "/ext/channel_api/config/context-window"
check "API 配置"            200 GET  "/ext/channel_api/config/api"
check "并发配置"            200 GET  "/ext/channel_api/config/concurrency"
check "成本控制配置"        200 GET  "/ext/channel_api/config/cost-control"

echo ""
echo "${YELLOW}[2/9] thinking-mode 域${RESET}"
check "健康检查"            200 GET  "/ext/channel_api/thinking-mode/health"
check "思考模型列表"        200 GET  "/ext/channel_api/thinking-mode/models"
check "思考模式推荐"        200 POST "/ext/channel_api/thinking-mode/recommendations" '{}'

echo ""
echo "${YELLOW}[3/9] users 域（stub）${RESET}"
check "用户列表"            200 GET  "/ext/channel_api/users"
check "用户统计"            200 GET  "/ext/channel_api/users/stats"
check "用户设置"            200 GET  "/ext/channel_api/users/settings"

echo ""
echo "${YELLOW}[4/9] sessions 域（stub）${RESET}"
check "会话总Token用量"     200 GET  "/ext/channel_api/sessions/test-sess/total-token-usage"
check "上下文Token用量"     200 GET  "/ext/channel_api/sessions/test-sess/context-token-usage"

echo ""
echo "${YELLOW}[5/9] client 域${RESET}"
check "客户端能力注册"      200 POST "/ext/channel_api/client/register" \
    '{"clientType":"web","version":"1.0.0","renderingSpaces":["main"]}'

echo ""
echo "${YELLOW}[6/9] execution 域${RESET}"
check "执行记录列表"        200 GET  "/ext/channel_api/execution/records?limit=10"
check "有记录的会话列表"    200 GET  "/ext/channel_api/execution/records/sessions"
check "记录分组概要"        200 GET  "/ext/channel_api/execution/records/group-summary"

echo ""
echo "${YELLOW}[7/9] modules/ui 域${RESET}"
check "模块 UI Schema 列表" 200 GET  "/ext/channel_api/modules/ui"

echo ""
echo "${YELLOW}[8/9] memory 域${RESET}"
check "记忆列表"            200 GET  "/ext/channel_api/memory"
check "记忆统计"            200 GET  "/ext/channel_api/memory/stats"
check "情景记忆列表"        200 GET  "/ext/channel_api/memory/episodes"
check "语义记忆列表"        200 GET  "/ext/channel_api/memory/semantic"

echo ""
echo "${YELLOW}[9/9] artifacts + annotations 域${RESET}"
check "制品列表(无task)"    200 GET  "/ext/channel_api/artifacts"
check "制品详情(不存在)"    200 GET  "/ext/channel_api/artifacts/nonexistent-id"
check "制品批注列表"        200 GET  "/ext/channel_api/artifacts/a1/annotations"

echo ""
echo "================================================================"
printf "结果：${GREEN}%d 通过${RESET}，${RED}%d 失败${RESET}\n" "$PASS" "$FAIL"
if [[ "$FAIL" -gt 0 ]]; then
    printf "${RED}失败项：${RESET}\n"
    for c in "${FAILED_CASES[@]}"; do printf "  - %s\n" "$c"; done
    echo ""
    echo "${YELLOW}提示：${RESET}"
    echo "  - 确认内核 :9100 已起（agentos-kernel）"
    echo "  - 确认 channel_api sidecar 已被内核加载（plugin.json http_endpoints 生效）"
    echo "  - 确认 dispatcher 已注册 75 条 /ext/channel_api/** 路由（看内核启动日志）"
    echo "  - 404 多为 sidecar 未就绪或路由未注册；502/503 为 handler 错误"
    exit 1
fi
echo "${GREEN}全部通过——4c 9 域端到端 OK，M5（:8988 退役）前置门槛已满足。${RESET}"
exit 0
