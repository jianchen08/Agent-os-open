"""直接测试 LLM 工具调用"""
import asyncio
import os
import sys

os.environ["PYTHONPATH"] = "src"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import litellm
from config.models import ModelConfigLoader

async def test():
    # 从配置加载 API Key
    loader = ModelConfigLoader()
    llm_data = loader._load_llm_data()
    model_config = llm_data.get("models", {}).get("minimax-m2.7", {})
    api_key = model_config.get("api_key")

    if not api_key:
        print("错误: 无法从配置加载 MINIMAX_API_KEY")
        return

    tools = [
        {
            "type": "function",
            "function": {
                "name": "task_submit",
                "description": "任务提交工具。将任务提交给指定的 Agent 执行。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {
                            "type": "object",
                            "description": "任务目标",
                            "properties": {
                                "title": {"type": "string", "description": "任务标题"},
                                "description": {"type": "string", "description": "任务描述"}
                            },
                            "required": ["title"]
                        },
                        "target_type": {"type": "string", "enum": ["agent"], "description": "目标类型"},
                        "target_id": {"type": "string", "description": "目标 Agent ID"}
                    },
                    "required": ["goal", "target_type", "target_id"]
                }
            }
        }
    ]

    messages = [
        {"role": "system", "content": "你是一个任务调度助手。当用户需要创建任务时，你必须使用 task_submit 工具。不要只返回文本描述，必须调用工具。"},
        {"role": "user", "content": "请使用 task_submit 工具提交一个任务：创建一个数据分析专家 Agent，目标 Agent 是 resource_manager_agent"}
    ]

    print("=" * 60)
    print("测试 LLM 工具调用")
    print("=" * 60)
    print(f"\n发送请求...")
    print(f"工具数量: {len(tools)}")

    try:
        response = await litellm.acompletion(
            model="minimax/MiniMax-M2.7",
            messages=messages,
            tools=tools,
            api_base="https://api.minimaxi.com/v1",
            api_key=api_key,
            temperature=0.7,
            max_tokens=4096,
        )

        choice = response.choices[0]
        message = choice.message

        print(f"\n响应内容: {message.content}")
        print(f"Finish reason: {choice.finish_reason}")

        if message.tool_calls:
            print("\n[PASS] 工具调用成功!")
            for tc in message.tool_calls:
                print(f"  - 工具: {tc.function.name}")
                print(f"  - 参数: {tc.function.arguments}")
        else:
            print("\n[FAIL] 没有工具调用")
            print(f"LLM 只是返回了文本，没有调用工具")

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test())
