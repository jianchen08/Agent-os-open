"""端到端 WebSocket 流式桥接测试。

验证重构后的 PipelineStreamBridge 能正确完成：
1. WebSocket 连接建立
2. 用户消息发送
3. stream_start 事件（含 pipeline_id）
4. stream_chunk / thinking_chunk 流式事件
5. stream_end 事件
6. new_message 最终消息
"""
import asyncio
import json
import sys

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed")
    sys.exit(1)

MAX_GLOBAL_TIMEOUT = 120


async def e2e_test():
    thread_id = "e2e-stream-bridge-test"
    uri = f"ws://localhost:8888/ws/{thread_id}"
    print("=== E2E WebSocket 流式桥接测试 ===")
    print(f"连接: {uri}")

    async with websockets.connect(uri) as ws:
        print("OK: WebSocket 连接成功")

        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        data = json.loads(msg)
        event_type = data.get("type", "")
        print(f"OK: 收到 {event_type}")
        assert event_type == "connection_confirmation", (
            f"期望 connection_confirmation，收到 {event_type}"
        )

        user_msg = json.dumps({
            "type": "user_input",
            "data": {
                "content": "回复两个字：你好",
            },
        })
        await ws.send(user_msg)
        print("OK: 发送用户消息")

        events = []
        event_counts = {}
        full_content = ""
        thinking_content = ""

        done = asyncio.get_event_loop().time() + MAX_GLOBAL_TIMEOUT

        while asyncio.get_event_loop().time() < done:
            remaining = done - asyncio.get_event_loop().time()
            if remaining <= 0:
                print(f"  TIMEOUT: 全局超时 {MAX_GLOBAL_TIMEOUT}s")
                break

            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 3))
                data = json.loads(raw)
                etype = data.get("type", "")
                events.append(etype)
                event_counts[etype] = event_counts.get(etype, 0) + 1

                if etype == "stream_start":
                    pid = data.get("data", {}).get("session_id", "?")
                    print(f"  OK: stream_start (pipeline_id={pid})")
                elif etype == "stream_chunk":
                    content = data.get("data", {}).get("content", "")
                    full_content += content
                elif etype == "thinking_chunk":
                    content = data.get("data", {}).get("content", "")
                    thinking_content += content
                elif etype == "stream_end":
                    fc = data.get("data", {}).get("full_content", "")
                    print(f"  OK: stream_end (full_content_len={len(fc)})")
                elif etype == "new_message":
                    msg_content = data.get("data", {}).get("content", "")
                    print(f"  OK: new_message (content_len={len(msg_content)})")
                elif etype == "heartbeat_ack":
                    pass
                elif etype == "thinking_start":
                    print(f"  OK: thinking_start")
                elif etype == "thinking_end":
                    print(f"  OK: thinking_end")
                elif etype == "tool_start":
                    tn = data.get("data", {}).get("tool_name", "?")
                    print(f"  OK: tool_start (tool={tn})")
                elif etype == "tool_result":
                    tn = data.get("data", {}).get("tool_name", "?")
                    print(f"  OK: tool_result (tool={tn})")
                elif etype == "iteration":
                    it = data.get("data", {}).get("iteration", "?")
                    print(f"  OK: iteration #{it}")

                if etype == "new_message":
                    break

            except asyncio.TimeoutError:
                continue

        print()
        print("=== 测试结果 ===")
        print(f"事件计数: {json.dumps(event_counts, indent=2)}")
        print(f"stream_chunk 累积内容 ({len(full_content)}字): {full_content[:200]}")
        print(f"thinking_chunk 累积内容 ({len(thinking_content)}字)")

        checks = {
            "stream_start": "缺少 stream_start 事件 — PipelineStreamBridge 未发送流式开始",
            "stream_end": "缺少 stream_end 事件 — drain_loop 未正常结束",
            "new_message": "缺少 new_message 事件 — 最终消息未发送",
        }

        all_pass = True
        for evt, err in checks.items():
            if evt in events:
                print(f"  PASS: {evt}")
            else:
                print(f"  FAIL: {err}")
                all_pass = False

        if event_counts.get("stream_chunk", 0) > 0 or event_counts.get("thinking_chunk", 0) > 0:
            print(f"  PASS: 流式传输正常 (stream_chunk={event_counts.get('stream_chunk', 0)}, thinking_chunk={event_counts.get('thinking_chunk', 0)})")
        else:
            print(f"  FAIL: 没有收到任何流式内容事件")
            all_pass = False

        if all_pass:
            print()
            print("=== E2E WebSocket 流式桥接测试通过 ===")
        else:
            print()
            print("=== E2E WebSocket 流式桥接测试失败 ===")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(e2e_test())
