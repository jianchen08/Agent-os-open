"""DeepSeek thinking 模式 - 用真实失败日志中的请求结构精确复现。

从 logs/pipeline/pipeline_a616a6a76609.log 提取实际失败的 messages 结构，
用真实 API 复现 400 错误，定位真正的触发条件。
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


def call_api(messages: list[dict], *, tools=None, tag: str = "", thinking=True) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
    }
    if thinking:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "max"
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


def scenario_h_real_failing_structure() -> dict:
    """场景 H：完整复现真实日志中的失败结构。

    关键差异（与之前测试对比）：
    - 真实日志的 MSG-6 是 content="1"（数字字符串），不是有意义的指令
    - 真实日志有 2 组连续的 tool_calls + tool results
    - 真实日志使用了完整的 thinking 模式

    构造一个真实复刻：2 组 tool_calls，中间夹着一条 user content="1"
    """
    print("\n" + "=" * 70)
    print("场景 H：完整复现真实日志的失败结构（2 组 tool_calls）")
    print("=" * 70)

    # 第一轮：让模型调用工具
    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="H-round1",
    )
    print(f"[H-round1] ok={r1['ok']} has_tool_calls={bool(r1.get('tool_calls'))}")
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "H", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # 构造完整复刻：assistant(tc1) → tool → user("1") → assistant(tc2) → tool
    # 先用第一轮结果构造前半段
    messages = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
        # 这就是日志中的 MSG-6
        {"role": "user", "content": "1"},
    ]
    # 第二轮：触发新的 tool_calls
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="H-round2")
    print(f"[H-round2] ok={r2['ok']} has_tool_calls={bool(r2.get('tool_calls'))}")
    if not r2["ok"] or not r2.get("tool_calls"):
        print(f"  错误: {r2.get('error', '')[:300]}")
        return {"scenario": "H", "result": "BLOCKED_AT_R2", "r2": r2}
    tc2 = r2["tool_calls"][0]

    # 关键时刻：构造完整的"两组 tool_calls 中间夹 user"结构
    # 这是日志中迭代 3 的完整结构
    messages_full = [
        {"role": "user", "content": "查北京天气"},
        # 第一组
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
        # 中间的 user（日志中 MSG-6）
        {"role": "user", "content": "1"},
        # 第二组
        {"role": "assistant", "content": r2.get("content") or "",
         "tool_calls": [{
             "id": tc2["id"], "type": "function",
             "function": {"name": tc2["function"]["name"], "arguments": tc2["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc2["id"],
         "content": json.dumps({"city": "上海", "weather": "雨 20度"})},
    ]
    r3 = call_api(messages_full, tools=[WEATHER_TOOL], tag="H-round3-two-groups-with-user-middle")
    print(f"[H-round3] ok={r3['ok']} status={r3['status']}")
    if not r3["ok"]:
        print(f"  ❌ 错误（复现！）: {r3.get('error', '')[:400]}")
        return {"scenario": "H", "result": "REPRODUCED_400", "r3": r3}
    print(f"  ✅ content: {(r3.get('content') or '')[:200]}")
    return {"scenario": "H", "result": "OK_NO_400", "r3": r3}


def scenario_i_empty_content_assistant() -> dict:
    """场景 I：assistant 消息 content 为空。

    真实日志中有些 assistant 消息 content="" （ToolCore 重建时）。
    DeepSeek 可能要求 content 不能为空。
    """
    print("\n" + "=" * 70)
    print("场景 I：assistant content 为空字符串")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="I-round1",
    )
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "I", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # 故意 content=""（模拟 ToolCore 重建的 assistant 消息）
    messages = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": "",  # ← 空字符串
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
    ]
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="I-round2-empty-content")
    print(f"[I-round2] ok={r2['ok']} status={r2['status']}")
    if r2["ok"]:
        print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    else:
        print(f"  ❌ 错误: {r2.get('error', '')[:400]}")
    return {"scenario": "I", "result": "OK" if r2["ok"] else "FAILED", "r2": r2}


def scenario_j_arguments_as_dict_not_string() -> dict:
    """场景 J：tool_calls arguments 用 dict 而非 string。

    ToolCore 重建时 arguments 是 dict（见 tool_core/plugin.py:771），
    但 OpenAI API 要求 arguments 是 JSON 字符串。
    """
    print("\n" + "=" * 70)
    print("场景 J：arguments 用 dict 而非 string（API 规范要求 string）")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="J-round1",
    )
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "J", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # arguments 用 dict（这正是 ToolCore 重建时的做法）
    messages = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {
                 "name": "get_weather",
                 "arguments": {"city": "北京"},  # ← dict 而非 string
             },
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
    ]
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="J-round2-dict-arguments")
    print(f"[J-round2] ok={r2['ok']} status={r2['status']}")
    if r2["ok"]:
        print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    else:
        print(f"  ❌ 错误: {r2.get('error', '')[:400]}")
    return {"scenario": "J", "result": "OK" if r2["ok"] else "FAILED", "r2": r2}


def main() -> int:
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：用真实日志结构精确复现 400")

    results = []
    results.append(scenario_h_real_failing_structure())
    results.append(scenario_i_empty_content_assistant())
    results.append(scenario_j_arguments_as_dict_not_string())

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    for r in results:
        print(f"  场景 {r['scenario']}: {r['result']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
