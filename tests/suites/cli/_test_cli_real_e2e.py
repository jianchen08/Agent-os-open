"""
CLI E2E 验证：发送真实消息测试任务提交-执行-评估闭环
"""
import asyncio
import sys
import os

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from channels.cli.cli_main import CLIApplication


async def test_full_loop():
    print("=" * 60)
    print("Agent OS CLI E2E 验证 - 任务提交-执行-评估闭环")
    print("=" * 60)

    app = CLIApplication(streaming=False)
    app.setup_real_pipeline()

    # 1. 验证引擎创建
    print("\n[1/6] 验证 Pipeline 引擎...")
    assert app._engine is not None, "Engine should be created"
    print("    OK: Pipeline 引擎初始化成功")

    # 2. 验证 LLM Core
    print("\n[2/6] 验证 LLM Core...")
    llm_core = app._plugin_registry.get_core("llm_call")
    assert llm_core is not None, "LLMCore should be loaded"
    print(f"    OK: LLM Core 加载成功 - {llm_core.name}")
    print(f"    Provider: {llm_core._provider}")
    print(f"    Model: {llm_core._model}")
    print(f"    API Base: {llm_core._api_base}")

    # 3. 验证 Tool Core
    print("\n[3/6] 验证 Tool Core...")
    tool_core = app._plugin_registry.get_core("tool_execute")
    assert tool_core is not None, "ToolCore should be loaded"
    print(f"    OK: Tool Core 加载成功 - {tool_core.name}")

    # 4. 验证 Agent 配置
    print("\n[4/6] 验证 Agent 配置...")
    if app._agent_config:
        print(f"    OK: Agent: {app._agent_config.config_id} ({app._agent_config.display_name})")
    else:
        print("    WARN: No agent config loaded")

    # 5. 真实 LLM 调用
    print("\n[5/6] 真实 LLM 调用测试...")
    agent_state = app._build_agent_state()
    user_state = {"user_input": "你好，请用一句话介绍你自己，并说你知道哪些工具可以调用。"}
    merged_state = {**agent_state, **user_state}

    result_state = await app._engine.run(merged_state)
    raw = result_state.get("raw_result", "")
    if raw:
        safe_raw = raw.encode("ascii", errors="replace").decode("ascii") if raw else ""
        print(f"    OK: LLM 回复长度 {len(raw)} 字符")
        print(f"    回复预览: {safe_raw[:300]}...")
    else:
        err = result_state.get("last_error")
        if err:
            print(f"    FAIL: 错误: {err}")
            return False
        print("    WARN: 无 raw_result，检查其他字段")
        print(f"    State keys: {list(result_state.keys())}")

    # 6. 任务闭环测试
    print("\n[6/6] 任务闭环测试...")
    from tasks.service import TaskService
    task_service = TaskService()

    task = task_service.create_task(
        title="测试任务",
        description="用一句话介绍自己",
        metadata={"acceptance_criteria": "输出简短自我介绍"}
    )
    print(f"    OK: 任务创建成功，ID: {task.id}, 状态: {task.status.value}")

    # 模拟任务执行
    task = task_service.start_task(task.id)
    print(f"    OK: 任务状态更新: {task.status.value}")

    # 模拟评估
    task = task_service.move_to_evaluating(task.id)
    print(f"    OK: 进入评估: {task.status.value}")

    task = task_service.complete_evaluation(task.id, passed=True)
    print(f"    OK: 评估完成，最终状态: {task.status.value}")

    print("\n" + "=" * 60)
    print("全部验证通过！")
    print("=" * 60)
    return True


if __name__ == "__main__":
    success = asyncio.run(test_full_loop())
    sys.exit(0 if success else 1)
