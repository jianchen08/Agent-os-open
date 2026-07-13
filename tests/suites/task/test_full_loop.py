"""测试完整任务闭环：灵汐提交任务 -> 子 Agent 执行 -> 结果返回"""

import asyncio
import os
import sys
import pytest

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

from channels.cli.cli_main import CLIApplication


@pytest.mark.integration
@pytest.mark.skip(reason="依赖已移除的 _build_agent_state API，需要 LLM API key")
async def test_full_loop():
    """测试完整任务闭环"""
    print("=" * 60)
    print("测试完整任务闭环")
    print("=" * 60)

    # 创建 CLI 应用（真实模式）
    app = CLIApplication(streaming=False)
    app.setup_pipeline()

    # 验证引擎创建成功
    assert app._engine is not None, "Engine should be created"
    print("\n[OK] Engine created")

    # 构建提示词 - 让灵汐提交任务创建 Agent
    prompt = """你是一个任务派发 Agent。请使用 task_submit 工具提交一个任务，让 resource_manager_agent 创建一个数据分析专家 Agent。

task_submit 参数：
{
  "goal": {
    "title": "创建数据分析专家 Agent 配置",
    "description": "创建一个专门用于数据分析的 Agent，具备数据清洗、统计分析、可视化能力"
  },
  "target_type": "agent",
  "target_id": "resource_manager_agent",
  "acceptance_criteria": {
    "file_check": {"pass_threshold": 100}
  },
  "priority": 5,
  "task_scope": "short_term"
}"""

    # 构建状态
    agent_state = app._build_agent_state()
    user_state = {"user_input": prompt}
    merged_state = {**agent_state, **user_state}

    print("\n[1] 启动灵汐 Agent，准备提交任务...")

    # 执行管道
    print("\n[执行中] 等待管道执行完成...")
    result_state = await app._engine.run(merged_state)

    # 检查结果
    print("\n" + "=" * 60)
    print("执行结果")
    print("=" * 60)

    iteration = result_state.get("iteration", 0)
    print(f"迭代次数: {iteration}")

    # 检查工具调用
    tool_calls = result_state.get("tool_calls", [])
    print(f"\n工具调用: {len(tool_calls)} 次")
    for i, tc in enumerate(tool_calls):
        name = tc.get('name', 'unknown')
        success = tc.get('result', {}).get('success', False)
        print(f"  [{i+1}] {name}: success={success}")

    # 检查 task_submit 结果
    task_submit_result = None
    for tc in tool_calls:
        if tc.get("name") == "task_submit":
            task_submit_result = tc.get("result", {})
            break

    if task_submit_result:
        print(f"\n[2] 任务提交结果:")
        print(f"  success: {task_submit_result.get('success')}")
        print(f"  task_id: {task_submit_result.get('task_id')}")
        print(f"  status: {task_submit_result.get('status')}")
        print(f"  target_agent: {task_submit_result.get('target_id')}")

        # 检查执行结果
        exec_result = task_submit_result.get("execution_result", "")
        if exec_result:
            print(f"\n[3] 子 Agent 执行结果:")
            # 替换 emoji 为 ASCII
            safe_result = str(exec_result).encode("ascii", errors="ignore").decode("ascii")
            print(f"  {safe_result[:800]}...")
    else:
        print("\n[2] 未检测到 task_submit 调用!")

    # LLM 最终回复
    print(f"\n[4] 灵汐 Agent 最终回复:")
    raw_result = result_state.get("raw_result", "")
    if raw_result:
        safe_reply = str(raw_result).encode("ascii", errors="ignore").decode("ascii")
        print(f"  {safe_reply[:500]}...")
    else:
        print("  (无回复)")

    # 检查错误
    error = result_state.get("error")
    if error:
        print(f"\n[ERROR] {error}")
        return False

    # 验证成功标准
    success = (
        task_submit_result is not None
        and task_submit_result.get("success") is True
        and task_submit_result.get("status") == "completed"
    )

    print("\n" + "=" * 60)
    if success:
        print("[PASS] 完整任务闭环测试通过!")
        print("  - 灵汐 Agent 成功调用 task_submit")
        print("  - 任务被创建并立即执行")
        print("  - 子 Agent 执行完成并返回结果")
        print("  - 任务状态: completed")
    else:
        print("[FAIL] 测试未通过")
        if task_submit_result is None:
            print("  - 未检测到 task_submit 调用")
        elif not task_submit_result.get("success"):
            print(f"  - task_submit 失败: {task_submit_result.get('error')}")
        elif task_submit_result.get("status") != "completed":
            print(f"  - 任务未成功完成，状态: {task_submit_result.get('status')}")
    print("=" * 60)

    # 如果成功，检查是否创建了 Agent 配置文件
    if success:
        import os
        agent_config_path = os.path.join("config", "agents", "generated", "data_analyst.yaml")
        if os.path.exists(agent_config_path):
            print(f"\n[验证] Agent 配置文件已创建: {agent_config_path}")
            with open(agent_config_path, 'r', encoding='utf-8') as f:
                content = f.read()
            print(f"[验证] 文件大小: {len(content)} 字符")
        else:
            print(f"\n[注意] Agent 配置文件未找到: {agent_config_path}")
            print("[注意] 子 Agent 可能执行了但没有创建文件，或文件路径不同")

    return success


if __name__ == "__main__":
    success = asyncio.run(test_full_loop())
    sys.exit(0 if success else 1)
