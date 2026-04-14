"""
真实测试：验证 Agent 能主动调用工具创建文件

这是真正的功能验收测试 - 不是 Mock，不是直接调用 API，
而是通过管道让 Agent 自主决定调用工具并执行。
"""
import asyncio
import sys
import os
from pathlib import Path

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from channels.cli.cli_main import CLIApplication
from pipeline.config import load_pipeline_config, build_plugin_registry
from pipeline.engine import PipelineEngine


async def test_agent_tool_call():
    """测试 Agent 主动调用工具创建文件。"""
    print("=" * 60)
    print("Agent 工具调用真实测试")
    print("=" * 60)

    # 准备测试环境
    test_output_dir = Path("test_output")
    test_output_dir.mkdir(exist_ok=True)
    test_file = test_output_dir / "agent_created.txt"

    # 如果文件已存在，先删除
    if test_file.exists():
        test_file.unlink()

    print(f"\n[准备] 测试文件路径: {test_file}")
    print(f"[准备] 文件存在: {test_file.exists()}")

    # 创建 CLI 应用并设置真实管道
    print("\n[1/4] 初始化 CLI 应用...")
    app = CLIApplication(streaming=False)
    app.setup_real_pipeline()
    print("    OK: 管道初始化成功")

    # 验证 pending_tools 插件存在
    print("\n[2/4] 验证 pending_tools 插件...")
    pending_tools = app._plugin_registry.get("pending_tools")
    if pending_tools:
        print(f"    OK: pending_tools 插件已注册 ({type(pending_tools).__name__})")
    else:
        print("    FAIL: pending_tools 插件未注册！")
        return False

    # 构建 Agent 状态
    print("\n[3/4] 准备发送消息...")
    agent_state = app._build_agent_state()

    # 关键测试：让 Agent 创建文件
    # 明确告诉 Agent 使用 write_to_file 工具
    user_input = (
        "请使用 write_to_file 工具创建一个文件，"
        f"filePath 是 '{test_file}'，"
        'content 是 "Hello from Agent OS! File created by tool call."'
    )

    user_state = {"user_input": user_input}
    merged_state = {**agent_state, **user_state}

    print(f"    用户输入: {user_input[:80]}...")

    # 执行管道
    print("\n[4/4] 执行管道（等待 Agent 响应）...")
    result_state = await app._engine.run(merged_state)

    # 检查结果
    print("\n" + "-" * 60)
    print("结果检查:")
    print("-" * 60)

    # 1. 检查 LLM 响应
    raw_result = result_state.get("raw_result", "")
    if raw_result:
        print(f"[OK] LLM 有响应 ({len(raw_result)} 字符)")
    else:
        print("[WARN] LLM 无 raw_result")

    # 2. 检查工具调用
    raw_tool_calls = result_state.get("raw_tool_calls", [])
    if raw_tool_calls:
        print(f"[OK] LLM 请求了 {len(raw_tool_calls)} 个工具调用")
        for tc in raw_tool_calls:
            print(f"    - 工具: {tc.get('function', {}).get('name', 'unknown')}")
    else:
        print("[INFO] LLM 未请求工具调用（可能直接回复了）")

    # 3. 检查文件是否被创建（关键验证）
    print(f"\n[关键验证] 检查文件是否创建...")
    print(f"    文件路径: {test_file}")
    print(f"    文件存在: {test_file.exists()}")

    if test_file.exists():
        content = test_file.read_text()
        print(f"    文件内容: {content[:100]}...")
        print("\n" + "=" * 60)
        print("[SUCCESS] 测试通过！Agent 成功调用工具创建了文件")
        print("=" * 60)
        return True
    else:
        print("\n" + "=" * 60)
        print("[FAIL] 测试失败！文件未被创建")
        print("=" * 60)

        # 调试信息
        print("\n[调试] 管道最终状态:")
        for key, value in result_state.items():
            if key not in ['messages', 'conversation_history']:
                print(f"    {key}: {value}")

        return False


if __name__ == "__main__":
    success = asyncio.run(test_agent_tool_call())
    sys.exit(0 if success else 1)
