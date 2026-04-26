"""端到端 WebSocket 测试：模拟前端连接后端"""
import sys
import asyncio
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp not installed")
    sys.exit(1)


async def test_websocket():
    uri = "http://localhost:8765/ws"

    print(f"=== 连接后端 WebSocket: {uri} ===")

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri) as ws:
                print("✅ WebSocket 连接成功!")

                # 等待 connection_confirmation 事件
                print("\n等待 connection_confirmation...")
                msg = await asyncio.wait_for(ws.receive_json(), timeout=5)
                print(f"✅ 收到事件: {json.dumps(msg, ensure_ascii=False, indent=2)}")

                # 发送一条消息（模拟用户输入）
                user_message = {
                    "type": "user_message",
                    "data": {
                        "content": "你好，请介绍一下你自己",
                        "thread_id": "test-thread-001",
                    },
                }
                print(f"\n>>> 发送消息: {user_message['data']['content']}")
                await ws.send_json(user_message)

                # 等待响应（最多 10 秒）
                print("\n等待回复...")
                response_count = 0
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            event_type = data.get("type", "unknown")
                            response_count += 1

                            if event_type == "stream_chunk":
                                content = data.get("data", {}).get("content", "")
                                print(f"  [chunk] {content}", end="", flush=True)
                            elif event_type == "stream_start":
                                print("  [stream_start]", flush=True)
                            elif event_type == "stream_end":
                                print("\n  [stream_end]", flush=True)
                            elif event_type == "pipeline_end":
                                print(f"  [pipeline_end]", flush=True)
                                break
                            else:
                                print(f"\n  [{event_type}] {json.dumps(data, ensure_ascii=False)[:200]}")

                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            print(f"  ❌ WebSocket 错误: {ws.exception()}")
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            print("  连接关闭")
                            break

                        if response_count > 50:
                            print("\n  (已收到 50 条消息，停止等待)")
                            break

                except asyncio.TimeoutError:
                    print("\n  ⚠️ 等待响应超时（10秒）")

                if response_count == 0:
                    print("  ⚠️ 未收到任何响应消息")
                else:
                    print(f"\n✅ 总共收到 {response_count} 条响应消息")

                await ws.close()
                print("\n=== 测试完成 ===")

    except aiohttp.ClientConnectorError as e:
        print(f"❌ 连接失败: {e}")
    except Exception as e:
        print(f"❌ 测试异常: {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(test_websocket())
