"""端到端测试 WebSocket 服务器（带 Echo 回显管道）

模拟完整的后端管道：
1. 接收用户消息
2. 发送 stream_start
3. 逐字发送 stream_chunk
4. 发送 stream_end
5. 发送 pipeline_end
"""
import sys
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from channels.websocket.server import WebSocketServer
from channels.websocket.protocol import EventType, create_event


async def echo_handler(session_id: str, parsed_message: dict) -> None:
    """Echo 回显处理器：收到消息后逐字回复"""
    global server

    msg_type = parsed_message.get("type", "")
    data = parsed_message.get("data", {})

    if msg_type != "user_message":
        print(f"  [忽略] 非用户消息: {msg_type}")
        return

    content = data.get("content", "")
    thread_id = data.get("thread_id", "")
    print(f"  收到用户消息: {content!r} (thread={thread_id})")

    reply = f"Echo: {content} — 你好！我是 Agent OS 测试回显。你的消息已成功送达后端并处理。"

    # 1. 发送 stream_start
    event = create_event(EventType.STREAM_START, {"thread_id": thread_id})
    await server.send_event(session_id, event)
    print("  → stream_start")

    # 2. 逐字发送 stream_chunk
    for char in reply:
        chunk_event = create_event(EventType.STREAM_CHUNK, {
            "thread_id": thread_id,
            "content": char,
        })
        await server.send_event(session_id, chunk_event)
        await asyncio.sleep(0.02)  # 模拟流式输出

    print(f"  → stream_chunk x {len(reply)}")

    # 3. 发送 stream_end
    end_event = create_event(EventType.STREAM_END, {
        "thread_id": thread_id,
        "full_content": reply,
    })
    await server.send_event(session_id, end_event)
    print("  → stream_end")

    # 4. 发送 pipeline_end
    pipeline_end = create_event(EventType.PIPELINE_END, {
        "thread_id": thread_id,
        "status": "completed",
    })
    await server.send_event(session_id, pipeline_end)
    print("  → pipeline_end")


async def main():
    global server
    server = WebSocketServer(port=8765)
    server.on_message = echo_handler
    await server.start()

    print("=" * 50)
    print("端到端测试 WebSocket 服务器已启动")
    print("ws://localhost:8765/ws")
    print("http://localhost:8765/health")
    print("模式: Echo 回显（模拟流式输出）")
    print("按 Ctrl+C 退出")
    print("=" * 50)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
