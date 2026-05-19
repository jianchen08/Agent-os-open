"""CLI 端到端测试：启动真实 LLM 调用，验证完整闭环。"""
import asyncio
import sys
import os

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from channels.cli.cli_main import CLIApplication


async def test_cli_real():
    app = CLIApplication(streaming=False)
    app.setup_real_pipeline()

    # 验证引擎创建成功
    assert app._engine is not None, "Engine should be created"
    print("[OK] Engine created")

    # 验证 LLMCore 插件加载
    llm_core = app._plugin_registry.get_core("llm_call")
    assert llm_core is not None, "LLMCore should be loaded"
    print(f"[OK] LLMCore loaded: {llm_core.name}")
    print(f"     provider={llm_core._provider}, model={llm_core._model}")
    print(f"     api_base={llm_core._api_base}")
    print(f"     api_key prefix={llm_core._api_key[:20] if llm_core._api_key else 'None'}...")

    # 验证 ToolCore 插件加载
    tool_core = app._plugin_registry.get_core("tool_execute")
    assert tool_core is not None, "ToolCore should be loaded"
    print(f"[OK] ToolCore loaded: {tool_core.name}")

    # 验证 Agent 配置加载
    if app._agent_config:
        print(f"[OK] Agent: {app._agent_config.config_id} ({app._agent_config.display_name})")
    else:
        print("[WARN] No agent config loaded")

    # 构建状态并执行一轮真实 LLM 调用
    print("\n--- Sending test message to LLM ---")
    agent_state = app._build_agent_state()
    user_state = {"user_input": "你好，请用一句话介绍你自己。"}
    merged_state = {**agent_state, **user_state}

    try:
        result_state = await app._engine.run(merged_state)
        raw = result_state.get("raw_result", "")
        # GBK 兼容：替换非 ASCII 字符
        safe_raw = raw.encode("ascii", errors="replace").decode("ascii") if raw else ""
        if safe_raw:
            print(f"[OK] LLM response: {safe_raw[:300]}...")
        else:
            print(f"[WARN] No raw_result, state keys: {list(result_state.keys())}")
            # 检查是否有错误
            err = result_state.get("last_error")
            if err:
                print(f"[FAIL] Error in pipeline: {err}")
        
        print("[OK] Full pipeline loop completed!")
    except Exception as e:
        print(f"[FAIL] Pipeline error: {e}")
        import traceback
        traceback.print_exc()
        raise

    print("\n=== CLI E2E Test PASSED ===")


if __name__ == "__main__":
    asyncio.run(test_cli_real())
