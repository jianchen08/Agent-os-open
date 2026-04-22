"""统一服务器启动入口。

同时启动 FastAPI（含 API 和 WebSocket）服务。
将 WebSocket 服务器挂载到 FastAPI 应用中，通过同一端口提供服务。

用法：
    python start_server.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import os
import uuid
from datetime import datetime, timezone

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

    def _generate_simulated_reply(user_content: str) -> str:
        """根据用户输入内容生成模拟 AI 回复。

        Args:
            user_content: 用户发送的文本内容

        Returns:
            模拟的 AI 回复文本
        """
        text = user_content.strip().lower()
        if text in ("你好", "hello", "hi", "hey", "嗨"):
            return (
                "你好！我是 Agent OS 助手，很高兴为你服务。\n\n"
                "有什么我可以帮助你的吗？"
            )
        if text in ("你是谁", "who are you"):
            return (
                "我是 Agent OS 的 AI 助手。\n\n"
                "我可以回答问题、提供建议和协助完成各种任务。"
            )
        if any(kw in text for kw in ("帮助", "help", "能做什么")):
            return (
                "我可以帮助你完成以下任务：\n\n"
                "1. 回答各类问题\n"
                "2. 提供技术建议\n"
                "3. 协助代码开发\n"
                "4. 数据分析和处理\n\n"
                "请告诉我你需要什么帮助！"
            )
        # 默认回复：回显用户消息
        return f"我收到了你的消息：{user_content}"

    async def _stream_ai_response(
        websocket: WebSocket,
        user_content: str,
        message_id: str,
        stop_event: asyncio.Event,
        session_id: str,
    ) -> None:
        """异步发送 AI 流式回复。

        按照流式协议依次发送 stream_start → stream_chunk(逐字) → stream_end → new_message。
        当 stop_event 被设置时，立即中断流式输出。

        Args:
            websocket: WebSocket 连接实例
            user_content: 用户发送的原始文本
            message_id: 本轮回复的消息 UUID
            stop_event: 用于取消流式生成的事件对象
            session_id: 当前线程/会话 ID
        """
        full_content = _generate_simulated_reply(user_content)

        # ---- stream_start ----
        await websocket.send_text(json.dumps({
            "type": "stream_start",
            "data": {
                "message_id": message_id,
                "session_id": session_id,
            },
        }, ensure_ascii=False))

        # ---- stream_chunk 逐字发送 ----
        sent_chars: list[str] = []
        for char in full_content:
            if stop_event.is_set():
                logger.info("流式生成被用户中断: message_id=%s", message_id)
                break
            sent_chars.append(char)
            await websocket.send_text(json.dumps({
                "type": "stream_chunk",
                "data": {
                    "content": char,
                    "message_id": message_id,
                },
            }, ensure_ascii=False))
            # 模拟逐字打字延迟
            await asyncio.sleep(0.05)

        actual_content = "".join(sent_chars)

        # ---- stream_end ----
        await websocket.send_text(json.dumps({
            "type": "stream_end",
            "data": {
                "message_id": message_id,
                "full_content": actual_content,
            },
        }, ensure_ascii=False))

        # ---- new_message 最终消息 ----
        await websocket.send_text(json.dumps({
            "type": "new_message",
            "data": {
                "id": message_id,
                "role": "assistant",
                "content": actual_content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence": 1,
            },
        }, ensure_ascii=False))

    @app.websocket("/ws/{thread_id}")
    async def websocket_thread(websocket: WebSocket, thread_id: str) -> None:
        """处理线程 WebSocket 连接，支持 AI 流式回复。

        支持可选的 token query 参数进行认证。
        处理前端发送的 user_input / heartbeat / stop_generation 消息类型，
        并通过 stream_start → stream_chunk → stream_end → new_message 协议回复。

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

        # 发送连接确认
        await websocket.send_text(json.dumps({
            "type": "connection_confirmation",
            "data": {
                "thread_id": thread_id,
                "status": "connected",
            },
        }, ensure_ascii=False))

        logger.info("WebSocket 连接已建立: thread_id=%s", thread_id)

        # 当前流式生成任务和取消事件
        current_stream_task: asyncio.Task | None = None
        stop_event = asyncio.Event()

        try:
            while True:
                data = await websocket.receive_text()

                # 尝试解析 JSON，兼容纯文本消息
                try:
                    message = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    message = {"type": "user_input", "content": data}

                msg_type = message.get("type", "")

                # ---- 心跳响应 ----
                if msg_type == "heartbeat":
                    await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
                    continue

                # ---- 停止生成 ----
                if msg_type == "stop_generation":
                    stop_event.set()
                    if current_stream_task and not current_stream_task.done():
                        current_stream_task.cancel()
                        try:
                            await current_stream_task
                        except asyncio.CancelledError:
                            pass
                    logger.info("用户请求停止生成: thread_id=%s", thread_id)
                    continue

                # ---- 用户输入：启动流式回复 ----
                if msg_type == "user_input":
                    # 提取用户文本内容
                    user_content = (
                        message.get("data", {}).get("content")
                        if isinstance(message.get("data"), dict)
                        else message.get("content", "")
                    )
                    if not user_content:
                        continue

                    # 若上一轮流式回复尚未结束，先取消
                    stop_event.set()
                    if current_stream_task and not current_stream_task.done():
                        current_stream_task.cancel()
                        try:
                            await current_stream_task
                        except asyncio.CancelledError:
                            pass

                    # 重置取消事件，启动新的流式任务
                    stop_event = asyncio.Event()
                    message_id = str(uuid.uuid4())
                    current_stream_task = asyncio.create_task(
                        _stream_ai_response(
                            websocket, user_content, message_id, stop_event, thread_id
                        )
                    )
                    continue

                # 未知消息类型，忽略
                logger.debug("收到未处理的消息类型: %s", msg_type)

        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开: thread_id=%s", thread_id)
        finally:
            # 连接断开时取消进行中的流式任务
            stop_event.set()
            if current_stream_task and not current_stream_task.done():
                current_stream_task.cancel()
            if thread_id in active_connections:
                active_connections[thread_id] = [
                    c for c in active_connections[thread_id] if c != websocket
                ]
                if not active_connections[thread_id]:
                    del active_connections[thread_id]

    @app.websocket("/ws/chat/{thread_id}")
    async def websocket_chat(websocket: WebSocket, thread_id: str) -> None:
        """处理聊天 WebSocket 连接，复用 websocket_thread 的流式回复逻辑。

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
