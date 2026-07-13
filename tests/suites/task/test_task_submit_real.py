"""测试真实 task_submit 工具调用。"""
import os
import sys
import asyncio
import pytest

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from channels.cli.cli_main import CLIApplication


@pytest.mark.integration
async def test_task_submit():
    """测试 task_submit 工具调用。"""
    print("=== 测试 Task Submit 工具调用 ===\n")

    # 创建 CLI 应用（真实模式）
    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    # 运行管道
    prompt = """请使用 task_submit 工具提交一个任务：
- 目标类型: agent
- 目标ID: resource_manager_agent  
- 任务标题: 创建数据分析专家 Agent
- 任务描述: 创建一个专门用于数据分析的 Agent，具备数据清洗、统计分析、可视化能力
- 优先级: 5
- 任务范围: short_term

请调用 task_submit 工具提交这个任务。"""

    print(f"【用户输入】")
    print(f"{prompt}\n")

    final_state = await app._engine.run(initial_state={"user_input": prompt})

    print(f"【结果分析】")
    print(f"  迭代次数: {final_state.get('iteration', 0)}")
    print(f"  ended: {final_state.get('ended', False)}")

    # 检查工具调用
    raw_tool_calls = final_state.get('raw_tool_calls', [])
    tool_results = final_state.get('tool_results', [])

    print(f"  raw_tool_calls: {len(raw_tool_calls)}")
    print(f"  tool_results: {len(tool_results)}")

    if raw_tool_calls:
        print(f"\n  【Raw Tool Calls】")
        for i, tc in enumerate(raw_tool_calls):
            print(f"    [{i}] name: {tc.get('name')}")
            print(f"         args: {tc.get('args', tc.get('arguments', {}))}")

    if tool_results:
        print(f"\n  【Tool Results】")
        for i, tr in enumerate(tool_results):
            print(f"    [{i}] tool_name: {tr.get('tool_name')}")
            print(f"         success: {tr.get('success')}")
            print(f"         data: {tr.get('data')}")
            print(f"         error: {tr.get('error')}")

    print(f"\n  【LLM 回复】")
    raw_result = final_state.get('raw_result', '')
    if raw_result:
        # 将 emoji 替换为 ASCII
        raw_str = str(raw_result)
        raw_ascii = raw_str.replace('✅', '[OK]').replace('❌', '[FAIL]').replace('📝', '[TASK]')
        raw_ascii = raw_ascii.encode('ascii', errors='ignore').decode('ascii')
        print(f"  {raw_ascii[:500]}...")
    else:
        print("  (无回复)")

    # 检查任务是否被创建
    if tool_results:
        for tr in tool_results:
            if tr.get('tool_name') == 'task_submit' and tr.get('success'):
                data = tr.get('data', {})
                if data.get('success'):
                    print(f"\n  [SUCCESS] 任务提交成功！")
                    print(f"      task_id: {data.get('task_id')}")
                    print(f"      message: {data.get('message')}")
                else:
                    print(f"\n  [FAIL] 任务提交失败: {data.get('error')}")

if __name__ == "__main__":
    asyncio.run(test_task_submit())
