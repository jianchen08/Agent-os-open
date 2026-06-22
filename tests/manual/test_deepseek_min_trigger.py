"""精确二分定位 reasoning_content 400 的最小触发条件。

已知：
- 测试 3（2 组 tool_calls + 中间 user）→ 复现
- 之前场景 F（1 组 tool_calls + 无 user）→ 不复现

二分：
A) 2 组 tool_calls + 中间 user（基线，已知复现）
B) 2 组 tool_calls + 中间用 assistant 消息（不是 user）
C) 2 组 tool_calls + 中间无消息（直接连接）
D) 1 组 tool_calls（多次）+ 中间 user
E) 2 组 tool_calls + 中间 user content="" 空字符串
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
        if not line or line.startswith("#") or "=":
            pass
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-pro"

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

TOOL = {"type": "function", "function": {
    "name": "search", "description": "搜索",
    "parameters": {"type": "object",
                   "properties": {"q": {"type": "string"}},
                   "required": ["q"]}}}


def call(messages, tag=""):
    payload = {"model": MODEL, "messages": messages,
               "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    payload["tools"] = [TOOL]
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload)
        dt = round(time.monotonic() - t0, 2)
        if r.status_code != 200:
            err = r.text[:200]
            is_rc = "reasoning_content" in err
            return {"ok": False, "status": r.status_code, "err": err, "is_rc": is_rc, "dt": dt}
        return {"ok": True, "status": 200, "dt": dt}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200], "is_rc": False, "dt": 0}


def tc(id_, q='{"q":"x"}'):
    return {"id": id_, "type": "function",
            "function": {"name": "search", "arguments": q}}


def tool_result(id_, content="result"):
    return {"role": "tool", "tool_call_id": id_, "content": content}


def assistant_with_tc(id_):
    return {"role": "assistant", "content": "",
            "tool_calls": [tc(id_)]}


# ── 各种最小结构 ──

def case_a_baseline():
    """A：2 组 tool_calls + 中间 user（已知复现）。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        {"role": "user", "content": "1"},  # ← 中间 user
        assistant_with_tc("call_2"),
        tool_result("call_2"),
    ]

def case_b_assistant_middle():
    """B：2 组 tool_calls + 中间 assistant（无 tool_calls）。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        {"role": "assistant", "content": "some text"},  # ← 中间 assistant（无 tool_calls）
        assistant_with_tc("call_2"),
        tool_result("call_2"),
    ]

def case_c_no_middle():
    """C：2 组 tool_calls 直接连接（中间无消息）。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        # 直接第二组
        assistant_with_tc("call_2"),
        tool_result("call_2"),
    ]

def case_d_1group_user():
    """D：1 组 tool_calls + 后面 user（对照组）。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        {"role": "user", "content": "1"},
    ]

def case_e_empty_user():
    """E：2 组 tool_calls + 中间 user content=''。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        {"role": "user", "content": ""},  # ← 空 user
        assistant_with_tc("call_2"),
        tool_result("call_2"),
    ]

def case_f_user_after_pair():
    """F：user 在完整配对之后（应该正常）。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        {"role": "user", "content": "继续"},  # 正常位置
    ]

def case_g_3groups():
    """G：3 组 tool_calls + 中间 2 个 user。"""
    return [
        {"role": "user", "content": "q1"},
        assistant_with_tc("call_1"),
        tool_result("call_1"),
        {"role": "user", "content": "1"},
        assistant_with_tc("call_2"),
        tool_result("call_2"),
        {"role": "user", "content": "2"},
        assistant_with_tc("call_3"),
        tool_result("call_3"),
    ]

def case_h_with_reasoning():
    """H：复刻 A 但给 assistant 补 reasoning_content（验证修复方案）。"""
    msgs = case_a_baseline()
    for m in msgs:
        if m.get("role") == "assistant":
            m["reasoning_content"] = "thinking..."
    return msgs


CASES = [
    ("A: 2组tc+中间user", case_a_baseline),
    ("B: 2组tc+中间assistant", case_b_assistant_middle),
    ("C: 2组tc+无中间", case_c_no_middle),
    ("D: 1组tc+后面user", case_d_1group_user),
    ("E: 2组tc+空user", case_e_empty_user),
    ("F: user在完整配对后", case_f_user_after_pair),
    ("G: 3组tc+中间user", case_g_3groups),
    ("H: A+补reasoning(修复)", case_h_with_reasoning),
]


def main():
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：精确定位 reasoning_content 400 的最小触发条件\n")

    for name, builder in CASES:
        msgs = builder()
        r = call(msgs, tag=name)
        status = "✅ OK" if r["ok"] else f"❌ {r['status']}"
        rc_flag = " [REASONING!]" if r.get("is_rc") else ""
        print(f"{name:35} → {status}{rc_flag}  ({r['dt']}s)")
        if not r["ok"] and not r.get("is_rc"):
            print(f"    其他错误: {r.get('err', '')[:150]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
