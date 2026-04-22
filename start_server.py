"""统一服务器启动入口。

同时启动 FastAPI（含 API 和 WebSocket）服务。
将 WebSocket 服务器挂载到 FastAPI 应用中，通过同一端口提供服务。

用法：
    python start_server.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import os

# 将 src 目录加入 sys.path，确保模块可被正确导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from channels.api.app import create_app
from channels.api.auth import verify_token

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def create_combined_app() -> FastAPI:
    """创建合并了 WebSocket 功能的 FastAPI 应用。

    将 WebSocket 路由注册到 FastAPI 中，实现单端口统一服务。

    Returns:
        配置好的 FastAPI 应用实例
    """
    app = create_app()

    # WebSocket 连接管理
    active_connections: dict[str, list[WebSocket]] = {}

    @app.websocket("/ws")
    async def websocket_root(websocket: WebSocket) -> None:
        """处理根路径 WebSocket 连接。"""
        await websocket.accept()
        logger.info("WebSocket 连接已建立（根路径）")
        try:
            while True:
                data = await websocket.receive_text()
                await websocket.send_text(f"Echo: {data}")
        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开（根路径）")

    @app.websocket("/ws/{thread_id}")
    async def websocket_thread(websocket: WebSocket, thread_id: str) -> None:
        """处理线程 WebSocket 连接。

        支持可选的 token query 参数进行认证。

        Args:
            websocket: WebSocket 连接实例
            thread_id: 线程 ID
        """
        # 可选 token 验证
        token = websocket.query_params.get("token", "")
        if token:
            payload = verify_token(token)
            if payload is None:
                await websocket.close(code=4001, reason="Invalid or expired token")
                return

        await websocket.accept()

        # 管理连接
        if thread_id not in active_connections:
            active_connections[thread_id] = []
        active_connections[thread_id].append(websocket)

        logger.info("WebSocket 连接已建立: thread_id=%s", thread_id)
        try:
            while True:
                data = await websocket.receive_text()
                # 向同一线程的所有连接广播消息
                for conn in active_connections.get(thread_id, []):
                    try:
                        await conn.send_text(data)
                    except Exception:
                        pass
        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开: thread_id=%s", thread_id)
        finally:
            if thread_id in active_connections:
                active_connections[thread_id] = [
                    c for c in active_connections[thread_id] if c != websocket
                ]
                if not active_connections[thread_id]:
                    del active_connections[thread_id]

    @app.websocket("/ws/chat/{thread_id}")
    async def websocket_chat(websocket: WebSocket, thread_id: str) -> None:
        """处理聊天 WebSocket 连接（与 /ws/{thread_id} 功能一致）。

        Args:
            websocket: WebSocket 连接实例
            thread_id: 线程 ID
        """
        await websocket_thread(websocket, thread_id)

    return app


def main() -> None:
    """主函数，启动 uvicorn 服务器。"""
    logger.info("正在启动 Agent OS 服务器...")
    logger.info("API 地址: http://localhost:8888")
    logger.info("API 文档: http://localhost:8888/docs")
    logger.info("健康检查: http://localhost:8888/health")

    app = create_combined_app()
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")


if __name__ == "__main__":
    main()
