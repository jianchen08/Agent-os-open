"""DeepSeek thinking 模式 reasoning_content 回传策略实测脚本 v2。

v1 用 flash 简单查询没复现 400，怀疑是 reasoning 太短（39 tokens）。
v2 改用 v4-pro + 复杂编程任务（强制长思考），更接近真实 bug 现场。

测试场景：
A) 复杂任务 + 不传 reasoning_content → 是否 400？
B) 只传上一轮 vs 传所有轮（4 轮工具调用）
C) 截断版能否省 token
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
MODEL = "deepseek-v4-pro"  # 改用 pro，触发更深思考

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

# 复杂的代码审查任务，强制长思考
CODE_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取指定路径的文件内容",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
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
        usage = data.get("usage", {})
        return {
            "tag": tag, "ok": True, "status": 200,
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": msg.get("tool_calls"),
            "usage": usage,
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "elapsed": round(elapsed, 2),
        }
    except Exception as exc:
        return {
            "tag": tag, "ok": False, "status": -1,
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed": round(time.monotonic() - t0, 2),
        }


def make_tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def extract_assistant_msg(r: dict, *, include_reasoning: bool = True) -> dict:
    """从 API 响应构造 assistant 消息。"""
    msg = {"role": "assistant", "content": r.get("content") or ""}
    if include_reasoning and r.get("reasoning_content"):
        msg["reasoning_content"] = r["reasoning_content"]
    if r.get("tool_calls"):
        msg["tool_calls"] = [
            {
                "id": tc["id"], "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
            }
            for tc in r["tool_calls"]
        ]
    return msg


def run_scenario_a_complex_no_reasoning() -> dict:
    """场景 A：复杂编程任务 + 不传 reasoning_content。

    用户给一段有 bug 的代码，让模型审查并修复。这会触发深度思考。
    """
    print("\n" + "=" * 70)
    print("场景 A（v4-pro + 复杂任务）：不传 reasoning_content")
    print("=" * 70)

    buggy_code = """def binary_search(arr, target):
    left, right = 0, len(arr)
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid
        else:
            right = mid
    return -1
# 测试：binary_search([1,2,3,4,5], 3) 期望返回 2，实际死循环
"""
    r1 = call_api(
        [{"role": "user", "content": f"这段二分查找代码有死循环 bug，请先用 read_file 读取 /tmp/code.py 看一下，然后告诉我根因和修复方案。\n\n```\n{buggy_code}\n```"}],
        tools=[CODE_REVIEW_TOOL],
        tag="A-round1",
    )
    print(f"[A-round1] ok={r1['ok']} reasoning_tokens={r1.get('reasoning_tokens', 0)} "
          f"rc_len={len(r1.get('reasoning_content') or '')} has_tool_calls={bool(r1.get('tool_calls'))}")
    if not r1["ok"]:
        print(f"  错误: {r1.get('error', '')[:300]}")
        return {"scenario": "A", "result": "BLOCKED", "r1": r1}
    if not r1.get("tool_calls"):
        print("  round1 没调用工具，跳过")
        return {"scenario": "A", "result": "NO_TOOL_CALL", "r1": r1}

    tc1 = r1["tool_calls"][0]
    # 关键：故意丢掉 reasoning_content
    messages = [
        {"role": "user", "content": f"这段二分查找代码有死循环 bug..."},
        {
            "role": "assistant",
            "content": r1.get("content") or "",
            "tool_calls": [{
                "id": tc1["id"], "type": "function",
                "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
            }],
            # 故意不写 reasoning_content
        },
        make_tool_result(tc1["id"], buggy_code),
    ]
    r2 = call_api(messages, tools=[CODE_REVIEW_TOOL], tag="A-round2-no-reasoning")
    print(f"[A-round2] ok={r2['ok']} status={r2['status']}")
    if not r2["ok"]:
        print(f"  ❌ 错误（复现 bug！）: {r2.get('error', '')[:400]}")
        return {"scenario": "A", "result": "400_CONFIRMED_BUG_REPRODUCED", "r2": r2}
    print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
    return {"scenario": "A", "result": "UNEXPECTED_OK_NO_400", "r2": r2}


def run_scenario_b_4rounds_only_last() -> dict:
    """场景 B：4 轮工具调用，最后一轮只保留"上一轮" reasoning_content。

    对比 B1（只留上一轮）vs B2（留所有）的 token 消耗。
    """
    print("\n" + "=" * 70)
    print("场景 B：4 轮工具调用 - 只留上一轮 vs 留所有（对比 token）")
    print("=" * 70)

    # 4 个文件依次读取（强制多轮工具调用）
    files = {
        "/tmp/auth.py": "def login(user, pwd): return user == 'admin' and pwd == '123456'",
        "/tmp/db.py": "import sqlite3\nconn = sqlite3.connect('app.db')",
        "/tmp/routes.py": "from flask import Flask\napp = Flask(__name__)",
        "/tmp/utils.py": "def hash(s): return s  # 明文，未加密",
    }
    prompts = [
        "请先读取 /tmp/auth.py",
        "再读取 /tmp/db.py",
        "再读取 /tmp/routes.py",
        "最后读取 /tmp/utils.py，然后总结这些文件的安全问题",
    ]

    # ── B1: 只留上一轮 ──
    print("\n--- B1：只留上一轮 reasoning_content ---")
    messages = []
    all_reasonings = []  # 存每一轮的 (msg_idx, rc)
    tool_call_ids = []
    last_round_result = None

    for i, prompt in enumerate(prompts):
        messages.append({"role": "user", "content": prompt})
        r = call_api(messages, tools=[CODE_REVIEW_TOOL], tag=f"B1-round{i+1}")
        print(f"[B1-round{i+1}] ok={r['ok']} reasoning_tokens={r.get('reasoning_tokens', 0)} rc_len={len(r.get('reasoning_content') or '')}")
        if not r["ok"]:
            print(f"  错误: {r.get('error', '')[:300]}")
            return {"scenario": "B1", "result": "FAILED", "failed_round": i + 1, "r": r}
        if not r.get("tool_calls"):
            print(f"  round{i+1} 没调用工具，结束")
            last_round_result = r
            break

        tc = r["tool_calls"][0]
        tool_call_ids.append(tc["id"])
        # 关键：只保留上一轮的 reasoning_content，丢掉更早的
        # 做法：追加新 assistant 消息（带 rc），同时清除前面所有 assistant 的 rc
        new_assistant = extract_assistant_msg(r, include_reasoning=True)
        messages.append(new_assistant)
        # 清除更早的 reasoning_content
        for m in messages[:-1]:
            if m.get("role") == "assistant" and "reasoning_content" in m:
                del m["reasoning_content"]
        # 加工具结果
        path = tc["function"]["arguments"]
        try:
            args = json.loads(path)
            path = args.get("path", "")
        except Exception:
            pass
        messages.append(make_tool_result(tc["id"], files.get(path, "file not found")))
        last_round_result = r

    b1_usage = last_round_result.get("usage", {}) if last_round_result else {}
    print(f"[B1-final] prompt_tokens={b1_usage.get('prompt_tokens', 0)} total={b1_usage.get('total_tokens', 0)}")

    # ── B2: 留所有轮 ──
    print("\n--- B2：保留所有轮次 reasoning_content ---")
    messages2 = []
    last_round_result2 = None
    for i, prompt in enumerate(prompts):
        messages2.append({"role": "user", "content": prompt})
        r = call_api(messages2, tools=[CODE_REVIEW_TOOL], tag=f"B2-round{i+1}")
        print(f"[B2-round{i+1}] ok={r['ok']} reasoning_tokens={r.get('reasoning_tokens', 0)} rc_len={len(r.get('reasoning_content') or '')}")
        if not r["ok"]:
            print(f"  错误: {r.get('error', '')[:300]}")
            return {"scenario": "B2", "result": "FAILED", "failed_round": i + 1, "r": r}
        if not r.get("tool_calls"):
            last_round_result2 = r
            break
        tc = r["tool_calls"][0]
        # 保留所有 reasoning_content
        messages2.append(extract_assistant_msg(r, include_reasoning=True))
        path = tc["function"]["arguments"]
        try:
            args = json.loads(path)
            path = args.get("path", "")
        except Exception:
            pass
        messages2.append(make_tool_result(tc["id"], files.get(path, "file not found")))
        last_round_result2 = r

    b2_usage = last_round_result2.get("usage", {}) if last_round_result2 else {}
    print(f"[B2-final] prompt_tokens={b2_usage.get('prompt_tokens', 0)} total={b2_usage.get('total_tokens', 0)}")

    # 对比
    saved = b2_usage.get('prompt_tokens', 0) - b1_usage.get('prompt_tokens', 0)
    print(f"\n>>> B1（只留上一轮）vs B2（留所有）对比:")
    print(f"    B1 prompt_tokens: {b1_usage.get('prompt_tokens', 0)}")
    print(f"    B2 prompt_tokens: {b2_usage.get('prompt_tokens', 0)}")
    print(f"    差异: {saved} tokens ({'B2更多' if saved > 0 else 'B1更多或相等'})")

    return {
        "scenario": "B",
        "b1_result": "OK",
        "b2_result": "OK",
        "b1_prompt_tokens": b1_usage.get('prompt_tokens', 0),
        "b2_prompt_tokens": b2_usage.get('prompt_tokens', 0),
        "token_diff": saved,
    }


def run_scenario_d_truncated_complex() -> dict:
    """场景 D：复杂任务 + 截断 reasoning_content（看能否省 token）。"""
    print("\n" + "=" * 70)
    print("场景 D（v4-pro）：传截断版 reasoning_content")
    print("=" * 70)

    r1 = call_api(
        [{"role": "user", "content": "请先用 read_file 读取 /tmp/auth.py，然后分析它的安全问题"}],
        tools=[CODE_REVIEW_TOOL], tag="D-round1",
    )
    print(f"[D-round1] ok={r1['ok']} rc_len={len(r1.get('reasoning_content') or '')}")
    if not r1["ok"] or not r1.get("tool_calls"):
        return {"scenario": "D", "result": "BLOCKED", "r1": r1}

    tc1 = r1["tool_calls"][0]
    rc1_full = r1.get("reasoning_content") or ""
    # 截断到 100 字符
    rc1_truncated = (rc1_full[:100] + "...[truncated]") if len(rc1_full) > 100 else rc1_full
    print(f"  rc_full_len={len(rc1_full)} rc_truncated_len={len(rc1_truncated)}")

    messages = [
        {"role": "user", "content": "请先用 read_file 读取 /tmp/auth.py，然后分析它的安全问题"},
        {
            "role": "assistant", "content": r1.get("content") or "",
            "reasoning_content": rc1_truncated,  # 截断版
            "tool_calls": [{
                "id": tc1["id"], "type": "function",
                "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
            }],
        },
        make_tool_result(tc1["id"], "def login(user, pwd): return user == 'admin' and pwd == '123456'"),
    ]
    r2 = call_api(messages, tools=[CODE_REVIEW_TOOL], tag="D-round2-truncated")
    print(f"[D-round2] ok={r2['ok']} status={r2['status']}")
    if r2["ok"]:
        print(f"  ✅ content: {(r2.get('content') or '')[:200]}")
        return {"scenario": "D", "result": "TRUNCATED_OK", "r2": r2}
    print(f"  ❌ 错误: {r2.get('error', '')[:400]}")
    return {"scenario": "D", "result": "TRUNCATED_REJECTED_400", "r2": r2}


def main() -> int:
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"thinking: enabled, reasoning_effort: max")
    print(f"API_KEY: {API_KEY[:10]}...{API_KEY[-4:]}")

    results = []
    results.append(run_scenario_a_complex_no_reasoning())
    results.append(run_scenario_b_4rounds_only_last())
    results.append(run_scenario_d_truncated_complex())

    print("\n" + "=" * 70)
    print("测试总结")
    print("=" * 70)
    for r in results:
        result_str = r.get("result", "?")
        extra = ""
        if "b1_prompt_tokens" in r:
            extra = f" | B1={r['b1_prompt_tokens']}t B2={r['b2_prompt_tokens']}t diff={r['token_diff']}t"
        print(f"  场景 {r['scenario']}: {result_str}{extra}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
