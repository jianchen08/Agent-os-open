"""简化版测试：验证任务闭环"""
import asyncio
import os
import sys
import pytest

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from channels.cli.cli_main import CLIApplication


@pytest.mark.integration
@pytest.mark.skip(reason="依赖已移除的 _build_agent_state API，需要 LLM API key")
async def test():
    print("=" * 60)
    print("测试任务闭环")
    print("=" * 60)

    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    prompt = """使用 task_submit 工具提交任务：
{
  "goal": {"title": "创建测试Agent", "description": "创建一个简单的测试Agent"},
  "target_type": "agent",
  "target_id": "resource_manager_agent",
  "acceptance_criteria": {"file_check": {"pass_threshold": 100}},
  "priority": 5,
  "task_scope": "short_term"
}"""

    agent_state = app._build_agent_state()
    merged_state = {**agent_state, "user_input": prompt}

    print("\n[执行中...]")

    # 检查初始 state 中的 tool_schemas
    print(f"\n[调试] 初始 state keys: {list(merged_state.keys())}")
    print(f"[调试] tool_ids: {merged_state.get('tool_ids', 'NOT SET')}")

    result = await app._engine.run(merged_state)

    # 检查最终 state
    print(f"\n[调试] 最终 state keys: {list(result.keys())}")
    print(f"[调试] tool_schemas: {result.get('tool_schemas', 'NOT SET')}")

    # 打印原始结果
    raw = result.get('raw_result', 'N/A')
    safe_raw = raw.encode('ascii', errors='ignore').decode('ascii') if raw else 'N/A'
    print(f"\n[调试] raw_result: {safe_raw[:500]}...")
    print(f"[调试] raw_tool_calls: {result.get('raw_tool_calls', [])}")
    print(f"[调试] raw_error: {result.get('raw_error', 'None')}")
    print(f"[调试] _last_raw_tool_calls: {result.get('_last_raw_tool_calls', [])}")

    # 检查工具调用结果（从 tool_results 读取）
    tool_results = result.get("tool_results", [])
    last_tool_calls = result.get("_last_raw_tool_calls", [])
    print(f"\n工具调用次数: {len(last_tool_calls)}")
    print(f"工具执行结果数: {len(tool_results)}")

    for tc in last_tool_calls:
        name = tc.get('name')
        print(f"  - 调用: {name}")

    for tr in tool_results:
        name = tr.get('tool_name')
        success = tr.get('success')
        print(f"  - 结果: {name}, success={success}")

    # 找 task_submit 结果
    submit_result = None
    for tr in tool_results:
        if tr.get('tool_name') == 'task_submit':
            submit_result = tr
            break

    if submit_result and submit_result.get('success'):
        data = submit_result.get('data', {})
        print("\n[PASS] 任务提交成功!")
        print(f"  task_id: {data.get('task_id')}")
        print(f"  status: {data.get('status')}")
    else:
        print("\n[FAIL] 任务未成功完成")
        if submit_result:
            print(f"  error: {submit_result.get('error')}")

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test())
