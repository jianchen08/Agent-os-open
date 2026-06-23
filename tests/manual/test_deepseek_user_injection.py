"""DeepSeek thinking 模式 - 验证真正的 bug 根因。

基于真实日志分析发现的根因：
在 assistant(tool_calls) 和 tool 结果之间，被插入了一条 user 消息。

测试这个结构是否真的会触发 400。
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


def scenario_e_user_between_tool_calls() -> dict:
    """场景 E：在 assistant(tool_calls) 和 tool 结果之间插入 user 消息。

    这是真实日志中观察到的结构（MSG-6 role=user content="1"）。
    测试是否触发 DeepSeek 的 reasoning_content 错误。
    """
    print("\n" + "=" * 70)
    print("场景 E：assistant(tool_calls) → user → assistant(tool_calls) → tool")
    print("       （模拟真实日志中观察到的结构）")
    print("=" * 70)

    # 第一轮：触发工具调用
    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="E-round1",
    )
    print(f"[E-round1] ok={r1['ok']} has_tool_calls={bool(r1.get('tool_calls'))}")
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "E", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # 关键构造：模拟真实日志中的结构
    # assistant(tool_calls) → tool结果 → user("1") → assistant(tool_calls)
    messages = [
        {"role": "user", "content": "查北京天气"},
        # 第一组 assistant + tool_calls（不传 reasoning_content）
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        # tool 结果
        {"role": "tool", "tool_call_id": tc1["id"], "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
        # 🚨 这就是日志里 MSG-6 的来源：莫名其妙的 user 消息
        {"role": "user", "content": "1"},
        # 第二轮，触发新的工具调用
    ]
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="E-round2-with-user-injection")
    print(f"[E-round2] ok={r2['ok']} status={r2['status']}")
    if not r2["ok"]:
        print(f"  ❌ 错误: {r2.get('error', '')[:400]}")
        # 关键验证：这个错误是不是 reasoning_content 错误？
        err = r2.get('error', '')
        if 'reasoning_content' in err:
            print("  >>> 错误类型: reasoning_content（与项目日志一致！）")
            return {"scenario": "E", "result": "USER_INJECTION_CAUSES_REASONING_400"}
        return {"scenario": "E", "result": "USER_INJECTION_CAUSES_OTHER_400", "error": err[:200]}
    print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    return {"scenario": "E", "result": "OK_DESPITE_USER_INJECTION", "r2": r2}


def scenario_f_normal_no_user_injection() -> dict:
    """场景 F：正常流程（不插入 user 消息），作为对照。"""
    print("\n" + "=" * 70)
    print("场景 F：正常流程（无 user 消息注入）— 对照组")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="F-round1",
    )
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "F", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # 正常流程：assistant(tool_calls) → tool，中间不插 user
    messages = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"], "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
    ]
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="F-round2-normal")
    print(f"[F-round2] ok={r2['ok']} status={r2['status']}")
    if r2["ok"]:
        print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    else:
        print(f"  ❌ 错误: {r2.get('error', '')[:300]}")
    return {"scenario": "F", "result": "OK" if r2["ok"] else "FAILED", "r2": r2}


def scenario_g_user_after_complete_pair() -> dict:
    """场景 G：完整 assistant→tool 配对之后才插入 user 消息。

    这是正确的结构：[assistant(tool_calls), tool, tool, user, assistant(new)]
    """
    print("\n" + "=" * 70)
    print("场景 G：完整配对后才插 user（正确结构）")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="G-round1",
    )
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "G", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    # 正确：assistant(tool_calls) → tool → user → assistant(新)
    messages = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"], "content": json.dumps({"city": "北京", "weather": "晴 25度"})},
        # user 在完整的 assistant→tool 配对之后
        {"role": "user", "content": "再查上海天气"},
    ]
    r2 = call_api(messages, tools=[WEATHER_TOOL], tag="G-round2-correct-user-position")
    print(f"[G-round2] ok={r2['ok']} status={r2['status']}")
    if r2["ok"]:
        print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    else:
        print(f"  ❌ 错误: {r2.get('error', '')[:300]}")
    return {"scenario": "G", "result": "OK" if r2["ok"] else "FAILED", "r2": r2}


def main() -> int:
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"thinking: enabled")
    print(f"目标：验证 user 消息注入是否是 400 的真正根因")

    results = []
    results.append(scenario_e_user_between_tool_calls())
    results.append(scenario_f_normal_no_user_injection())
    results.append(scenario_g_user_after_complete_pair())

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    for r in results:
        print(f"  场景 {r['scenario']}: {r['result']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
