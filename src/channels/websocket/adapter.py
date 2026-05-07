"""WebSocket 通道适配器。

实现 IInputAdapter 和 IOutputAdapter 接口，将 WebSocket 连接
适配为管道引擎可用的输入/输出通道。

核心职责：
- 输入适配：从 WebSocket 接收前端消息，转换为管道初始 state
- 输出适配：将管道结果和流式 chunk 通过 WebSocket 推送回前端
- 执行控制：处理停止/审批等控制信号
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any

from channels.input_adapter import IInputAdapter
from channels.output_adapter import IOutputAdapter
from channels.websocket.protocol import (
    ErrorData,
    EventEnvelope,
    EventType,
    PipelineEndData,
    PipelineStartData,
    StreamChunkData,
    StreamEndData,
    StreamStartData,
    create_event,
)
from channels.websocket.server import WebSocketServer
from channels.websocket.session_manager import SessionManager
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)


class WebSocketInputAdapter(IInputAdapter):
    """WebSocket 输入适配器。

    从 WebSocket 接收前端消息，将其转换为管道引擎的初始 state。
    支持处理用户输入和控制命令（stop_generation / resume_action）。

    使用 asyncio.Queue 作为消息缓冲区：
    - 前端发送的消息通过 WebSocketServer 回调进入队列
    - receive() 从队列中取出消息并转换为 state

    Attributes:
        session_manager: 会话管理器
        _message_queue: 消息缓冲队列

    Example::

        adapter = WebSocketInputAdapter(session_manager=manager)
        # 由 WebSocketServer.on_message 回调填充队列
        state = await adapter.receive()
    """

    def __init__(self, session_manager: SessionManager | None = None) -> None:
        """初始化 WebSocket 输入适配器。

        Args:
            session_manager: 会话管理器实例（可选）
        """
        self.session_manager = session_manager or SessionManager()
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue_message(self, session_id: str, message: dict[str, Any]) -> None:
        """将接收到的消息放入处理队列。

        此方法由 WebSocketServer 的 on_message 回调调用。

        Args:
            session_id: 发送方的会话 ID
            message: 解析后的消息字典（EventEnvelope.to_dict() 格式）
        """
        await self._message_queue.put({
            "session_id": session_id,
            **message,
        })

    async def receive(self) -> dict[str, Any]:
        """从消息队列中取出下一条消息，转换为管道初始 state。

        阻塞等待直到有消息可用。处理两类消息：
        - user_input: 转换为包含用户输入的 state
        - stop_generation: 设置 should_stop=True
        - resume_action: 设置审批结果

        Returns:
            管道初始 state 字典
        """
        message = await self._message_queue.get()

        event_type = message.get("type", "")
        data = message.get("data", {})
        session_id = message.get("session_id", uuid.uuid4().hex[:12])

        # BUG-FIX-fix_pipeline_thread_id_missing:
        # 从 SessionManager 查找 thread_id，注入管道 state，
        # 供 TrackPlugin 在保存 PipelineRunSummary 时写入 thread_id 字段。
        _thread_id = ""
        ws_session = self.session_manager.get_session(session_id)
        if ws_session and ws_session.thread_id:
            _thread_id = ws_session.thread_id

        if event_type == EventType.STOP_GENERATION.value:
            return {
                "user_input": "",
                StateKeys.CORE_TYPE: "llm_call",
                StateKeys.SESSION_ID: session_id,
                StateKeys.SHOULD_STOP: True,
                "iteration": 1,
                "_ws_session_id": session_id,
                "thread_id": _thread_id,
            }

        if event_type == EventType.RESUME_ACTION.value:
            approved = data.get("approved", False)
            return {
                "user_input": "",
                StateKeys.CORE_TYPE: "llm_call",
                StateKeys.SESSION_ID: session_id,
                StateKeys.SHOULD_STOP: not approved,
                StateKeys.APPROVAL_REQUIRED: False,
                "iteration": 1,
                "_ws_session_id": session_id,
                "_approval_result": approved,
                "thread_id": _thread_id,
            }

        # 默认处理 user_input
        content = data.get("content", "")
        return {
            "user_input": content,
            StateKeys.CORE_TYPE: "llm_call",
            StateKeys.SESSION_ID: session_id,
            StateKeys.SHOULD_STOP: False,
            "iteration": 1,
            "_ws_session_id": session_id,
            "_parent_record_id": data.get("parent_record_id", ""),
            "thread_id": _thread_id,
        }


class WebSocketOutputAdapter(IOutputAdapter):
    """WebSocket 输出适配器。

    将管道结果和流式 chunk 通过 WebSocket 推送回前端。
    遵循 frontend-backend-protocol.md 定义的事件格式。

    支持的推送类型：
    - 管道最终 state → pipeline_end 事件
    - 流式 chunk → stream_chunk 事件
    - 错误信息 → pipeline_error 事件
    - 管道启动 → pipeline_start 事件

    Attributes:
        server: WebSocket 服务器实例
        _current_message_id: 当前流式消息 ID
        _sequence_counter: 流式 chunk 序号计数器

    Example::

        adapter = WebSocketOutputAdapter(server=ws_server)
        await adapter.send({"raw_result": "Hello!", ...})
        await adapter.send_stream({"text": "H", "type": "token"})
    """

    def __init__(self, server: WebSocketServer) -> None:
        """初始化 WebSocket 输出适配器。

        Args:
            server: WebSocket 服务器实例，用于发送事件
        """
        self.server = server
        self._current_message_id: str = ""
        self._sequence_counter: int = 0
        self._full_content: str = ""
        self._stream_start_sent: bool = False
        self._pipeline_id: str = ""

    def set_pipeline_id(self, pipeline_id: str) -> None:
        """设置当前管道 ID，用于前端消息路由。

        在子管道创建时调用，使流式事件携带 pipeline_id，
        前端据此将消息路由到对应的子 Tab。

        Args:
            pipeline_id: 管道唯一标识
        """
        self._pipeline_id = pipeline_id

    def set_session_id(self, session_id: str) -> None:
        """设置当前推送目标的会话 ID。

        在处理每个新请求时调用，确保输出推送到正确的 WebSocket 连接。

        Args:
            session_id: 目标会话 ID
        """
        self._session_id = session_id

    def start_stream(self, message_id: str | None = None, model: str = "") -> None:
        """开始一个新的流式输出会话。

        重置计数器并发送 stream_start 事件。

        Args:
            message_id: 消息 ID（可选，不传则自动生成）
            model: 使用的模型名称
        """
        self._current_message_id = message_id or str(uuid.uuid4())
        self._sequence_counter = 0
        self._full_content = ""
        self._stream_start_sent = True

    async def send_pipeline_start(self, session_id: str, agent_level: str = "L1", pipeline_id: str = "") -> None:
        """发送 pipeline_start 事件。

        在管道引擎开始执行时调用。
        如果传入 pipeline_id，同时缓存到适配器供后续流式事件使用。

        Args:
            session_id: 会话 ID
            agent_level: Agent 层级
            pipeline_id: 管道 ID（可选，传入后缓存供流式事件使用）
        """
        # 缓存 pipeline_id，确保后续 stream_start/chunk/end 都能携带
        if pipeline_id:
            self._pipeline_id = pipeline_id
        event = create_event(
            EventType.PIPELINE_START,
            PipelineStartData(
                session_id=session_id,
                agent_level=agent_level,
            ).to_dict(),
        )
        await self.server.send_event(session_id, event)

    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state。

        根据 state 内容发送不同类型的事件：
        - 包含 error → pipeline_error 事件
        - should_stop → pipeline_end（stopped）
        - 正常完成 → pipeline_end（completed）

        同时从 state 中提取 pipeline_id 并缓存，确保后续事件携带。

        Args:
            state: 管道引擎的最终 state 字典
        """
        session_id = state.get("_ws_session_id", getattr(self, "_session_id", ""))

        # 从 state 中提取 pipeline_id（如果尚未设置），确保 pipeline_end 事件也能携带
        state_pipeline_id = state.get(StateKeys.PIPELINE_ID, "")
        if state_pipeline_id and not self._pipeline_id:
            self._pipeline_id = state_pipeline_id

        # 错误输出
        if error := state.get(StateKeys.RAW_ERROR):
            error_event = create_event(
                EventType.PIPELINE_ERROR,
                ErrorData(
                    error=str(error),
                    phase="core",
                ).to_dict(),
            )
            if session_id:
                await self.server.send_event(session_id, error_event)

            # 同时发送 pipeline_end
            end_event = create_event(
                EventType.PIPELINE_END,
                PipelineEndData(
                    session_id=session_id,
                    status="failed",
                    total_iterations=state.get(StateKeys.ITERATION, 0),
                ).to_dict(),
            )
            if session_id:
                await self.server.send_event(session_id, end_event)
            return

        # 停止信号
        if state.get(StateKeys.SHOULD_STOP):
            end_event = create_event(
                EventType.PIPELINE_END,
                PipelineEndData(
                    session_id=session_id,
                    status="stopped",
                    total_iterations=state.get(StateKeys.ITERATION, 0),
                ).to_dict(),
            )
            if session_id:
                await self.server.send_event(session_id, end_event)
            return

        # 正常完成
        status = "completed" if state.get(StateKeys.ENDED, True) else "running"
        end_event = create_event(
            EventType.PIPELINE_END,
            PipelineEndData(
                session_id=session_id,
                status=status,
                total_iterations=state.get(StateKeys.ITERATION, 0),
            ).to_dict(),
        )
        if session_id:
            await self.server.send_event(session_id, end_event)

    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出一个 chunk。

        将 LLM 生成的逐 token chunk 通过 WebSocket 推送为 stream_chunk 事件。
        自动管理 stream_start/stream_chunk/stream_end 的时序：
        - 第一个 chunk 自动先发送 stream_start
        - 后续 chunk 发送 stream_chunk
        - 调用方需在流式结束后调用 end_stream()

        Args:
            chunk: 流式数据块，包含：
                - text: 当前 chunk 的文本内容
                - type: chunk 类型（"token" / "error" / "system"）
        """
        session_id = getattr(self, "_session_id", "")
        text = chunk.get("text", "")
        chunk_type = chunk.get("type", "token")

        if not session_id:
            logger.warning("No session_id set for stream output, skipping chunk")
            return

        # 如果是错误 chunk，直接发送错误事件
        if chunk_type == "error":
            error_event = create_event(
                EventType.PIPELINE_ERROR,
                ErrorData(error=text, phase="core").to_dict(),
            )
            await self.server.send_event(session_id, error_event)
            return

        # 首次流式输出，发送 stream_start
        if not self._stream_start_sent:
            self.start_stream(model="unknown")

        if self._sequence_counter == 0:
            start_event = create_event(
                EventType.STREAM_START,
                StreamStartData(
                    message_id=self._current_message_id,
                    model=getattr(self, "_model", ""),
                    pipeline_id=self._pipeline_id,
                ).to_dict(),
            )
            await self.server.send_event(session_id, start_event)

        # 累积完整内容
        self._full_content += text
        self._sequence_counter += 1

        # 发送 stream_chunk
        chunk_event = create_event(
            EventType.STREAM_CHUNK,
            StreamChunkData(
                message_id=self._current_message_id,
                content=text,
                sequence=self._sequence_counter,
                pipeline_id=self._pipeline_id,
            ).to_dict(),
        )
        await self.server.send_event(session_id, chunk_event)

    async def end_stream(self, usage: dict[str, int] | None = None) -> None:
        """结束流式输出，发送 stream_end 事件。

        在流式输出完成后调用，发送包含完整内容的 stream_end 事件。

        Args:
            usage: token 使用量信息（可选）
        """
        session_id = getattr(self, "_session_id", "")
        if not session_id or not self._stream_start_sent:
            return

        end_event = create_event(
            EventType.STREAM_END,
            StreamEndData(
                message_id=self._current_message_id,
                full_content=self._full_content,
                usage=usage or {},
                pipeline_id=self._pipeline_id,
            ).to_dict(),
        )
        await self.server.send_event(session_id, end_event)

        # 重置流式状态
        self._stream_start_sent = False
        self._sequence_counter = 0
        self._full_content = ""


class WebSocketAdapter:
    """WebSocket 通道适配器（组合模式）。

    组合 WebSocketInputAdapter 和 WebSocketOutputAdapter，
    提供 WebSocket 通道的完整输入/输出能力。

    同时负责：
    - 创建和管理 WebSocketServer
    - 将 server 的消息回调连接到 input_adapter 的队列
    - 服务器生命周期管理

    Example::

        adapter = WebSocketAdapter(host="0.0.0.0", port=8765)
        await adapter.start()
        # ... 使用 adapter.input_adapter / adapter.output_adapter ...
        await adapter.stop()
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        session_manager: SessionManager | None = None,
    ) -> None:
        """初始化 WebSocket 通道适配器。

        Args:
            host: 服务器监听地址
            port: 服务器监听端口
            session_manager: 会话管理器（可选，不传则自动创建）
        """
        self.session_manager = session_manager or SessionManager()
        self.server = WebSocketServer(
            host=host,
            port=port,
            session_manager=self.session_manager,
        )
        self.input_adapter = WebSocketInputAdapter(
            session_manager=self.session_manager,
        )
        self.output_adapter = WebSocketOutputAdapter(
            server=self.server,
        )

        # 绑定 server 的消息回调到 input_adapter
        self.server.on_message = self.input_adapter.enqueue_message

    async def start(self) -> None:
        """启动 WebSocket 服务器。"""
        await self.server.start()
        logger.info("WebSocket adapter started on %s:%d", self.server.host, self.server.port)

    async def stop(self) -> None:
        """停止 WebSocket 服务器。"""
        await self.server.stop()
        logger.info("WebSocket adapter stopped")
