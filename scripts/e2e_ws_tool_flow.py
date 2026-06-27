"""E2E WebSocket 测试：发消息触发工具调用，验证工具字段契约（按数据源划分）+ 消息加载。

完整链路：
1. 登录获取 token
2. 创建会话
3. 连接 WebSocket（/ws/chat?token=...）
4. 发送会触发工具调用的消息（如"读取文件 xxx"）
5. 接收流式事件：验证 tool_start/tool_result 实时事件字段为 snake_case
   （来自 bridge_events._handle_chunk，前端 toolHandler 双读对齐）
6. 验证 new_message/stream_end 的 parts[] tool_call 子项为 camelCase
   （来自 bridge_core._build_parts_from_state）
7. 轮询历史 messages：验证 toolCalls 子项为 camelCase（HTTP API 后端 alias）
8. 验证消息能正确加载（C3 loadPipelineMessages 底层 fetchMessages）

注：WS 协议的命名"混用"是确定性的——实时事件 snake_case、parts 子项 camelCase，
   二者按数据源划分，不是 bug。
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.request
import urllib.error

import websockets

BASE = "http://localhost:8988"
WS_BASE = "ws://localhost:8988"
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


def http(path: str, method: str = "GET", token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body else None
    r = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


async def run_ws_flow(token: str, thread_id: str, pipeline_id: str) -> None:
    """连接 WS、发消息、收集事件。"""
    uri = f"{WS_BASE}/ws/chat?token={token}"
    print(f"\n[WS] 连接 {WS_BASE}/ws/chat?token=...")

    # 触发工具调用的消息：让 agent 读一个不存在的文件，会触发 read_file 工具
    user_msg = {
        "type": "user_input",
        "thread_id": thread_id,
        "content": "请读取 /tmp/e2e_test_file.txt 文件的内容",
        "pipeline_id": pipeline_id,
        "attachments": [],
        "enable_thinking": False,
        "client_message_id": f"e2e-{int(time.time())}",
    }

    tool_events: list[dict] = []
    all_event_types: list[str] = []
    deadline = time.time() + 90  # 最长等 90 秒

    async with websockets.connect(uri, max_size=2**20, ping_interval=30) as ws:
        await ws.send(json.dumps(user_msg))
        print(f"[WS] 已发送消息: {user_msg['content'][:30]}...")

        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                print("[WS] 10s 无事件，检查是否完成...")
                # 检查是否已收到终态
                if any(t in ("state_change",) for t in all_event_types[-3:]):
                    break
                continue
            except websockets.ConnectionClosed:
                print("[WS] 连接关闭")
                break

            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "?")
            data = evt.get("data", {})
            all_event_types.append(etype)

            # 收集工具相关事件（C5 验证重点）
            if etype in ("tool_start", "tool_result", "stream_start", "stream_end", "new_message"):
                tool_events.append(evt)
                # 打印关键字段
                if etype == "tool_start":
                    keys = sorted(data.keys())
                    print(f"\n[EVENT] {etype}")
                    print(f"  data keys: {keys}")
                    print(f"  tool_name: {data.get('tool_name')!r}")
                    print(f"  call_id: {data.get('call_id')!r}")
                    print(f"  container_task_id: {data.get('container_task_id')!r}")
                    # 实时 WS 工具事件契约 = snake_case（来自 bridge_events._handle_chunk）。
                    # 与前端 toolHandler 的 `call_id || data?.call_id` 双读对齐。
                    # 注意：这里与 parts[] 子项（camelCase）刻意不同——按数据源划分。
                    has_snake = "tool_name" in data and "call_id" in data
                    status = "OK" if has_snake else "FAIL"
                    print(f"  >>> tool_start snake_case: [{status}] snake={has_snake}")
                elif etype == "tool_result":
                    print(f"\n[EVENT] {etype} tool_name={data.get('tool_name')!r} call_id={data.get('call_id')!r} duration_ms={data.get('duration_ms')!r}")
                elif etype in ("stream_end", "new_message"):
                    parts = data.get("parts", [])
                    tc_parts = [p for p in parts if isinstance(p, dict) and p.get("type") == "tool_call"]
                    for p in tc_parts:
                        print(f"\n[EVENT] {etype} part[tool_call]:")
                        print(f"  keys: {sorted(p.keys())}")
                        print(f"  name={p.get('name')!r} callId={p.get('callId')!r}")
                        # parts[] 子项来自 bridge_core._build_parts_from_state，契约 = camelCase。
                        # 这与实时 tool_start/tool_result 的 snake_case 刻意不同（按数据源划分）。
                        has_camel = "callId" in p and "name" in p
                        print(f"  >>> parts camelCase: [{'OK' if has_camel else 'FAIL'}]")
            elif etype in ("state_change", "error", "stream_error"):
                print(f"\n[EVENT] {etype}: {json.dumps(data, ensure_ascii=False)[:120]}")
                if etype == "state_change":
                    break

    return tool_events


def verify_history_messages(token: str, thread_id: str, pipeline_id: str) -> None:
    """轮询历史 messages，验证 HTTP API 后端 alias 输出 camelCase。"""
    print("\n--- 轮询历史 messages 验证 HTTP 后端 alias（camelCase）---")
    for attempt in range(15):
        code, data = http(
            f"/api/v1/threads/{thread_id}/messages?pipeline_run_id={pipeline_id}&limit=50",
            token=token,
        )
        if code != 200:
            print(f"  尝试{attempt}: HTTP {code}")
            time.sleep(2)
            continue
        msgs = data.get("messages", [])
        # 找有工具调用的消息
        tool_msgs = [m for m in msgs if m.get("toolCalls") or m.get("role") == "tool"]
        if tool_msgs:
            print(f"  找到 {len(tool_msgs)} 条工具相关消息（共 {len(msgs)} 条）")
            for m in tool_msgs[:3]:
                print(f"\n  消息 role={m.get('role')} id={m.get('id')}")
                print(f"    顶层 keys: {sorted(m.keys())}")
                if m.get("toolCalls"):
                    for tc in m["toolCalls"]:
                        print(f"    toolCalls 子项 keys: {sorted(tc.keys())}")
                        has_camel = "callId" in tc and "toolName" in tc
                        has_snake = "call_id" in tc or "tool_name" in tc
                        print(f"    >>> C5 后端alias camelCase: [{'OK' if has_camel else 'FAIL'}] snake残留={has_snake}")
                if m.get("role") == "tool":
                    # role=tool 的顶层字段也应是 camelCase
                    has_top = "toolName" in m and "toolCallId" in m
                    print(f"    >>> C5 顶层工具字段 camelCase: [{'OK' if has_top else 'FAIL'}] toolName={m.get('toolName')!r}")
            return
        print(f"  尝试{attempt}: {len(msgs)} 条消息，暂无工具调用，等待...")
        time.sleep(2)
    print("  [WARN] 60s 内未出现工具调用消息")


async def main() -> None:
    print("=" * 60)
    print("E2E WebSocket 工具调用 + C5 字段验证")
    print("=" * 60)

    # 1. 登录
    code, data = http("/api/v1/auth/login", "POST", body={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert code == 200, f"登录失败: {code}"
    token = data["access_token"]
    print("[OK] 登录成功")

    # 2. 创建会话
    code, data = http("/api/v1/threads", "POST", token=token,
                      body={"title": "E2E-WS工具字段", "intent": "测试工具调用camelCase"})
    assert code in (200, 201), f"创建会话失败: {code} {data}"
    thread_id = data["thread_id"]
    pipeline_id = (data.get("pipeline_ids") or [None])[0]
    print(f"[OK] 会话: {thread_id} 管道: {pipeline_id}")
    if not pipeline_id:
        time.sleep(2)
        _, data = http(f"/api/v1/threads/{thread_id}", token=token)
        pipeline_id = (data.get("pipeline_ids") or [None])[0]
    assert pipeline_id, "管道未就绪"

    # 3-5. WS 流程
    await run_ws_flow(token, thread_id, pipeline_id)

    # 6. 验证历史消息
    verify_history_messages(token, thread_id, pipeline_id)

    print("\n" + "=" * 60)
    print("E2E 完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
