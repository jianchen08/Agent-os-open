"""DeepSeek thinking 模式 - 终极复现测试。

组合两个 bug：
1. MSG-6 user content="1" 异常注入
2. MSG-7 assistant 由 ToolCore 重建，arguments 是 dict（而非 string）

这是真实日志中迭代 3 的完整结构。
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


def scenario_n_combined_bugs() -> dict:
    """场景 N：组合 user 注入 + dict arguments（真实日志复刻）。

    完整复刻迭代 3 的 11 条消息结构：
    MSG-0 system
    MSG-1 user
    MSG-2 assistant tool_calls=[3个]   (LLMCore 原生产, arguments=string)
    MSG-3 tool
    MSG-4 tool
    MSG-5 tool
    MSG-6 user content="1"             (异常注入)
    MSG-7 assistant tool_calls=[3个]   (ToolCore 重建, arguments=DICT ← bug)
    MSG-8 tool
    MSG-9 tool
    MSG-10 tool
    """
    print("\n" + "=" * 70)
    print("场景 N：组合 bug（user 注入 + dict arguments）")
    print("=" * 70)

    # 第一轮：触发 3 个并行工具调用
    r1 = call_api(
        [{"role": "user", "content": "同时查北京、上海、广州的天气"}],
        tools=[WEATHER_TOOL], tag="N-round1",
    )
    print(f"[N-round1] ok={r1['ok']} tool_calls_count={len(r1.get('tool_calls') or [])}")
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "N", "result": "BLOCKED", "r1": r1}
    tcs1 = r1["tool_calls"]

    # 第二轮：在第一组 tool_calls 后插入 user("1")，触发第二组 tool_calls
    msgs_r2 = [
        {"role": "user", "content": "同时查北京、上海、广州的天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc["id"], "type": "function",
             "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
         } for tc in tcs1]},
    ]
    # 3 个 tool 结果
    for i, tc in enumerate(tcs1):
        msgs_r2.append({
            "role": "tool", "tool_call_id": tc["id"],
            "content": json.dumps({"city": f"city{i}", "weather": "晴"}),
        })
    # MSG-6：异常 user 注入
    msgs_r2.append({"role": "user", "content": "1"})
    # 再加一条指令触发第二组工具调用
    msgs_r2.append({"role": "user", "content": "再查深圳、杭州、成都的天气"})

    r2 = call_api(msgs_r2, tools=[WEATHER_TOOL], tag="N-round2")
    print(f"[N-round2] ok={r2['ok']} tool_calls_count={len(r2.get('tool_calls') or [])}")
    if not r2["ok"]:
        err = r2.get('error', '')
        print(f"  ❌ 错误: {err[:400]}")
        if 'reasoning_content' in err:
            return {"scenario": "N", "result": "REPRODUCED_AT_R2"}
        return {"scenario": "N", "result": "OTHER_400_AT_R2", "error": err[:200]}
    if not r2.get("tool_calls"):
        return {"scenario": "N", "result": "NO_TOOL_CALLS_AT_R2"}
    tcs2 = r2["tool_calls"]

    # 第三轮：完整 11 条消息复刻（关键！）
    msgs_r3 = [
        # MSG-0 system
        {"role": "system", "content": "你是天气助手"},
        # MSG-1 user
        {"role": "user", "content": "同时查北京、上海、广州的天气"},
        # MSG-2 assistant tool_calls（LLMCore 原生产，arguments=string）
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc["id"], "type": "function",
             "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
         } for tc in tcs1]},
        # MSG-3,4,5 tool 结果
    ]
    for i, tc in enumerate(tcs1):
        msgs_r3.append({
            "role": "tool", "tool_call_id": tc["id"],
            "content": json.dumps({"city": f"city{i}", "weather": "晴"}),
        })
    # MSG-6 user content="1"（异常注入）
    msgs_r3.append({"role": "user", "content": "1"})
    # MSG-7 assistant tool_calls（ToolCore 重建，arguments=DICT ← bug）
    msgs_r3.append({
        "role": "assistant", "content": r2.get("content") or "",
        "tool_calls": [{
            "id": tc["id"], "type": "function",
            "function": {
                "name": tc["function"]["name"],
                "arguments": {"city": f"city{i}"},  # ← dict（ToolCore bug）
            },
        } for i, tc in enumerate(tcs2)],
    })
    # MSG-8,9,10 tool 结果
    for i, tc in enumerate(tcs2):
        msgs_r3.append({
            "role": "tool", "tool_call_id": tc["id"],
            "content": json.dumps({"city": f"city{i}", "weather": "雨"}),
        })

    print(f"\n[N-round3] 发送 {len(msgs_r3)} 条消息（复刻日志结构）...")
    r3 = call_api(msgs_r3, tools=[WEATHER_TOOL], tag="N-round3-combined-bugs")
    print(f"[N-round3] ok={r3['ok']} status={r3['status']}")
    if not r3["ok"]:
        err = r3.get('error', '')
        print(f"  ❌ 错误: {err[:400]}")
        if 'reasoning_content' in err:
            print("  >>> 复现 reasoning_content 错误！")
            return {"scenario": "N", "result": "REPRODUCED_REASONING_400"}
        return {"scenario": "N", "result": "OTHER_400", "error": err[:200]}
    print(f"  ✅ content: {(r3.get('content') or '')[:200]}")
    return {"scenario": "N", "result": "OK", "r3": r3}


def scenario_o_just_dict_args_with_user_in_middle() -> dict:
    """场景 O：简化版 - 只测 dict arguments + user 注入，2 组 tool_calls。"""
    print("\n" + "=" * 70)
    print("场景 O：dict arguments + user 注入（简化版）")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "查北京天气"}],
        tools=[WEATHER_TOOL], tag="O-round1",
    )
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "O", "result": "BLOCKED", "r1": r1}
    tc1 = r1["tool_calls"][0]

    r2_msgs = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"weather": "晴 25度"})},
        {"role": "user", "content": "1"},
        {"role": "user", "content": "再查上海"},
    ]
    r2 = call_api(r2_msgs, tools=[WEATHER_TOOL], tag="O-round2")
    if not r2["ok"] or not r2.get("tool_calls"):
        return {"scenario": "O", "result": "BLOCKED_AT_R2"}
    tc2 = r2["tool_calls"][0]

    # 第三轮：dict arguments + user 注入
    msgs = [
        {"role": "user", "content": "查北京天气"},
        {"role": "assistant", "content": r1.get("content") or "",
         "tool_calls": [{
             "id": tc1["id"], "type": "function",
             "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
         }]},
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": json.dumps({"weather": "晴 25度"})},
        {"role": "user", "content": "1"},
        # 第二组：dict arguments
        {"role": "assistant", "content": r2.get("content") or "",
         "tool_calls": [{
             "id": tc2["id"], "type": "function",
             "function": {
                 "name": tc2["function"]["name"],
                 "arguments": {"city": "上海"},  # ← dict
             },
         }]},
        {"role": "tool", "tool_call_id": tc2["id"],
         "content": json.dumps({"weather": "雨 20度"})},
        {"role": "user", "content": "总结"},
    ]
    r3 = call_api(msgs, tools=[WEATHER_TOOL], tag="O-round3")
    print(f"[O-round3] ok={r3['ok']} status={r3['status']}")
    if not r3["ok"]:
        err = r3.get('error', '')
        print(f"  ❌ 错误: {err[:400]}")
        if 'reasoning_content' in err:
            return {"scenario": "O", "result": "REPRODUCED_REASONING_400"}
        return {"scenario": "O", "result": "OTHER_400", "error": err[:200]}
    print(f"  ✅ content: {(r3.get('content') or '')[:200]}")
    return {"scenario": "O", "result": "OK"}


def main() -> int:
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    results = []
    results.append(scenario_n_combined_bugs())
    results.append(scenario_o_just_dict_args_with_user_in_middle())

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    for r in results:
        print(f"  场景 {r['scenario']}: {r['result']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
