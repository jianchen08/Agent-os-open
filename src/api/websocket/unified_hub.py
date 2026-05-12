"""
统一消息中心（Unified Message Hub）

核心职责：
1. 流式状态管理：管理每个消息的流式状态（片段累积、工具调用、思考内容）
2. 消息格式转换：LangGraph 消息 ↔ 统一事件格式
3. 安全过滤：集中过滤系统提示词、敏感数据
4. 数据库协调：流式开始/结束时操作数据库
5. 多前端路由：将事件路由到不同的前端适配器

架构决策: ADR-002
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.api.websocket.message_bus import SourceType, get_message_bus
from src.api.websocket.message_types import (
    create_standard_message,
)

logger = logging.getLogger(__name__)


# ============================================
# 数据模型定义
# ============================================


class UnifiedIncomingMessage(BaseModel):
    """统一输入消息格式（前端 → 后端）

    Args:
        type: 消息类型（user_input | command | system_event）
        content: 消息内容
        thread_id: 会话ID
        user_id: 用户ID
        message_id: 消息ID（可选，后端生成）
        metadata: 元数据（可选）
        timestamp: ISO 8601 格式时间戳
    """

    type: str = Field(..., description="消息类型")
    content: str = Field(..., description="消息内容")
    thread_id: str = Field(..., description="会话ID")
    user_id: str = Field(..., description="用户ID")
    message_id: str | None = Field(None, description="消息ID（可选，后端生成）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="时间戳",
    )


class UnifiedStreamEvent(BaseModel):
    """统一流式事件格式（后端 → 前端）

    Args:
        event_type: 事件类型
        message_id: 消息ID
        thread_id: 会话ID
        payload: 事件载荷
        metadata: 元数据
    """

    event_type: str = Field(..., description="事件类型")
    message_id: str = Field(..., description="消息ID")
    thread_id: str = Field(..., description="会话ID")
    payload: dict[str, Any] = Field(default_factory=dict, description="事件载荷")
    metadata: dict[str, Any] = Field(
        default_factory=lambda: {"timestamp": datetime.now(UTC).isoformat()},
        description="元数据",
    )


# ============================================
# 流式状态管理
# ============================================


@dataclass
class StreamState:
    """单个消息的流式状态

    管理单个消息的流式状态，累积片段直到流式结束。
    支持消息分段：文本段和工具调用段可以交错排列

    Attributes:
        message_id: 消息ID
        thread_id: 会话ID
        chunks: 文本片段累积列表
        segments: 消息分段列表（文本段 + 工具调用段）
        tool_calls: 工具调用列表
        thinking: 思考内容累积
        status: 状态（streaming/completed/failed）
        started_at: 开始时间
        chunk_count: 片段数量
        tool_call_map: 工具调用映射（tool_call_id -> tool_call_info）
        has_thinking: 是否有思考内容
    """

    message_id: str
    thread_id: str
    chunks: list[str] = field(default_factory=list)
    segments: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking: str = ""
    status: str = "streaming"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    chunk_count: int = 0
    tool_call_map: dict[str, dict[str, Any]] = field(default_factory=dict)
    has_thinking: bool = False

    def add_text_chunk(self, chunk: str):
        """
        添加文本片段，同时更新当前文本段

        Args:
            chunk: 文本片段
        """
        self.chunks.append(chunk)
        self.chunk_count += 1

        # 如果最后一段是文本，追加到该段
        if self.segments and self.segments[-1].get("type") == "text":
            self.segments[-1]["content"] += chunk
        else:
            # 否则创建新的文本段
            self.segments.append({"type": "text", "content": chunk})

    def add_tool_call(self, tool_call_id: str, tool_name: str, args: dict[str, Any]):
        """
        添加工具调用，在分段数组中插入工具调用段

        Args:
            tool_call_id: 工具调用ID
            tool_name: 工具名称
            args: 工具参数
        """
        tool_call_info = {
            "id": tool_call_id,
            "name": tool_name,
            "args": args,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
        }
        self.tool_call_map[tool_call_id] = tool_call_info
        self.tool_calls.append(tool_call_info)

        # 在分段数组中添加工具调用段
        self.segments.append({
            "type": "tool_call",
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
        })

        # 创建新的空文本段（用于工具调用后的内容）
        self.segments.append({"type": "text", "content": ""})

    def update_tool_call(
        self,
        tool_call_id: str,
        status: str,
        result: Any | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ):
        """
        更新工具调用状态

        Args:
            tool_call_id: 工具调用ID
            status: 状态（completed/failed）
            result: 执行结果
            error: 错误信息
            duration_ms: 执行时长（毫秒）
        """
        if tool_call_id in self.tool_call_map:
            tool_call_info = self.tool_call_map[tool_call_id]
            tool_call_info["status"] = status
            if result is not None:
                tool_call_info["result"] = result
            if error is not None:
                tool_call_info["error"] = error
            if duration_ms is not None:
                tool_call_info["duration_ms"] = duration_ms
            tool_call_info["ended_at"] = datetime.now(UTC).isoformat()

    def add_thinking(self, content: str):
        """
        添加思考内容

        Args:
            content: 思考内容
        """
        self.thinking += content
        self.has_thinking = True

    def get_full_content(self) -> str:
        """
        获取完整内容（用于数据库保存）

        Returns:
            完整的文本内容
        """
        return "".join(self.chunks)

    def get_duration_ms(self) -> int:
        """
        获取执行时长（毫秒）

        Returns:
            执行时长（毫秒）
        """
        return int((datetime.now(UTC) - self.started_at).total_seconds() * 1000)

    def to_message_data(self) -> dict[str, Any]:
        """
        转换为数据库消息数据格式

        Returns:
            消息数据字典
        """
        message_data = {
            "type": "ai",
            "content": self.get_full_content(),
            "status": self.status,
        }

        if self.has_thinking:
            message_data["thinking"] = self.thinking

        if self.tool_calls:
            message_data["tool_calls"] = self.tool_calls

        # 添加分段信息（用于前端正确渲染工具卡片位置）
        if self.segments:
            message_data["segments"] = self.segments

        message_data["duration_ms"] = self.get_duration_ms()

        return message_data


# ============================================
# 内容过滤
# ============================================


class ContentFilter:
    """内容安全过滤

    集中过滤系统提示词、敏感数据等
    """

    def __init__(self):
        # 系统提示词标记列表
        self.system_prompt_markers = [
            "You are a helpful assistant",
            "You are an AI assistant",
            "You are an AI",
            "你是一个乐于助人的助手",
            "你是一个AI助手",
            "You are ChatGPT",
            "You are Claude",
        ]

        # 敏感数据正则表达式
        self.sensitive_patterns = [
            (r'sk-[a-zA-Z0-9]{48}', 'sk-***'),  # OpenAI API Key
            (r'sk-[a-zA-Z0-9]{20}', 'sk-***'),  # 短格式 API Key
            (r'[a-zA-Z0-9]{32}-[a-zA-Z0-9]{16}', '***'),  # 其他 Key 格式
            (r'Bearer [a-zA-Z0-9_-]{20,}', 'Bearer ***'),  # Bearer Token
            (r'password["\s]*[:=]["\s]*[^\s"]{8,}', 'password="***"'),  # 密码
        ]

    def filter(self, event: UnifiedStreamEvent) -> UnifiedStreamEvent | None:
        """
        过滤事件内容

        Args:
            event: 统一流式事件

        Returns:
            过滤后的事件（None 表示被拦截）
        """
        content = event.payload.get("content", "")
        thinking_content = event.payload.get("thinking_content", "")

        # 检查是否是系统提示词
        if self._is_system_prompt(content) or self._is_system_prompt(
            thinking_content
        ):
            logger.warning(
                f"[ContentFilter] 拦截系统提示词泄露 | "
                f"message_id={event.message_id} | "
                f"content_preview={content[:50]}..."
            )
            return None

        # 脱敏处理
        event.payload["content"] = self._mask_sensitive_data(content)
        if thinking_content:
            event.payload["thinking_content"] = self._mask_sensitive_data(
                thinking_content
            )

        # 内容长度限制
        max_length = 10000
        if len(event.payload.get("content", "")) > max_length:
            event.payload["content"] = event.payload["content"][:max_length] + "..."

        return event

    def _is_system_prompt(self, content: str) -> bool:
        """
        检测是否是系统提示词

        Args:
            content: 内容

        Returns:
            是否是系统提示词
        """
        if not content:
            return False
        return any(marker in content for marker in self.system_prompt_markers)

    def _mask_sensitive_data(self, content: str) -> str:
        """
        脱敏处理

        Args:
            content: 原始内容

        Returns:
            脱敏后的内容
        """
        masked_content = content
        for pattern, replacement in self.sensitive_patterns:
            masked_content = re.sub(pattern, replacement, masked_content)
        return masked_content


# ============================================
# 统一消息中心
# ============================================


class UnifiedMessageHub:
    """
    统一消息中心

    核心职责：
    1. 流式状态管理：管理每个消息的流式状态（片段累积、工具调用、思考内容）
    2. 消息格式转换：LangGraph 消息 ↔ 统一事件格式
    3. 安全过滤：集中过滤系统提示词、敏感数据
    4. 数据库协调：流式开始/结束时操作数据库
    5. 多前端路由：将事件路由到不同的前端适配器
    """

    _instance: Optional["UnifiedMessageHub"] = None

    def __new__(cls) -> "UnifiedMessageHub":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # 流式状态管理
        self.stream_states: dict[str, StreamState] = {}

        # 内容过滤器
        self.content_filter = ContentFilter()

        # 消息总线
        self.message_bus = get_message_bus()

        # 数据库仓储（延迟初始化，避免循环依赖）
        self._execution_record_repo = None

        # 锁
        self._lock = asyncio.Lock()

        # 统计
        self._stats = {
            "total_messages": 0,
            "total_streams": 0,
            "total_chunks": 0,
            "total_tool_calls": 0,
            "active_streams": 0,
        }

        self._initialized = True
        logger.info("[UnifiedMessageHub] 统一消息中心已初始化")

    async def set_execution_record_repo(self, repo: Any):
        """
        设置执行记录仓储（延迟初始化）

        Args:
            repo: 执行记录仓储实例
        """
        self._execution_record_repo = repo
        logger.info("[UnifiedMessageHub] 执行记录仓储已设置")

    # ============================================
    # 用户输入处理
    # ============================================

    async def on_user_input(self, message: UnifiedIncomingMessage) -> str:
        """
        处理用户输入

        流程：
        1. 确保消息ID已设置
        2. 初始化流式状态
        3. 发送 stream.start 事件到前端

        注意：用户消息的保存由 user_input.py 中的 MessagePersistence 处理
        这里只负责流式状态管理，不保存数据库记录

        Args:
            message: 统一输入消息

        Returns:
            生成的消息ID
        """
        # 生成消息ID（如果未提供）- 使用嵌套ID格式
        if not message.message_id:
            from src.db.connection import get_async_session
            from src.utils.message_id_helper import generate_execution_record_id

            async for db_session in get_async_session():
                message.message_id = await generate_execution_record_id(
                    db_session, message.thread_id
                )
                break

        # 注意：用户消息的保存由 user_input.py 中的 MessagePersistence 处理
        # 这里只初始化流式状态，不保存数据库记录

        # 初始化流式状态（使用消息级别的锁，支持并发）
        async with self._lock:
            self.stream_states[message.message_id] = StreamState(
                message_id=message.message_id,
                thread_id=message.thread_id,
            )
            self._stats["total_messages"] += 1
            self._stats["active_streams"] += 1

        logger.info(
            f"[UnifiedMessageHub] 用户输入已处理 | "
            f"message_id={message.message_id} | "
            f"thread_id={message.thread_id}"
        )

        return message.message_id

    async def _save_user_message(self, message: UnifiedIncomingMessage):
        """
        保存用户消息到数据库

        Args:
            message: 统一输入消息
        """
        if self._execution_record_repo:
            message_data = {
                "type": "human",
                "content": message.content,
                "metadata": message.metadata,
            }

            # 使用消息ID作为执行记录ID（确保是嵌套ID格式）
            record_id = message.message_id
            if not record_id.startswith("exec-") and not record_id.startswith(message.thread_id):
                # 如果不是嵌套ID格式，生成一个新的
                from src.db.connection import get_async_session
                from src.utils.message_id_helper import generate_execution_record_id

                async for db_session in get_async_session():
                    record_id = await generate_execution_record_id(
                        db_session, message.thread_id
                    )
                    break

            await self._execution_record_repo.save_execution_record(
                session_id=message.thread_id,
                message_data=message_data,
                parent_record_id=None,
                record_id=record_id,
            )

            logger.debug(
                f"[UnifiedMessageHub] 用户消息已保存 | "
                f"message_id={message.message_id}"
            )

    # ============================================
    # LLM 流式输出处理
    # ============================================

    async def on_stream_start(self, message_id: str, thread_id: str):
        """
        流式开始

        发送 stream.start 事件到前端，并初始化流式状态

        Args:
            message_id: 消息ID
            thread_id: 会话ID
        """
        logger.info(
            f"[UnifiedMessageHub] on_stream_start 被调用 | "
            f"message_id={message_id} | "
            f"thread_id={thread_id}"
        )

        # 初始化流式状态（如果不存在）
        if message_id not in self.stream_states:
            self.stream_states[message_id] = StreamState(
                message_id=message_id,
                thread_id=thread_id,
            )
            self._stats["active_streams"] += 1
            logger.debug(
                f"[UnifiedMessageHub] 创建流式状态 | "
                f"message_id={message_id} | "
                f"thread_id={thread_id}"
            )

        # 创建标准消息，确保与前端期望的格式一致
        # 前端期望格式：{ type, message_id, thread_id, data: { ai_message_id } }
        message = create_standard_message(
            message_type="stream_start",
            thread_id=thread_id,
            data={"ai_message_id": message_id},  # 前端期望 data.ai_message_id
            message_id=message_id,
        )

        logger.info(
            f"[UnifiedMessageHub] 准备发送 stream_start 事件 | "
            f"message_id={message_id} | "
            f"thread_id={thread_id}"
        )

        # 通过 MessageBus 发送
        success = await self.message_bus.emit(
            thread_id=thread_id,
            message=message,
            source_type=SourceType.MAIN,
            source_id="unified_hub",
        )

        logger.info(
            f"[UnifiedMessageHub] stream_start 事件发送结果 | "
            f"success={success} | "
            f"message_id={message_id} | "
            f"thread_id={thread_id}"
        )

    async def on_llm_chunk(self, message_id: str, chunk: str):
        """
        处理 LLM 流式输出片段

        流程：
        1. 创建 stream.chunk 事件
        2. 更新流式状态
        3. 发送到前端（不对每个 chunk 过滤，保证实时性）

        Args:
            message_id: 消息ID
            chunk: 文本片段
        """
        state = self.stream_states.get(message_id)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 更新流式状态（先更新，确保状态一致性）
        state.add_text_chunk(chunk)

        # 创建标准消息，确保与前端期望的格式一致
        # 前端期望格式：{ type, message_id, thread_id, data: { chunk, ai_message_id } }
        message = create_standard_message(
            message_type="stream_chunk",
            thread_id=state.thread_id,
            data={
                "chunk": chunk,
                "ai_message_id": message_id,  # 前端期望 data.ai_message_id
            },
            message_id=message_id,
        )

        logger.info(
            f"[UnifiedMessageHub] 准备发送 stream_chunk 事件 | "
            f"message_id={message_id} | "
            f"chunk_length={len(chunk)} | "
            f"chunk_content={repr(chunk[:50])} | "
            f"chunk_index={state.chunk_count}"
        )

        # 通过 MessageBus 发送
        success = await self.message_bus.emit(
            thread_id=state.thread_id,
            message=message,
            source_type=SourceType.MAIN,
            source_id="unified_hub",
        )

        logger.debug(
            f"[UnifiedMessageHub] stream_chunk 事件发送结果 | "
            f"success={success} | "
            f"message_id={message_id}"
        )

        self._stats["total_chunks"] += 1

    async def on_stream_end(self, message_id: str):
        """
        流式结束

        流程：
        1. 更新流式状态为 completed
        2. 保存完整内容到数据库
        3. 发送 stream.end 事件到前端
        4. 清理流式状态

        Args:
            message_id: 消息ID
        """
        state = self.stream_states.pop(message_id, None)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 更新状态
        state.status = "completed"

        # 保存 AI 消息到数据库
        try:
            await self._save_ai_message(state)
            logger.info(
                f"[UnifiedMessageHub] AI 消息已保存 | message_id={message_id}"
            )
        except Exception as e:
            logger.error(
                f"[UnifiedMessageHub] 保存 AI 消息失败 | message_id={message_id} | error={e}",
                exc_info=True,
            )

        # 创建标准消息，确保与前端期望的格式一致
        # 前端期望格式：{ type, message_id, thread_id, data: { ai_message_id, final_message_id } }
        message = create_standard_message(
            message_type="stream_end",
            thread_id=state.thread_id,
            data={
                "ai_message_id": message_id,  # 前端期望 data.ai_message_id
                "final_message_id": message_id,  # 前端期望 data.final_message_id
            },
            message_id=message_id,
        )

        # 通过 MessageBus 发送
        success = await self.message_bus.emit(
            thread_id=state.thread_id,
            message=message,
            source_type=SourceType.MAIN,
            source_id="unified_hub",
        )

        logger.info(
            f"[UnifiedMessageHub] stream_end 事件发送结果 | "
            f"success={success} | "
            f"message_id={message_id}"
        )

        self._stats["total_streams"] += 1
        self._stats["active_streams"] -= 1

        logger.info(
            f"[UnifiedMessageHub] 流式结束 | "
            f"message_id={message_id} | "
            f"chunks={state.chunk_count} | "
            f"duration_ms={state.get_duration_ms()}"
        )

    async def _save_ai_message(self, state: StreamState):
        """
        保存 AI 消息到数据库

        Args:
            state: 流式状态
        """
        from src.db.connection import get_session_context
        from src.db.repositories.execution_record_repo import ExecutionRecordRepository
        from src.utils.message_id_helper import generate_execution_record_id

        message_data = state.to_message_data()

        try:
            async with get_session_context() as db:
                repo = ExecutionRecordRepository(db)

                # 使用消息ID作为执行记录ID（确保是嵌套ID格式）
                record_id = state.message_id
                if not record_id.startswith("exec-") and not record_id.startswith(state.thread_id):
                    # 如果不是嵌套ID格式，生成一个新的
                    record_id = await generate_execution_record_id(db, state.thread_id)

                await repo.save_execution_record(
                    session_id=state.thread_id,
                    message_data=message_data,
                    parent_record_id=None,
                    record_id=record_id,
                )

                logger.info(
                    f"[UnifiedMessageHub] AI 消息已保存 | "
                    f"record_id={record_id} | "
                    f"thread_id={state.thread_id} | "
                    f"status={state.status}"
                )
        except Exception as e:
            logger.error(
                f"[UnifiedMessageHub] 保存 AI 消息失败 | "
                f"thread_id={state.thread_id} | error={e}",
                exc_info=True,
            )

    # ============================================
    # 工具调用处理
    # ============================================

    async def on_tool_start(
        self, message_id: str, tool_call_id: str, tool_name: str, args: dict[str, Any]
    ):
        """
        工具调用开始

        流程：
        1. 创建 tool.start 事件
        2. 更新流式状态
        3. 发送到前端
        4. 创建工具执行记录

        Args:
            message_id: 消息ID
            tool_call_id: 工具调用ID
            tool_name: 工具名称
            args: 工具参数
        """
        state = self.stream_states.get(message_id)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 创建工具开始事件
        event = UnifiedStreamEvent(
            event_type="tool_call_start",
            message_id=message_id,
            thread_id=state.thread_id,
            payload={
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "args": args,
            },
        )

        # 更新流式状态
        state.add_tool_call(tool_call_id, tool_name, args)

        # 发送到前端
        await self.emit_to_frontends(event)

        # 注意：工具执行记录由实际执行工具的代码（execute_tools.py）创建
        # 这里只负责前端事件推送，不创建数据库记录

        self._stats["total_tool_calls"] += 1

    async def _create_tool_record(
        self, state: StreamState, tool_call_id: str, tool_name: str, args: dict[str, Any]
    ):
        """
        创建工具执行记录

        Args:
            state: 流式状态
            tool_call_id: 工具调用ID
            tool_name: 工具名称
            args: 工具参数
        """
        if self._execution_record_repo:
            message_data = {
                "type": "tool",
                "tool_call_id": tool_call_id,
                "name": tool_name,
                "status": "running",
                "input": args,
            }

            # 生成工具调用记录ID（嵌套ID格式）
            from src.db.connection import get_async_session
            from src.utils.message_id_helper import generate_execution_record_id

            async for db_session in get_async_session():
                tool_record_id = await generate_execution_record_id(
                    db_session, state.thread_id, state.message_id
                )
                break

            await self._execution_record_repo.save_execution_record(
                session_id=state.thread_id,
                message_data=message_data,
                parent_record_id=state.message_id,
                record_id=tool_record_id,
            )

    async def on_tool_end(
        self,
        message_id: str,
        tool_call_id: str,
        status: str,
        result: Any | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ):
        """
        工具调用结束

        流程：
        1. 创建 tool.end 事件
        2. 更新流式状态
        3. 发送到前端
        4. 更新工具执行记录

        Args:
            message_id: 消息ID
            tool_call_id: 工具调用ID
            status: 状态（completed/failed）
            result: 执行结果
            error: 错误信息
            duration_ms: 执行时长（毫秒）
        """
        state = self.stream_states.get(message_id)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 创建工具结束事件
        payload = {
            "tool_call_id": tool_call_id,
            "status": status,
        }

        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        event = UnifiedStreamEvent(
            event_type="tool_call_end",
            message_id=message_id,
            thread_id=state.thread_id,
            payload=payload,
        )

        # 更新流式状态
        state.update_tool_call(tool_call_id, status, result, error, duration_ms)

        # 发送到前端
        await self.emit_to_frontends(event)

        # 注意：工具执行记录的更新由实际执行工具的代码（execute_tools.py）处理
        # 这里只负责前端事件推送，不更新数据库记录

    async def _update_tool_record(
        self,
        state: StreamState,
        tool_call_id: str,
        status: str,
        result: Any | None,
        error: str | None,
    ):
        """
        更新工具执行记录

        Args:
            state: 流式状态
            tool_call_id: 工具调用ID
            status: 状态
            result: 执行结果
            error: 错误信息
        """
        if self._execution_record_repo:
            message_data = {
                "type": "tool",
                "tool_call_id": tool_call_id,
                "status": status,
            }

            if result is not None:
                message_data["output"] = {"result": result}
            if error is not None:
                message_data["error"] = error

            record_id = f"{state.thread_id}-{state.message_id}-{tool_call_id}"
            await self._execution_record_repo.update_execution_record(
                record_id, message_data
            )

    # ============================================
    # 思考模式处理
    # ============================================

    async def on_thinking_start(self, message_id: str):
        """
        思考开始

        Args:
            message_id: 消息ID
        """
        state = self.stream_states.get(message_id)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 创建思考开始事件
        event = UnifiedStreamEvent(
            event_type="thinking_start",
            message_id=message_id,
            thread_id=state.thread_id,
            payload={},
        )

        await self.emit_to_frontends(event)

        logger.debug(f"[UnifiedMessageHub] 思考开始 | message_id={message_id}")

    async def on_thinking_chunk(self, message_id: str, content: str):
        """
        思考片段

        Args:
            message_id: 消息ID
            content: 思考内容
        """
        state = self.stream_states.get(message_id)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 创建思考片段事件
        event = UnifiedStreamEvent(
            event_type="thinking_chunk",
            message_id=message_id,
            thread_id=state.thread_id,
            payload={"chunk": content},
        )

        # 安全过滤
        filtered_event = self.content_filter.filter(event)
        if not filtered_event:
            return

        # 更新流式状态
        state.add_thinking(content)

        # 发送到前端
        await self.emit_to_frontends(filtered_event)

    async def on_thinking_end(self, message_id: str):
        """
        思考结束

        Args:
            message_id: 消息ID
        """
        state = self.stream_states.get(message_id)
        if not state:
            logger.warning(
                f"[UnifiedMessageHub] 流式状态不存在 | message_id={message_id}"
            )
            return

        # 创建思考结束事件
        event = UnifiedStreamEvent(
            event_type="thinking_end",
            message_id=message_id,
            thread_id=state.thread_id,
            payload={},
        )

        await self.emit_to_frontends(event)

        logger.debug(f"[UnifiedMessageHub] 思考结束 | message_id={message_id}")

    # ============================================
    # 错误处理
    # ============================================

    async def on_stream_error(self, message_id: str, error: Exception):
        """
        流式错误

        流程：
        1. 发送 stream.error 事件到前端
        2. 更新流式状态为 failed
        3. 更新数据库记录
        4. 清理流式状态

        Args:
            message_id: 消息ID
            error: 异常对象
        """
        state = self.stream_states.pop(message_id, None)
        if not state:
            return

        # 更新状态
        state.status = "failed"

        # 发送错误事件
        event = UnifiedStreamEvent(
            event_type="stream_error",
            message_id=message_id,
            thread_id=state.thread_id,
            payload={
                "error_code": type(error).__name__,
                "error_message": str(error),
            },
        )

        await self.emit_to_frontends(event)

        # 更新数据库记录
        await self._update_ai_message_as_failed(state, error)

        self._stats["active_streams"] -= 1

        logger.error(
            f"[UnifiedMessageHub] 流式错误 | "
            f"message_id={message_id} | "
            f"error={type(error).__name__}: {error}"
        )

    async def _update_ai_message_as_failed(self, state: StreamState, error: Exception):
        """
        更新 AI 消息为失败状态

        Args:
            state: 流式状态
            error: 异常对象
        """
        if self._execution_record_repo:
            message_data = state.to_message_data()
            message_data["status"] = "failed"
            message_data["error"] = str(error)

            record_id = f"{state.thread_id}-{state.message_id}"
            await self._execution_record_repo.update_execution_record(
                record_id, message_data
            )

    # ============================================
    # 前端消息发送
    # ============================================

    async def emit_to_frontends(self, event: UnifiedStreamEvent) -> bool:
        """
        发送事件到前端

        将 UnifiedStreamEvent 转换为标准 WebSocket 消息格式，并通过 MessageBus 发送

        Args:
            event: 统一流式事件

        Returns:
            是否成功发送
        """
        logger.info(
            f"[UnifiedMessageHub] emit_to_frontends 被调用 | "
            f"event_type={event.event_type} | "
            f"message_id={event.message_id} | "
            f"thread_id={event.thread_id}"
        )

        # 转换为标准 WebSocket 消息格式
        message = create_standard_message(
            message_type=event.event_type,
            thread_id=event.thread_id,
            data=event.payload,
            message_id=event.message_id,
            timestamp=event.metadata.get("timestamp"),
        )

        logger.info(
            f"[UnifiedMessageHub] 标准消息已创建 | "
            f"message_type={message.get('type')} | "
            f"thread_id={message.get('thread_id')} | "
            f"message_id={message.get('message_id')}"
        )

        # 通过 MessageBus 发送
        success = await self.message_bus.emit(
            thread_id=event.thread_id,
            message=message,
            source_type=SourceType.MAIN,
            source_id="unified_hub",
        )

        logger.info(
            f"[UnifiedMessageHub] message_bus.emit 返回 | "
            f"success={success} | "
            f"event_type={event.event_type} | "
            f"thread_id={event.thread_id}"
        )

        if success:
            logger.debug(
                f"[UnifiedMessageHub] 事件已发送 | "
                f"event_type={event.event_type} | "
                f"message_id={event.message_id}"
            )
        else:
            logger.warning(
                f"[UnifiedMessageHub] 事件发送失败 | "
                f"event_type={event.event_type} | "
                f"thread_id={event.thread_id}"
            )

        return success

    # ============================================
    # 状态清理
    # ============================================

    async def cleanup_stream_state(self, message_id: str):
        """
        清理流式状态

        Args:
            message_id: 消息ID
        """
        state = self.stream_states.pop(message_id, None)
        if state:
            self._stats["active_streams"] -= 1
            logger.debug(
                f"[UnifiedMessageHub] 流式状态已清理 | message_id={message_id}"
            )

    async def cleanup_all_stream_states(self):
        """清理所有流式状态"""
        count = len(self.stream_states)
        self.stream_states.clear()
        self._stats["active_streams"] = 0
        logger.info(f"[UnifiedMessageHub] 所有流式状态已清理 | count={count}")

    # ============================================
    # 统计信息
    # ============================================

    def get_stats(self) -> dict[str, Any]:
        """
        获取统计信息

        Returns:
            统计信息字典
        """
        return {
            **self._stats,
            "stream_states_count": len(self.stream_states),
            "stream_state_ids": list(self.stream_states.keys()),
        }


# ============================================
# 全局单例
# ============================================

_unified_message_hub: UnifiedMessageHub | None = None


def get_unified_message_hub() -> UnifiedMessageHub:
    """
    获取统一消息中心单例

    Returns:
        统一消息中心实例
    """
    global _unified_message_hub
    if _unified_message_hub is None:
        _unified_message_hub = UnifiedMessageHub()
    return _unified_message_hub
