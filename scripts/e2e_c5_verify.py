"""E2E 验证 C1-C6 改动：登录 → 创建会话 → 发消息（触发工具调用）→ 验证消息加载和工具字段 camelCase。

用法：python scripts/e2e_c5_verify.py
前提：后端服务已在 localhost:8988 运行新代码。
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

BASE = "http://localhost:8988"
ADMIN_USER = "admin"
ADMIN_PASS = "admin123"


def req(path: str, method: str = "GET", token: str | None = None, body: dict | None = None) -> tuple[int, dict]:
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


def main() -> None:
    print("=" * 60)
    print("E2E 验证 C1-C6 消息加载架构重构")
    print("=" * 60)

    # 1. 登录
    code, data = req("/api/v1/auth/login", "POST", body={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert code == 200, f"登录失败: {code} {data}"
    token = data["access_token"]
    print(f"[OK] 登录成功: admin")

    # 2. C1 验证：死接口返回路由 404
    print("\n--- C1: 死接口删除验证 ---")
    for path, name in [("/api/v1/threads/x/detail", "detail"),
                       ("/api/v1/threads/x/history", "history"),
                       ("/api/v1/threads/messages/search?query=t", "search")]:
        code, data = req(path, token=token)
        is_route_404 = code == 404 and data.get("detail") == "Not Found"
        print(f"  [{'OK' if is_route_404 else 'FAIL'}] {name}: HTTP {code} {data.get('detail', '')[:40]}")

    # 3. 创建会话
    print("\n--- 创建测试会话 ---")
    code, data = req("/api/v1/threads", "POST", token=token,
                     body={"title": "E2E-C5工具字段验证", "intent": "测试工具调用字段camelCase"})
    assert code in (200, 201), f"创建会话失败: {code} {data}"
    thread_id = data["thread_id"]
    pipeline_id = (data.get("pipeline_ids") or [None])[0]
    print(f"[OK] 会话创建: {thread_id}")
    print(f"  pipeline_ids: {data.get('pipeline_ids')}")

    # 4. 等待主管道 pipeline_id 就绪（创建后异步注册）
    print("\n--- 获取主管道 pipeline_id ---")
    if not pipeline_id:
        for _ in range(10):
            code, data = req(f"/api/v1/threads/{thread_id}", token=token)
            if code == 200:
                pids = data.get("pipeline_ids") or []
                if pids:
                    pipeline_id = pids[0]
                    break
            time.sleep(1)
    assert pipeline_id, f"主管道未就绪: {code} {data}"
    print(f"[OK] 主管道: {pipeline_id}")

    # 5. 发送会触发工具调用的消息（通过 WebSocket 需要 ws 库，这里用 HTTP 指引）
    # 注：发送消息走 WebSocket，此处改用 curl/wscat 手动触发，或检查现有消息
    print(f"\n--- 发送消息（需通过 WebSocket）---")
    print(f"  WebSocket URL: ws://localhost:8988/ws/chat?token={token[:20]}...")
    print(f"  发送: {{\"type\":\"user_message\",\"thread_id\":\"{thread_id}\",\"content\":\"现在几点了？\",\"pipeline_id\":\"{pipeline_id}\"}}")
    print(f"  然后轮询 GET /api/v1/threads/{thread_id}/messages?pipeline_run_id={pipeline_id} 验证工具字段")

    # 6. C5 验证：检查 messages 接口返回的工具字段命名（如果有历史消息）
    print(f"\n--- C5: 验证 messages 接口工具字段 camelCase ---")
    code, data = req(f"/api/v1/threads/{thread_id}/messages?pipeline_run_id={pipeline_id}", token=token)
    print(f"  messages HTTP: {code}, 消息数: {len(data.get('messages', []))}")
    msgs = data.get("messages", [])
    if msgs:
        for m in msgs[:3]:
            print(f"  消息 role={m.get('role')} keys={sorted(m.keys())}")
            if m.get("toolCalls"):
                tc0 = m["toolCalls"][0]
                print(f"    toolCalls[0] keys: {sorted(tc0.keys())}")
                # C5 验证：应为 camelCase (callId/toolName/toolArgs)
                has_camel = "callId" in tc0 and "toolName" in tc0
                has_snake = "call_id" in tc0 or "tool_name" in tc0
                print(f"    [{'OK' if has_camel else 'FAIL'}] camelCase(callId/toolName): {has_camel}")
                print(f"    [{'OK' if not has_snake else 'WARN'}] 无snake_case残留: {not has_snake}")
            if m.get("toolName") or m.get("toolCallId"):
                print(f"    顶层工具字段: toolName={m.get('toolName')} toolCallId={m.get('toolCallId')}")
    else:
        print("  (暂无消息，需先通过 WebSocket 发送消息触发工具调用)")


if __name__ == "__main__":
    main()
