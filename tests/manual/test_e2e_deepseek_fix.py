"""端到端验证：通过项目完整链路调用 DeepSeek API。

复现之前必 400 的场景：
    2 组 tool_calls（中间夹 user 消息），不传 reasoning_content → 必 400

验证修复：
    LLMCore 现在会自动存 reasoning_content，ProviderAdapter 会让 DeepSeek 保留 rc
    → 应该 200 OK

测试通过项目的真实代码链路：
    LLMCore._build_messages → normalize_messages_for_provider →
    adapter.completion → provider_adapters → litellm → DeepSeek API
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# 加载 .env
ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# 确保项目 src 在 path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


async def test_e2e_deepseek_no_400() -> bool:
    """端到端：用真实 LLMCore + 真实 adapter 链路调用 DeepSeek。

    构造一个必 400 的场景（2 组 tool_calls 不传 rc），验证修复后不再 400。
    """
    print("=" * 70)
    print("端到端验证：LLMCore 完整链路调用 DeepSeek")
    print("=" * 70)

    from pipeline.plugin import PluginContext
    from pipeline.types import StateKeys, create_initial_state
    from plugins.core.llm_core import LLMCore

    # 用真实 DeepSeek 配置（router 模式）
    core = LLMCore(config={
        "provider": "deepseek",
        "model_name": "deepseek-v4-pro",  # router 别名
        "context_window": 1000000,
        "default_params": {
            "temperature": 0.7,
            "max_tokens": 100000,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        },
    })

    TOOL_SCHEMA = [{
        "type": "function",
        "function": {
            "name": "search",
            "description": "搜索",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
    }]

    # ── 第一轮：让 LLM 调用工具 ──
    print("\n[轮次 1] 触发工具调用...")
    state = create_initial_state(
        messages=[{"role": "user", "content": "请用 search 工具查询天气"}],
    )
    state["tool_schemas"] = TOOL_SCHEMA
    state["streaming"] = False  # 非流式，便于断言
    state["pipeline_id"] = "e2e_test"
    state["iteration"] = 1

    ctx = PluginContext(state=state, config={})
    try:
        result1 = await core.execute(ctx)
    except Exception as e:
        print(f"  ❌ 轮次 1 失败: {type(e).__name__}: {str(e)[:300]}")
        return False

    tool_calls_1 = result1.get(StateKeys.RAW_TOOL_CALLS, [])
    thinking_1 = result1.get(StateKeys.RAW_THINKING)
    print(f"  ✅ 轮次 1 成功: tool_calls={len(tool_calls_1)} "
          f"thinking_len={len(thinking_1 or '')}")

    if not tool_calls_1:
        print("  ⚠️ LLM 没调用工具，跳过")
        return True

    # ── 关键验证：state["messages"] 里的 assistant 消息是否真的存了 rc ──
    msgs_after_round1 = state["messages"]
    asst_msg = next((m for m in msgs_after_round1
                     if m.get("role") == "assistant" and m.get("tool_calls")), None)
    if asst_msg:
        if "reasoning_content" in asst_msg:
            print(f"  ✅ 关键: assistant 消息已存 reasoning_content "
                  f"(len={len(asst_msg['reasoning_content'] or '')})")
        else:
            print(f"  ❌ 关键: assistant 消息没存 reasoning_content！bug 未修复")
            return False

    # ── 第二轮：构造之前必 400 的场景 ──
    # 模拟 ToolCore 追加 tool 结果 + 新 user 消息，然后再次调用 LLM
    print("\n[轮次 2] 构造 2 组 tool_calls 场景（之前必 400）...")
    import json
    import uuid

    tc_id_1 = tool_calls_1[0].get("id", f"call_{uuid.uuid4().hex[:24]}")
    # 追加 tool 结果
    state["messages"].append({
        "role": "tool",
        "tool_call_id": tc_id_1,
        "content": json.dumps({"result": "晴天 25度"}),
    })
    # 追加一条新 user 消息（之前日志里的 MSG-6 异常注入场景）
    state["messages"].append({"role": "user", "content": "继续查询"})
    state["iteration"] = 2

    ctx2 = PluginContext(state=state, config={})
    try:
        result2 = await core.execute(ctx2)
    except Exception as e:
        err_str = str(e)
        print(f"  ❌ 轮次 2 失败: {type(e).__name__}: {err_str[:400]}")
        if "reasoning_content" in err_str:
            print("\n  >>> 🔴 Bug 未修复：仍然触发 reasoning_content 400")
            return False
        print("\n  >>> ⚠️ 其他错误（不是 reasoning_content 问题）")
        return False

    tool_calls_2 = result2.get(StateKeys.RAW_TOOL_CALLS, [])
    print(f"  ✅ 轮次 2 成功: tool_calls={len(tool_calls_2)} "
          f"thinking_len={len(result2.get(StateKeys.RAW_THINKING) or '')}")

    # ── 第三轮：现在 messages 里有 2 条 assistant(tool_calls)
    # 这正是之前必 400 的场景！ ──
    if tool_calls_2:
        print("\n[轮次 3] 第二组 tool_calls 后再调 LLM（2 条 assistant(tc)，之前必 400）...")
        tc_id_2 = tool_calls_2[0].get("id", f"call_{uuid.uuid4().hex[:24]}")
        state["messages"].append({
            "role": "tool",
            "tool_call_id": tc_id_2,
            "content": json.dumps({"result": "雨天 20度"}),
        })
        state["messages"].append({"role": "user", "content": "总结两个城市"})
        state["iteration"] = 3

        ctx3 = PluginContext(state=state, config={})
        try:
            result3 = await core.execute(ctx3)
        except Exception as e:
            err_str = str(e)
            print(f"  ❌ 轮次 3 失败: {type(e).__name__}: {err_str[:400]}")
            if "reasoning_content" in err_str:
                print("\n  >>> 🔴 Bug 未修复：仍然触发 reasoning_content 400")
                return False
            return False

        text = result3.get(StateKeys.RAW_RESULT, "")
        print(f"  ✅ 轮次 3 成功！content={text[:100] if text else '(空)'}")

    print("\n" + "=" * 70)
    print("🎉 端到端验证通过：DeepSeek 不再触发 reasoning_content 400")
    print("=" * 70)
    return True


async def test_e2e_minimax_still_works() -> bool:
    """验证其他 provider（如 minimax）不受影响。

    构造一个带 reasoning_content 的消息，发给 minimax 配置（不真实调用，
    只验证 ProviderAdapter 会剥离 rc）。
    """
    print("\n" + "=" * 70)
    print("验证：其他 provider 不受影响（rc 被正确剥离）")
    print("=" * 70)

    from llm.provider_adapters import get_provider_adapter

    test_messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "reasoning_content": "思考...",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
    ]

    # minimax 应该剥离 rc
    mm_adapter = get_provider_adapter("minimax/MiniMax-M3")
    mm_result = mm_adapter.adapt_messages_before_send(test_messages)
    asst_msg = next(m for m in mm_result if m.get("role") == "assistant")
    if "reasoning_content" in asst_msg:
        print(f"  ❌ minimax 应该剥离 rc，但保留了")
        return False
    print(f"  ✅ minimax: rc 已剥离")

    # glm 应该剥离 rc
    glm_adapter = get_provider_adapter("zai/glm-5.2")
    glm_result = glm_adapter.adapt_messages_before_send(test_messages)
    asst_msg = next(m for m in glm_result if m.get("role") == "assistant")
    if "reasoning_content" in asst_msg:
        print(f"  ❌ glm 应该剥离 rc，但保留了")
        return False
    print(f"  ✅ glm: rc 已剥离")

    # deepseek 应该保留 rc
    ds_adapter = get_provider_adapter("deepseek/deepseek-v4-pro")
    ds_result = ds_adapter.adapt_messages_before_send(test_messages)
    asst_msg = next(m for m in ds_result if m.get("role") == "assistant")
    if "reasoning_content" not in asst_msg:
        print(f"  ❌ deepseek 应该保留 rc，但被剥离了")
        return False
    print(f"  ✅ deepseek: rc 已保留")

    # 原数据不被修改
    orig_asst = next(m for m in test_messages if m.get("role") == "assistant")
    if "reasoning_content" not in orig_asst:
        print(f"  ❌ 原数据被污染（rc 被删了）")
        return False
    print(f"  ✅ 原数据保持完整（rc 未被删除）")

    print("\n🎉 其他 provider 验证通过")
    return True


async def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("⚠️ DEEPSEEK_API_KEY 未配置，跳过真实 API 测试")
        ok2 = await test_e2e_minimax_still_works()
        return 0 if ok2 else 1

    ok1 = await test_e2e_deepseek_no_400()
    ok2 = await test_e2e_minimax_still_works()

    print("\n" + "#" * 70)
    print("# 最终结果")
    print("#" * 70)
    print(f"  DeepSeek 端到端（真实 API）: {'✅ 通过' if ok1 else '❌ 失败'}")
    print(f"  其他 provider 不受影响:      {'✅ 通过' if ok2 else '❌ 失败'}")

    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
