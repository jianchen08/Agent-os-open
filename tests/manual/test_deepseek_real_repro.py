"""DeepSeek 终极复现 - 用真实日志中的完整 messages 原样发送。

从 logs/pipeline/pipeline_a616a6a76609.log 提取迭代 3 实际发送的 11 条 messages，
原样（一字不改）发给 DeepSeek API，看是否复现 reasoning_content 错误。

如果复现 → 我们就能精确定位到底是哪条消息触发的
如果不复现 → 说明是项目代码层（litellm 调用前）的问题
"""

from __future__ import annotations

import json
import os
import re
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


def call_raw_api(messages: list[dict], *, tools=None, tag: str = "") -> dict:
    """原样发送，不做任何修改。"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
        "max_tokens": 100000,
        "temperature": 0.7,
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
                "error": resp.text[:500], "elapsed": round(elapsed, 2),
            }
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        return {
            "tag": tag, "ok": True, "status": 200,
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": msg.get("tool_calls"),
            "elapsed": round(elapsed, 2),
        }
    except Exception as exc:
        return {"tag": tag, "ok": False, "status": -1,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.monotonic() - t0, 2)}


def build_real_messages_from_log() -> tuple[list[dict], list[dict]]:
    """从真实日志构造完整的 11 条 messages。

    使用 logs/pipeline/pipeline_a616a6a76609.log 迭代 3 的实际内容。
    """
    # MSG-0 system（精简版，但保留结构）
    system_msg = {"role": "system", "content": "你是灵汐，一个智能助理。负责理解需求、调度任务。"}

    # MSG-1 user（原文）
    user_msg = {"role": "user", "content":
        "我想将你也就是agentos这个项目拿去申请专利，参加比赛"
        "（https://www.trae.cn/ai-creativity 和鸿蒙的开发者大赛）"
        "还有就是求职和开源，帮我搜索整理材料"}

    # MSG-2 assistant tool_calls（3 个并行）— 用真实日志里的 id 和 arguments
    msg2 = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_6a31036b0122491ea5ef4395", "type": "function",
             "function": {"name": "file_read",
                          "arguments": '{"path": "D:\\\\myproject\\\\container_224042d3b925"}'}},
            {"id": "call_5be83ecc4d1b4be88a8b643b", "type": "function",
             "function": {"name": "enhanced_search",
                          "arguments": '{"query": ".", "path": "D:\\\\myproject\\\\container_224042d3b925\\\\.project", "file_pattern": "*"}'}},
            {"id": "call_76b898da92854e19b3372f89", "type": "function",
             "function": {"name": "resource_search",
                          "arguments": '{"resource_type": "tool", "query": "web_search", "mode": "detailed"}'}},
        ],
    }

    # MSG-3,4,5 tool 结果（用真实日志中的内容摘要）
    msg3 = {"role": "tool", "tool_call_id": "call_6a31036b0122491ea5ef4395",
            "content": "{'success': False, 'error': '路径不是文件'}"}
    msg4 = {"role": "tool", "tool_call_id": "call_5be83ecc4d1b4be88a8b643b",
            "content": "{'output': {'match_count': 90, 'files': ['constraints.md', 'domain_model.md']}}"}
    msg5 = {"role": "tool", "tool_call_id": "call_76b898da92854e19b3372f89",
            "content": "{'output': {'tool_c': 1, 'message': 'web_search 已找到'}}"}

    # MSG-6 user（异常注入 - 这是日志里真实存在的）
    msg6 = {"role": "user", "content": "1"}

    # MSG-7 assistant tool_calls（3 个并行）— 第二组
    msg7 = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_5cf9d62010e7478da4260e73", "type": "function",
             "function": {"name": "web_search",
                          "arguments": '{"query": "Trae AI创意大赛 2026", "max_results": 5, "search_mode": "full"}'}},
            {"id": "call_de90e0c96a4c4b0dbcdbcbda", "type": "function",
             "function": {"name": "web_search",
                          "arguments": '{"query": "鸿蒙开发者大赛 2026", "max_results": 5, "search_mode": "full"}'}},
            {"id": "call_27397b2f090148ad9f656359", "type": "function",
             "function": {"name": "enhanced_search",
                          "arguments": '{"query": "README|LICENSE", "path": "D:\\\\myproject", "search_type": "filename"}'}},
        ],
    }

    # MSG-8,9,10 tool 结果
    msg8 = {"role": "tool", "tool_call_id": "call_5cf9d62010e7478da4260e73",
            "content": "{'output': {'results': [{'title': 'TRAE', 'url': 'https://www.trae.cn/'}]}}"}
    msg9 = {"role": "tool", "tool_call_id": "call_de90e0c96a4c4b0dbcdbcbda",
            "content": "{'output': {'results': [{'title': 'HarmonyOS', 'url': 'https://consumer.huawei.com/'}]}}"}
    msg10 = {"role": "tool", "tool_call_id": "call_27397b2f090148ad9f656359",
             "content": "{'output': {'d': [], 'c': 0}}"}

    messages = [system_msg, user_msg, msg2, msg3, msg4, msg5, msg6, msg7, msg8, msg9, msg10]

    # 工具定义（与项目实际使用的对齐）
    tools = [
        {"type": "function", "function": {
            "name": "file_read", "description": "读取文件",
            "parameters": {"type": "object",
                           "properties": {"path": {"type": "string"}},
                           "required": ["path"]}}},
        {"type": "function", "function": {
            "name": "enhanced_search", "description": "搜索",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "resource_search", "description": "资源搜索",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
        {"type": "function", "function": {
            "name": "web_search", "description": "网络搜索",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string"}},
                           "required": ["query"]}}},
    ]
    return messages, tools


def test_real_structure_original() -> dict:
    """测试 1：完全复刻日志结构（不传 reasoning_content）。"""
    print("\n" + "=" * 70)
    print("测试 1：完整复刻日志迭代 3 的 11 条消息（不传 reasoning_content）")
    print("=" * 70)
    messages, tools = build_real_messages_from_log()
    print(f"消息数: {len(messages)}")
    r = call_raw_api(messages, tools=tools, tag="real-structure-no-rc")
    print(f"结果: ok={r['ok']} status={r['status']}")
    if not r["ok"]:
        err = r.get('error', '')
        print(f"  错误: {err[:400]}")
        if 'reasoning_content' in err:
            print("  >>> 复现 reasoning_content 错误！")
            return {"test": "real-no-rc", "result": "REASONING_400_REPRODUCED"}
    else:
        print(f"  ✅ content: {(r.get('content') or '')[:150]}")
    return {"test": "real-no-rc", "result": "OK" if r["ok"] else "OTHER_400"}


def test_real_structure_with_reasoning() -> dict:
    """测试 2：复刻日志结构 + 给 MSG-2/MSG-7 补 reasoning_content。"""
    print("\n" + "=" * 70)
    print("测试 2：复刻结构 + 补 reasoning_content")
    print("=" * 70)
    messages, tools = build_real_messages_from_log()
    # 给两条 assistant 消息补 reasoning_content
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            m["reasoning_content"] = "让我分析一下用户的需求，需要查询相关资料。"
    r = call_raw_api(messages, tools=tools, tag="real-structure-with-rc")
    print(f"结果: ok={r['ok']} status={r['status']}")
    if not r["ok"]:
        print(f"  错误: {r.get('error', '')[:400]}")
    else:
        print(f"  ✅ content: {(r.get('content') or '')[:150]}")
    return {"test": "real-with-rc", "result": "OK" if r["ok"] else "FAILED"}


def test_minimal_repro() -> dict:
    """测试 3：最小化复刻 - 只保留核心结构。"""
    print("\n" + "=" * 70)
    print("测试 3：最小化结构（2 组 tool_calls + 中间 user）")
    print("=" * 70)

    # 最小化版本
    messages = [
        {"role": "user", "content": "查资料"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_aaa", "type": "function",
                         "function": {"name": "web_search", "arguments": '{"query": "test"}'}}]},
        {"role": "tool", "tool_call_id": "call_aaa", "content": "结果1"},
        {"role": "user", "content": "1"},  # 异常注入
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_bbb", "type": "function",
                         "function": {"name": "web_search", "arguments": '{"query": "test2"}'}}]},
        {"role": "tool", "tool_call_id": "call_bbb", "content": "结果2"},
    ]
    tools = [{"type": "function", "function": {
        "name": "web_search", "description": "网络搜索",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string"}},
                       "required": ["query"]}}}]
    r = call_raw_api(messages, tools=tools, tag="minimal-repro")
    print(f"结果: ok={r['ok']} status={r['status']}")
    if not r["ok"]:
        err = r.get('error', '')
        print(f"  错误: {err[:400]}")
        if 'reasoning_content' in err:
            print("  >>> 最小化也复现了！")
            return {"test": "minimal", "result": "REASONING_400"}
    else:
        print(f"  ✅ content: {(r.get('content') or '')[:150]}")
    return {"test": "minimal", "result": "OK" if r["ok"] else "OTHER_400"}


def main() -> int:
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：用真实日志结构精确复现 reasoning_content 错误")

    results = []
    results.append(test_real_structure_original())
    results.append(test_real_structure_with_reasoning())
    results.append(test_minimal_repro())

    print("\n" + "=" * 70)
    print("最终总结")
    print("=" * 70)
    for r in results:
        print(f"  {r['test']}: {r['result']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
