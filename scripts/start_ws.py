"""启动 WebSocket 服务器（用于端到端测试）"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from channels.websocket.server import WebSocketServer


async def main():
    server = WebSocketServer(port=8765)
    await server.start()
    print("=== WebSocket 服务器已启动 ===")
    print("ws://localhost:8765/ws")
    print("http://localhost:8765/health")
    print("按 Ctrl+C 退出")
    print("=" * 40)

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


if __name__ == "__main__":
    asyncio.run(main())
