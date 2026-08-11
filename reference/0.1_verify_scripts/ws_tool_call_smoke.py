#!/usr/bin/env python3
"""后端 WS 工具调用链路冒烟验证（浏览器验证的前置检查）。

流程：
1. POST /api/v1/auth/login 获取 token（admin/admin12345）
2. 连接 ws://localhost:9100/ws/chat?token=...&version=3.0.0
3. 发送 user_input 消息："用计算工具算一下 5+3"
4. 收集 60 秒内的 WS 事件，检查是否出现 tool_start / tool_result

预期：
- 收到 connection_confirmation
- 收到 stream_start / stream_chunk / thinking_* 等
- 关键：收到 tool_start（tool_name=scientific_calculator）和 tool_result（success=true, result 含 8）
- 收到 new_message 收尾
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
import websockets

KERNEL = "http://localhost:9100"
WS_URL = "ws://localhost:9100/ws/chat"
USERNAME = "admin"
PASSWORD = "admin12345"
MESSAGE = "用计算工具算一下 5+3"


async def main() -> int:
    # 1. 登录
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            f"{KERNEL}/api/v1/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
        )
        print(f"[1] login status={r.status_code}")
        if r.status_code != 200:
            print(f"    body={r.text[:300]}")
            return 1
        token = r.json().get("access_token", "")
        print(f"    token={token[:20]}...")

    # 2. 连接 WS
    ws_url = f"{WS_URL}?token={token}&version=3.0.0"
    print(f"[2] connecting {ws_url[:80]}...")
    events: list[dict] = []
    tool_events: list[dict] = []

    try:
        async with websockets.connect(ws_url, open_timeout=15) as ws:
            # 收 connection_confirmation
            try:
                first = await asyncio.wait_for(ws.recv(), timeout=10)
                evt = json.loads(first)
                print(f"    first event type={evt.get('type')}")
                events.append(evt)
            except Exception as e:
                print(f"    [WARN] 未收到首事件: {e}")

            # 3. 发送 user_input
            thread_id = f"verify_{int(time.time())}"
            payload = {
                "type": "user_input",
                "content": MESSAGE,
                "thread_id": thread_id,
            }
            await ws.send(json.dumps(payload))
            print(f"[3] sent user_input thread_id={thread_id}")

            # 4. 收集事件（最多 60 秒）
            print("[4] collecting events (60s)...")
            start = time.monotonic()
            while time.monotonic() - start < 60:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    continue
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"    [WARN] 连接关闭: {e}")
                    break
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                etype = evt.get("type", "?")
                events.append(evt)
                if etype in ("tool_start", "tool_result"):
                    tool_events.append(evt)
                    print(f"    >>> TOOL EVENT: {etype}")
                    print(f"        keys={list(evt.keys())}")
                    data = evt.get("data", evt)
                    for k in ("tool_name", "call_id", "pipeline_id", "message_id",
                              "success", "result", "duration_ms"):
                        if k in data:
                            v = str(data[k])
                            print(f"        {k}={v[:120]}")
                if etype == "new_message":
                    print("    >>> new_message 收到（管道完成）")
                    break
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return 1

    # 5. 汇总
    print("\n" + "=" * 60)
    print("汇总:")
    print(f"  总事件数: {len(events)}")
    types = {}
    for e in events:
        t = e.get("type", "?")
        types[t] = types.get(t, 0) + 1
    for t, c in sorted(types.items()):
        print(f"    {t}: {c}")
    print(f"  工具事件数: {len(tool_events)}")
    tool_start = [e for e in tool_events if e.get("type") == "tool_start"]
    tool_result = [e for e in tool_events if e.get("type") == "tool_result"]
    if tool_start and tool_result:
        print("\n[PASS] 收到 tool_start + tool_result 事件")
        return 0
    if tool_start:
        print("\n[PARTIAL] 仅收到 tool_start，未收到 tool_result")
        return 2
    print("\n[FAIL] 未收到任何工具事件")
    return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
