#!/usr/bin/env bash
# E2E 测试覆盖率补全 - 复现验证脚本
# 用法: bash verify_e2e_review.sh
set -e
cd "$(dirname "$0")"

echo "=== 步骤 1: 检查测试文件存在 ==="
ls -l tests/e2e/test_trigger.py tests/e2e/test_memory.py tests/e2e/test_approval.py tests/e2e/test_multichannel.py tests/e2e/test_workspace.py tests/e2e/conftest.py

echo ""
echo "=== 步骤 2: 检查 Bug 修复落地 ==="
echo "-- conftest fixtures --"
grep -n "_ensure_demo_user\|_reset_rate_limiter" tests/e2e/conftest.py | head -10
echo "-- media_reviewer 可选导入 --"
sed -n '15,22p' src/review/media_reviewer.py
echo "-- uvicorn 延迟导入 --"
grep -n "import uvicorn" src/channels/websocket/app_factory.py
echo "-- routes_auth logout 撤销 --"
grep -n "revoke_refresh_token\|def logout" src/channels/api/routes_auth.py
echo "-- routes_memory 路由顺序（{memory_id} 必须在 stats/semantic 之后） --"
grep -n "@router" src/channels/api/routes_memory.py | head -20

echo ""
echo "=== 步骤 3: Pytest 收集所有新增 E2E 测试 ==="
cd src
PYTHONPATH=$PWD python3 -m pytest ../tests/e2e/test_trigger.py ../tests/e2e/test_memory.py ../tests/e2e/test_approval.py ../tests/e2e/test_multichannel.py ../tests/e2e/test_workspace.py --collect-only 2>&1 | grep "tests collected"

echo ""
echo "=== 步骤 4: 执行 5 个新增测试文件 ==="
PYTHONPATH=$PWD python3 -m pytest ../tests/e2e/test_trigger.py ../tests/e2e/test_memory.py ../tests/e2e/test_approval.py ../tests/e2e/test_multichannel.py ../tests/e2e/test_workspace.py ../tests/e2e/test_tool_call.py -v 2>&1 | tail -10

echo ""
echo "=== 步骤 5: Docker 端口校验（任务声称改成 8989/5290，实际仍 8988/5289） ==="
cd ..
echo "-- 搜索 8989/5290 --"
grep -rn "8989\|5290" docker-compose.yml start_web*.bat start_web*.sh 2>/dev/null || echo "  ✗ 未找到 8989/5290（任务声称已修改但实际未落地）"
echo "-- 当前 docker-compose 端口 --"
grep -E "ports:|[0-9]{4}:[0-9]{4}" docker-compose.yml | head -10
