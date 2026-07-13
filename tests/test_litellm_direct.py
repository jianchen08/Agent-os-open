"""直接用 litellm 测试 normalize 后的消息序列"""
import asyncio
import os

import litellm

litellm.suppress_debug_info = True

API_KEY = os.environ.get("MINIMAX_API_KEY", "your-minimax-api-key")
API_BASE = "https://api.minimaxi.com/v1"
MODEL = "minimax/MiniMax-M2.7"


async def test_after_normalize():
    """模拟 normalize 后的消息序列直接发送给 MiniMax"""
    messages = [
        {"role": "system", "content": "你是一个调研专家 Agent。"},
        {"role": "user", "content": "调研 Agent OS 项目现状"},
        {"role": "assistant", "content": "我将执行全面调研任务...", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "resource_search", "arguments": '{"q": "project structure"}'}},
            {"id": "call_2", "type": "function", "function": {"name": "resource_search", "arguments": '{"q": "architecture"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": "Project structure results..."},
        {"role": "tool", "tool_call_id": "call_2", "content": "Architecture results..."},
        # 已被 normalize 转为 user 的 system 消息
        {"role": "user", "content": "[StreamRepetitionGuard] 检测到流式输出中出现重复内容"},
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_5", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "src/pipeline"}'}},
            {"id": "call_6", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "src/agents"}'}},
            {"id": "call_7", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "src/memory"}'}},
            {"id": "call_8", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "src/tools"}'}},
            {"id": "call_9", "type": "function", "function": {"name": "file_read", "arguments": '{"path": "config"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_5", "content": "路径不是文件: src\\pipeline"},
        {"role": "tool", "tool_call_id": "call_6", "content": "路径不是文件: src\\agents"},
        {"role": "tool", "tool_call_id": "call_7", "content": "路径不是文件: src\\memory"},
        {"role": "tool", "tool_call_id": "call_8", "content": "路径不是文件: src\\tools"},
        {"role": "tool", "tool_call_id": "call_9", "content": "路径不是文件: config"},
    ]

    print(f"Testing with {len(messages)} messages (after normalize)")
    print()

    # 检查是否有 system 在非首位
    for i, m in enumerate(messages):
        if i > 0 and m.get("role") == "system":
            print(f"WARNING: message [{i}] has role=system!")

    try:
        response = await litellm.acompletion(
            model=MODEL,
            messages=messages,
            api_base=API_BASE,
            api_key=API_KEY,
            max_tokens=50,
        )
        print(f"SUCCESS: {response.choices[0].message.content[:200]}")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}")
        print(f"Error: {str(e)[:500]}")


if __name__ == "__main__":
    asyncio.run(test_after_normalize())
