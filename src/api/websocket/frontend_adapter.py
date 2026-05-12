"""
前端适配层（Frontend Adapter Layer）

负责适配不同类型的前端，将前端特定格式转换为统一内部格式。

架构决策: ADR-002
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from src.api.websocket.message_bus import get_message_bus
from src.api.websocket.message_types import create_standard_message
from src.api.websocket.unified_hub import (
    UnifiedIncomingMessage,
    UnifiedStreamEvent,
)

logger = logging.getLogger(__name__)


# ============================================
# 抽象基类
# ============================================


class FrontendAdapter(ABC):
    """
    前端适配器基类

    所有前端适配器必须实现此接口，提供统一的消息转换和发送能力。
    """

    def __init__(self):
        """初始化适配器"""
        self.message_bus = get_message_bus()

    @property
    @abstractmethod
    def adapter_id(self) -> str:
        """
        适配器唯一标识

        Returns:
            适配器ID，如 'web', 'cli', 'telegram'
        """
        pass

    @abstractmethod
    async def parse_incoming(self, raw_message: Any) -> UnifiedIncomingMessage:
        """
        解析前端发来的消息为统一格式

        Args:
            raw_message: 前端原始消息（可能是 JSON、文本、Bot API 对象等）

        Returns:
            统一输入消息格式

        Raises:
            ValueError: 消息格式无效
        """
        pass

    @abstractmethod
    async def format_outgoing(self, event: UnifiedStreamEvent) -> Any:
        """
        将统一事件格式转换为前端特定格式

        Args:
            event: 统一流式事件

        Returns:
            前端特定格式的消息（JSON、文本、Bot API 格式等）
        """
        pass

    @abstractmethod
    async def send_message(self, client_id: str, message: Any) -> bool:
        """
        发送消息到特定客户端

        Args:
            client_id: 客户端ID（对于 WebSocket 是 thread_id）
            message: 前端特定格式的消息

        Returns:
            是否成功发送
        """
        pass


# ============================================
# Web 适配器（React Web 前端）
# ============================================


class WebAdapter(FrontendAdapter):
    """
    React Web 前端适配器

    特点：
    - JSON 格式
    - 完整流式支持
    - 与现有的前端消息格式兼容
    - 通过 MessageBus 发送
    """

    @property
    def adapter_id(self) -> str:
        """适配器ID"""
        return "web"

    async def parse_incoming(self, raw_message: dict[str, Any]) -> UnifiedIncomingMessage:
        """
        解析前端发来的 JSON 消息

        前端消息格式：
        {
            "type": "user_input",
            "message_id": "msg-xxx",
            "thread_id": "thread-xxx",
            "data": {
                "content": "消息内容",
                "metadata": {...}
            },
            "timestamp": "2024-01-01T00:00:00Z"
        }

        Args:
            raw_message: 前端原始 JSON 消息

        Returns:
            统一输入消息
        """
        try:
            # 提取消息内容
            data = raw_message.get("data", {})
            content = data.get("content", "")

            # 构建统一输入消息
            unified_message = UnifiedIncomingMessage(
                type=raw_message.get("type", "user_input"),
                content=content,
                thread_id=raw_message.get("thread_id", ""),
                user_id=data.get("user_id", "default"),
                message_id=raw_message.get("message_id"),
                metadata=data.get("metadata", {}),
                timestamp=raw_message.get("timestamp"),
            )

            logger.debug(
                f"[WebAdapter] 解析前端消息成功 | "
                f"message_id={unified_message.message_id} | "
                f"type={unified_message.type}"
            )

            return unified_message

        except Exception as e:
            logger.error(f"[WebAdapter] 解析前端消息失败 | error={e}", exc_info=True)
            raise ValueError(f"无效的前端消息格式: {e}")

    async def format_outgoing(self, event: UnifiedStreamEvent) -> dict[str, Any]:
        """
        将统一事件转换为 Web 前端格式

        转换规则：
        - event_type → type
        - payload → data
        - metadata → 附加字段

        Args:
            event: 统一流式事件

        Returns:
            Web 前端格式的 JSON 消息
        """
        # 使用标准消息格式工厂函数
        message = create_standard_message(
            message_type=event.event_type,
            thread_id=event.thread_id,
            data=event.payload,
            message_id=event.message_id,
            timestamp=event.metadata.get("timestamp"),
        )

        # 添加适配器标识
        message["metadata"] = {
            **event.metadata,
            "adapter_id": self.adapter_id,
        }

        logger.debug(
            f"[WebAdapter] 格式化事件 | "
            f"event_type={event.event_type} | "
            f"message_id={event.message_id}"
        )

        return message

    async def send_message(self, client_id: str, message: dict[str, Any]) -> bool:
        """
        通过 MessageBus 发送消息到 Web 前端

        Args:
            client_id: 客户端ID（thread_id）
            message: Web 前端格式的消息

        Returns:
            是否成功发送
        """
        try:
            success = await self.message_bus.emit(
                thread_id=client_id,
                message=message,
            )

            if success:
                logger.debug(
                    f"[WebAdapter] 消息已发送 | client_id={client_id} | type={message.get('type')}"
                )
            else:
                logger.warning(
                    f"[WebAdapter] 消息发送失败 | client_id={client_id} | 无活跃连接"
                )

            return success

        except Exception as e:
            logger.error(f"[WebAdapter] 发送消息异常 | error={e}", exc_info=True)
            return False


# ============================================
# CLI 适配器（命令行界面）
# ============================================


class CLIAdapter(FrontendAdapter):
    """
    命令行界面适配器

    特点：
    - 文本格式
    - 颜色编码（ANSI 转义序列）
    - 简化输出，便于阅读
    - 累积缓冲区，避免频繁刷新
    """

    # ANSI 颜色代码
    COLOR_RESET = "\033[0m"
    COLOR_GRAY = "\033[90m"
    COLOR_GREEN = "\033[92m"
    COLOR_YELLOW = "\033[93m"
    COLOR_BLUE = "\033[94m"
    COLOR_RED = "\033[91m"

    def __init__(self):
        """初始化适配器"""
        super().__init__()
        # 每个客户端的消息缓冲区
        self._buffers: dict[str, list[str]] = {}

    @property
    def adapter_id(self) -> str:
        """适配器ID"""
        return "cli"

    async def parse_incoming(self, raw_message: str) -> UnifiedIncomingMessage:
        """
        解析命令行输入

        简单的文本输入，默认为用户消息

        Args:
            raw_message: 命令行文本输入

        Returns:
            统一输入消息
        """
        try:
            # 简单的文本输入，使用默认 thread_id 和 user_id
            # 实际使用时，这些应该从上下文获取
            unified_message = UnifiedIncomingMessage(
                type="user_input",
                content=raw_message.strip(),
                thread_id="cli-session",
                user_id="cli-user",
                metadata={"source": "cli"},
            )

            logger.debug(f"[CLIAdapter] 解析命令行输入 | content={raw_message[:50]}")

            return unified_message

        except Exception as e:
            logger.error(f"[CLIAdapter] 解析命令行输入失败 | error={e}", exc_info=True)
            raise ValueError(f"无效的命令行输入: {e}")

    async def format_outgoing(self, event: UnifiedStreamEvent) -> str:
        """
        将统一事件转换为带颜色的文本格式

        Args:
            event: 统一流式事件

        Returns:
            带颜色编码的文本
        """
        output = ""

        # 根据事件类型生成不同颜色的文本
        if event.event_type == "stream.start":
            output = f"{self.COLOR_BLUE}[开始] {self.COLOR_RESET}\n"

        elif event.event_type == "stream.chunk":
            content = event.payload.get("content", "")
            output = content

        elif event.event_type == "stream.end":
            event.payload.get("final_content", "")
            duration_ms = event.payload.get("duration_ms", 0)
            output = f"\n{self.COLOR_GREEN}[完成] {self.COLOR_RESET}耗时 {duration_ms}ms\n"

        elif event.event_type == "stream.error":
            error_msg = event.payload.get("error_message", "未知错误")
            output = f"\n{self.COLOR_RED}[错误] {error_msg}{self.COLOR_RESET}\n"

        elif event.event_type == "tool.start":
            tool_name = event.payload.get("tool_name", "")
            args = event.payload.get("args", {})
            args_str = json.dumps(args, ensure_ascii=False) if args else ""
            output = f"\n{self.COLOR_YELLOW}[工具] {tool_name}({args_str}){self.COLOR_RESET}\n"

        elif event.event_type == "tool.end":
            tool_name = event.payload.get("tool_name", "")
            status = event.payload.get("status", "")
            if status == "completed":
                output = f"{self.COLOR_GREEN}✓ {tool_name} 完成{self.COLOR_RESET}\n"
            else:
                output = f"{self.COLOR_RED}✗ {tool_name} 失败{self.COLOR_RESET}\n"

        elif event.event_type == "thinking.start":
            output = f"{self.COLOR_GRAY}[思考]{self.COLOR_RESET} "

        elif event.event_type == "thinking.chunk":
            thinking = event.payload.get("thinking_content", "")
            output = thinking

        elif event.event_type == "thinking.end":
            output = f"{self.COLOR_GRAY}[思考结束]{self.COLOR_RESET}\n"

        else:
            # 未知事件类型
            output = f"{self.COLOR_GRAY}[{event.event_type}]{self.COLOR_RESET} "

        return output

    async def send_message(self, client_id: str, message: str) -> bool:
        """
        发送文本到命令行界面

        对于 CLI，我们将消息添加到缓冲区
        实际的输出由 CLI 客户端主动获取

        Args:
            client_id: 客户端ID
            message: 文本消息

        Returns:
            是否成功
        """
        try:
            # 初始化缓冲区
            if client_id not in self._buffers:
                self._buffers[client_id] = []

            # 添加到缓冲区
            self._buffers[client_id].append(message)

            logger.debug(f"[CLIAdapter] 消息已缓冲 | client_id={client_id} | len={len(message)}")

            return True

        except Exception as e:
            logger.error(f"[CLIAdapter] 缓冲消息失败 | error={e}", exc_info=True)
            return False

    def get_buffered_messages(self, client_id: str, clear: bool = True) -> list[str]:
        """
        获取缓冲的消息

        Args:
            client_id: 客户端ID
            clear: 是否清空缓冲区

        Returns:
            缓冲的消息列表
        """
        messages = self._buffers.get(client_id, [])
        if clear:
            self._buffers[client_id] = []
        return messages


# ============================================
# Bot 适配器（Telegram/Discord/Slack）
# ============================================


class BotAdapter(FrontendAdapter):
    """
    Bot 适配器（Telegram/Discord/Slack）

    特点：
    - Markdown 格式
    - 累积发送，避免频繁请求
    - 支持不同的 Bot API
    - 处理 Bot API 特有的限制
    """

    def __init__(self, bot_type: str = "telegram"):
        """
        初始化适配器

        Args:
            bot_type: Bot 类型（telegram/discord/slack）
        """
        super().__init__()
        self.bot_type = bot_type
        # 每个会话的累积缓冲区
        self._accumulators: dict[str, str] = {}
        # 消息发送回调函数（需要外部设置）
        self._send_callback: callable | None = None

    @property
    def adapter_id(self) -> str:
        """适配器ID"""
        return f"bot_{self.bot_type}"

    def set_send_callback(self, callback: callable):
        """
        设置消息发送回调函数

        Args:
            callback: 发送回调函数，签名为 async def callback(chat_id: str, text: str) -> bool
        """
        self._send_callback = callback
        logger.info(f"[BotAdapter] 发送回调已设置 | bot_type={self.bot_type}")

    async def parse_incoming(self, raw_message: dict[str, Any]) -> UnifiedIncomingMessage:
        """
        解析 Bot API 的 Webhook 消息

        Args:
            raw_message: Bot API 原始消息

        Returns:
            统一输入消息
        """
        try:
            # 根据 Bot 类型解析
            if self.bot_type == "telegram":
                return await self._parse_telegram_message(raw_message)
            elif self.bot_type == "discord":
                return await self._parse_discord_message(raw_message)
            elif self.bot_type == "slack":
                return await self._parse_slack_message(raw_message)
            else:
                raise ValueError(f"不支持的 Bot 类型: {self.bot_type}")

        except Exception as e:
            logger.error(f"[BotAdapter] 解析 Bot 消息失败 | error={e}", exc_info=True)
            raise ValueError(f"无效的 Bot 消息格式: {e}")

    async def _parse_telegram_message(self, raw_message: dict[str, Any]) -> UnifiedIncomingMessage:
        """
        解析 Telegram 消息

        Args:
            raw_message: Telegram Webhook 消息

        Returns:
            统一输入消息
        """
        message = raw_message.get("message", {})
        text = message.get("text", "")

        # 获取 chat_id 和 user_id
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        user = message.get("from", {})
        user_id = str(user.get("id", ""))

        unified_message = UnifiedIncomingMessage(
            type="user_input",
            content=text,
            thread_id=f"telegram-{chat_id}",
            user_id=f"telegram-{user_id}",
            metadata={
                "bot_type": "telegram",
                "chat_id": chat_id,
                "message_id": message.get("message_id"),
                "raw_message": raw_message,
            },
        )

        logger.debug(
            f"[BotAdapter] 解析 Telegram 消息 | chat_id={chat_id} | text={text[:50]}"
        )

        return unified_message

    async def _parse_discord_message(self, raw_message: dict[str, Any]) -> UnifiedIncomingMessage:
        """
        解析 Discord 消息

        Args:
            raw_message: Discord 交互消息

        Returns:
            统一输入消息
        """
        content = raw_message.get("content", "")
        guild_id = raw_message.get("guild_id", "")
        channel_id = raw_message.get("channel_id", "")
        author = raw_message.get("author", {})
        user_id = author.get("id", "")

        unified_message = UnifiedIncomingMessage(
            type="user_input",
            content=content,
            thread_id=f"discord-{channel_id}",
            user_id=f"discord-{user_id}",
            metadata={
                "bot_type": "discord",
                "guild_id": guild_id,
                "channel_id": channel_id,
                "raw_message": raw_message,
            },
        )

        logger.debug(
            f"[BotAdapter] 解析 Discord 消息 | channel_id={channel_id} | content={content[:50]}"
        )

        return unified_message

    async def _parse_slack_message(self, raw_message: dict[str, Any]) -> UnifiedIncomingMessage:
        """
        解析 Slack 消息

        Args:
            raw_message: Slack 事件消息

        Returns:
            统一输入消息
        """
        text = raw_message.get("text", "")
        channel = raw_message.get("channel", "")
        user = raw_message.get("user", "")

        unified_message = UnifiedIncomingMessage(
            type="user_input",
            content=text,
            thread_id=f"slack-{channel}",
            user_id=f"slack-{user}",
            metadata={
                "bot_type": "slack",
                "channel": channel,
                "team": raw_message.get("team"),
                "raw_message": raw_message,
            },
        )

        logger.debug(
            f"[BotAdapter] 解析 Slack 消息 | channel={channel} | text={text[:50]}"
        )

        return unified_message

    async def format_outgoing(self, event: UnifiedStreamEvent) -> dict[str, Any]:
        """
        将统一事件转换为 Bot API 格式

        策略：
        - 流式片段：累积到缓冲区
        - 流式结束：发送完整内容
        - 工具调用：转换为特殊格式的文本

        Args:
            event: 统一流式事件

        Returns:
            Bot API 格式的消息（用于 send_message）
        """
        chat_id = event.thread_id.split("-", 1)[1] if "-" in event.thread_id else event.thread_id

        # 根据事件类型处理
        if event.event_type == "stream.chunk":
            # 流式片段：累积到缓冲区
            content = event.payload.get("content", "")
            self._accumulators[event.message_id] = (
                self._accumulators.get(event.message_id, "") + content
            )
            # 流式片段不立即发送
            return {"type": "buffer", "chat_id": chat_id}

        elif event.event_type == "stream.end":
            # 流式结束：发送完整内容
            full_content = self._accumulators.pop(event.message_id, "")
            # 清空累积器
            if event.message_id in self._accumulators:
                del self._accumulators[event.message_id]
            # 转换为 Markdown 格式
            markdown_text = self._to_markdown(full_content)
            return {"type": "send", "chat_id": chat_id, "text": markdown_text}

        elif event.event_type == "tool.start":
            # 工具调用开始
            tool_name = event.payload.get("tool_name", "")
            args = event.payload.get("args", {})
            args_text = json.dumps(args, ensure_ascii=False) if args else ""
            text = f"🔧 调用工具: `{tool_name}`\n参数: `{args_text}`"
            return {"type": "send", "chat_id": chat_id, "text": text}

        elif event.event_type == "tool.end":
            # 工具调用结束
            status = event.payload.get("status", "")
            if status == "completed":
                result = event.payload.get("result", "")
                text = f"✅ 工具执行成功\n```json\n{json.dumps(result, ensure_ascii=False)}\n```"
            else:
                error = event.payload.get("error", "未知错误")
                text = f"❌ 工具执行失败: `{error}`"
            return {"type": "send", "chat_id": chat_id, "text": text}

        elif event.event_type == "stream.error":
            # 流式错误
            error_msg = event.payload.get("error_message", "未知错误")
            text = f"⚠️ 发生错误: `{error_msg}`"
            return {"type": "send", "chat_id": chat_id, "text": text}

        else:
            # 其他事件类型：忽略或转换为简单文本
            return {"type": "ignore"}

    def _to_markdown(self, text: str) -> str:
        """
        将文本转换为 Markdown 格式

        Args:
            text: 原始文本

        Returns:
            Markdown 格式的文本
        """
        # 简单的 Markdown 转换
        # 实际应用中可能需要更复杂的处理
        return text

    async def send_message(self, client_id: str, message: dict[str, Any]) -> bool:
        """
        发送消息到 Bot

        Args:
            client_id: 客户端ID（chat_id）
            message: Bot API 格式的消息

        Returns:
            是否成功发送
        """
        try:
            msg_type = message.get("type")

            if msg_type == "buffer":
                # 缓冲消息，不立即发送
                return True

            elif msg_type == "send":
                # 发送消息
                if not self._send_callback:
                    logger.warning("[BotAdapter] 发送回调未设置，无法发送消息")
                    return False

                chat_id = message.get("chat_id")
                text = message.get("text", "")

                success = await self._send_callback(chat_id, text)

                if success:
                    logger.debug(
                        f"[BotAdapter] 消息已发送 | chat_id={chat_id} | len={len(text)}"
                    )
                else:
                    logger.warning(f"[BotAdapter] 消息发送失败 | chat_id={chat_id}")

                return success

            elif msg_type == "ignore":
                # 忽略此消息
                return True

            else:
                logger.warning(f"[BotAdapter] 未知的消息类型: {msg_type}")
                return False

        except Exception as e:
            logger.error(f"[BotAdapter] 发送消息异常 | error={e}", exc_info=True)
            return False

    async def flush_accumulator(self, message_id: str, thread_id: str) -> bool:
        """
        强制刷新累积器，发送累积的内容

        Args:
            message_id: 消息ID
            thread_id: 会话ID

        Returns:
            是否成功发送
        """
        if message_id not in self._accumulators:
            return False

        full_content = self._accumulators.pop(message_id)
        chat_id = thread_id.split("-", 1)[1] if "-" in thread_id else thread_id
        markdown_text = self._to_markdown(full_content)

        message = {"type": "send", "chat_id": chat_id, "text": markdown_text}
        return await self.send_message(thread_id, message)


# ============================================
# 适配器注册表
# ============================================


class AdapterRegistry:
    """
    适配器注册表

    管理所有已注册的前端适配器，提供查找和获取功能。
    """

    def __init__(self):
        """初始化注册表"""
        self._adapters: dict[str, FrontendAdapter] = {}

    def register(self, adapter: FrontendAdapter):
        """
        注册适配器

        Args:
            adapter: 前端适配器实例
        """
        adapter_id = adapter.adapter_id
        if adapter_id in self._adapters:
            logger.warning(f"[AdapterRegistry] 适配器已存在，将被覆盖 | adapter_id={adapter_id}")

        self._adapters[adapter_id] = adapter
        logger.info(f"[AdapterRegistry] 适配器已注册 | adapter_id={adapter_id}")

    def get(self, adapter_id: str) -> FrontendAdapter | None:
        """
        获取适配器

        Args:
            adapter_id: 适配器ID

        Returns:
            适配器实例或 None
        """
        return self._adapters.get(adapter_id)

    def list_all(self) -> list[str]:
        """
        列出所有已注册的适配器ID

        Returns:
            适配器ID列表
        """
        return list(self._adapters.keys())

    def is_registered(self, adapter_id: str) -> bool:
        """
        检查适配器是否已注册

        Args:
            adapter_id: 适配器ID

        Returns:
            是否已注册
        """
        return adapter_id in self._adapters


# ============================================
# 全局注册表实例
# ============================================

_adapter_registry: AdapterRegistry | None = None


def get_adapter_registry() -> AdapterRegistry:
    """
    获取适配器注册表单例

    Returns:
        适配器注册表实例
    """
    global _adapter_registry
    if _adapter_registry is None:
        _adapter_registry = AdapterRegistry()

        # 注册默认适配器
        _adapter_registry.register(WebAdapter())
        _adapter_registry.register(CLIAdapter())

        logger.info("[AdapterRegistry] 适配器注册表已初始化，已注册默认适配器")

    return _adapter_registry


def get_adapter(adapter_id: str) -> FrontendAdapter | None:
    """
    获取指定适配器的便捷函数

    Args:
        adapter_id: 适配器ID

    Returns:
        适配器实例或 None
    """
    registry = get_adapter_registry()
    return registry.get(adapter_id)
