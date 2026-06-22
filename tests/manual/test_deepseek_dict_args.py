"""DeepSeek thinking 模式 - 验证 dict arguments 在多轮中是否触发 reasoning_content 400。

场景 J 已经证明 dict arguments 会触发 400（错误信息是 "invalid type: map"）。
但项目日志里报的是 "reasoning_content must be passed back"。
这说明项目里触发的可能是 **更深层的问题**：arguments 是 dict 导致 litellm 内部
解析失败，然后错误信息被覆盖。

测试：多轮 + dict arguments，看实际错误信息。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-pro"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "获取指定城市的天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def call_api(messages: list[dict], *, tools=None, tag: str = "") -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    if tools:
        payload["tools"] = tools

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload)
        elapsed = time.monotonic() - t0
        if resp.status_code != 200:
            return {
                "tag": tag, "ok": False, "status": resp.status_code,
                "error": resp.text[:800], "elapsed": round(elapsed, 2),
            }
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        return {
            "tag": tag, "ok": True, "status": 200,
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": msg.get("tool_calls"),
            "usage": data.get("usage"),
            "elapsed": round(elapsed, 2),
        }
    except Exception as exc:
        return {"tag": tag, "ok": False, "status": -1,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.monotonic() - t0, 2)}


def scenario_k_multiturn_dict_args() -> dict:
    """场景 K：多轮（3 轮）+ 第二轮起 arguments 用 dict。

    真实场景：LLMCore 第一轮返回 assistant(tool_calls)，arguments 是 string。
    ToolCore 执行后第二轮调用 LLM，这次 messages 里带着第一轮的 assistant，
    arguments 应该还是 string。但如果 ToolCore 重建了 assistant 消息
    （has_tool_call_msg=False 场景），arguments 就会变成 dict。
    """
    print("\n" + "=" * 70)
    print("场景 K：3 轮工具调用，第二轮开始 arguments 为 dict")
    print("=" * 70)

    # 第一轮
    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="K-round1",
    )
    print(f"[K-round1] ok={r1['ok']} has_tool_calls={bool(r1.get('tool_calls'))}")
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "K", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # 第二轮：第一组 assistant 用正确的 string arguments
    messages = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {
                 "name": tc1["function"]["name"],
                 "arguments": tc1["function"]["arguments"],  # string（正确）
             },
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
        {"role": "user", "content": "再查上海天气"},
    ]
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="K-round2")
    print(f"[K-round2] ok={r2['ok']} has_tool_calls={bool(r2.get('tool_calls'))}")
    if not r2["ok"] or not r2.get("tool_calls"):
        return {"scenario": "K", "result": "BLOCKED_AT_R2", "r2": r2}
    tc2 = r2["tool_calls"][0]

    # 第三轮：关键！第二组 assistant 用 DICT arguments（模拟 ToolCore bug）
    messages_dict = [
        {"role": "user", "content": "查北京天气"},
        # 第一组：正确 string
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"],
                          "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
        {"role": "user", "content": "再查上海天气"},
        # 第二组：错误的 dict arguments（ToolCore bug）
        {"role": "assistant", "content": r2.get("content") or "",
         "tool_calls": [{
             "id": tc2["id"], "type": "function",
             "function": {
                 "name": tc2["function"]["name"],
                 "arguments": {"city": "上海"},  # ← dict，复刻 ToolCore bug
             },
         }]},
        {"role": "tool", "tool_call_id": tc2["id"],
         "content": json.dumps({"city": "上海", "weather": "雨 20度"})},
        {"role": "user", "content": "总结对比"},
    ]
    r3 = call_api(messages_dict, tools=[WEATHER_TOOL], tag="K-round3-dict-args")
    print(f"[K-round3] ok={r3['ok']} status={r3['status']}")
    if not r3["ok"]:
        err = r3.get('error', '')
        print(f"  ❌ 错误: {err[:400]}")
        if 'reasoning_content' in err:
            print("  >>> 错误类型: reasoning_content（与项目日志一致！）")
            return {"scenario": "K", "result": "DICT_ARGS_TRIGGERS_REASONING_400"}
        return {"scenario": "K", "result": "DICT_ARGS_TRIGGERS_OTHER_400", "error_snippet": err[:200]}
    print(f"  ✅ content: {(r3.get('content') or '')[:200]}")
    return {"scenario": "K", "result": "OK", "r3": r3}


def scenario_l_orphan_tool_result() -> dict:
    """场景 L：孤立的 tool result（前面没有 assistant tool_calls）。

    _message_normalizer 的 Phase A 专门处理这个，但如果清理失败会怎样？
    """
    print("\n" + "=" * 70)
    print("场景 L：孤立的 tool result（无对应 assistant tool_calls）")
    print("=" * 70)

    # 直接发一个孤立 tool result（没有对应的 assistant tool_calls）
    messages = [
        {"role": "user", "content": "查天气"},
        {"role": "tool", "tool_call_id": "call_nonexistent",
         "content": json.dumps({"weather": "晴 25度"})},
    ]
    r1 = call_api(messages, tools=[WEATHER_TOOL], tag="L-orphan-tool")
    print(f"[L] ok={r1['ok']} status={r1['status']}")
    if not r1["ok"]:
        err = r1.get('error', '')
        print(f"  ❌ 错误: {err[:400]}")
        if 'reasoning_content' in err:
            return {"scenario": "L", "result": "ORPHAN_TRIGGERS_REASONING_400"}
    else:
        print(f"  ✅ content: {(r1.get('content') or '')[:200]}")
    return {"scenario": "L", "result": "OK" if r1["ok"] else "FAILED", "r1": r1}


def scenario_m_assistant_tool_calls_without_all_results() -> dict:
    """场景 M：assistant(tool_calls) 有 3 个调用，但只跟了 2 个 tool result。

    这是 _message_normalizer Phase B 处理的场景，但如果清理失败...
    """
    print("\n" + "=" * 70)
    print("场景 M：3 个 tool_calls 只跟 2 个 tool result")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "查北京、上海、广州的天气（请同时调用 3 次）"}],
        tools=[WEATHER_TOOL], tag="M-round1",
    )
    print(f"[M-round1] ok={r1['ok']} tool_calls_count={len(r1.get('tool_calls') or [])}")
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "M", "result": "BLOCKED", "r1": r1}
    tcs = r1["tool_calls"]

    # 故意只给前 2 个 tool 结果，缺第 3 个
    messages = [
        {"role": "user", "content": "查北京、上海、广州的天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc["id"], "type": "function",
             "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
         } for tc in tcs]},
    ]
    # 只给前 2 个
    for i, tc in enumerate(tcs[:2]):
        messages.append({
            "role": "tool", "tool_call_id": tc["id"],
            "content": json.dumps({"city": f"city{i}", "weather": "晴"}),
        })
    # 第三个 tool_call 没有对应 tool 结果

    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="M-round2-missing-result")
    print(f"[M-round2] ok={r2['ok']} status={r2['status']}")
    if not r2["ok"]:
        err = r2.get('error', '')
        print(f"  ❌ 错误: {err[:400]}")
        if 'reasoning_content' in err:
            print("  >>> 错误类型: reasoning_content（与项目日志一致！）")
            return {"scenario": "M", "result": "MISSING_RESULT_TRIGGERS_REASONING_400"}
        return {"scenario": "M", "result": "MISSING_RESULT_OTHER_400", "error_snippet": err[:200]}
    print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    return {"scenario": "M", "result": "OK_DESPITE_MISSING", "r2": r2}


def main() -> int:
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：定位触发 reasoning_content 400 的真实条件")

    results = []
    results.append(scenario_k_multiturn_dict_args())
    results.append(scenario_l_orphan_tool_result())
    results.append(scenario_m_assistant_tool_calls_without_all_results())

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    for r in results:
        print(f"  场景 {r['scenario']}: {r['result']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
