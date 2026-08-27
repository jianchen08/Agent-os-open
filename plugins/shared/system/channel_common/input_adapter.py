"""输入适配器基类模块（channel_common 渠道共享包）。

单一事实源：四渠道的输入适配器公共实现只在本文维护，各渠道插件目录不得再放
同名 input_adapter.py（scripts/check_channel_copy_guard.py 守卫复制回潮）。
路径注入契约：本目录由各渠道 server.py 以 sys.path.append 引入、绝不 insert(0)——
本目录模块名是通用名，insert(0) 会遮蔽其他目录的同名模块，
谁在前谁生效。完整背景见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §三。

定义所有输入适配器的抽象接口，负责从外部系统接收请求
并转换为管道可处理的初始 state。

标准接口：
- receive(): 接收外部请求（抽象）
- health_check(): 检查适配器是否健康
- is_connected: 适配器是否已连接
- get_status(): 获取适配器状态信息

具体实现沉淀：
- QueuedChannelInputAdapter: 队列缓冲型 IM 渠道（dingtalk/feishu/wecom/qq）
  共用的收消息机制，渠道差异点（原始报文解析）保留在子类 _raw_to_state。
- build_channel_state(): 四渠道同构的管道初始 state 公共信封构造器。
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Any

from agentos_plugin_sdk.pipeline_types import StateKeys


class IInputAdapter(ABC):
    """输入适配器抽象基类。

    负责从特定外部源（CLI、API、消息队列等）接收请求，
    并将其转换为管道引擎可处理的初始 state 字典。

    Subclasses:
        CLIInputAdapter: 命令行输入适配器
    """

    @abstractmethod
    async def receive(self) -> dict[str, Any]:
        """接收外部请求，返回初始 state。

        从外部源读取输入，将其封装为管道引擎的初始 state 字典。
        该方法应为异步非阻塞调用。

        Returns:
            dict[str, Any]: 包含初始管道状态的字典，通常包括：
                - user_input: 用户输入文本
                - core_type: 请求的核心处理类型
                - session_id: 会话唯一标识
                - should_stop: 是否应停止管道循环
                - iteration: 当前迭代次数（初始为 1）
        """
        ...

    async def health_check(self) -> bool:
        """检查输入适配器是否健康。

        默认实现返回 True。网络类适配器应重写此方法，
        执行实际的连接检查。

        Returns:
            True 表示适配器可正常接收消息
        """
        return True

    @property
    def is_connected(self) -> bool:
        """输入适配器是否已连接。

        默认返回 True（如 CLI 适配器始终可用）。
        网络类适配器应重写此属性以反映实际连接状态。

        Returns:
            True 表示适配器已连接并可接收消息
        """
        return True

    def get_status(self) -> dict[str, Any]:
        """获取输入适配器状态信息。

        Returns:
            包含适配器状态的字典，至少包含：
                - type: 适配器类名
                - connected: 是否已连接
                - healthy: 是否健康
        """
        return {
            "type": self.__class__.__name__,
            "connected": self.is_connected,
            "healthy": True,
        }


def build_channel_state(
    *,
    channel_type: str,
    user_input: str,
    session_id: str,
    channel_user_id: str,
    raw_message: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    """组装四渠道同构的管道初始 state 公共信封。

    渠道特有的附加键经 **extra 注入（如 dingtalk 的 _sender_id、
    qq 的 _message_type），置于公共键之后。

    Args:
        channel_type: 通道类型标识（dingtalk/feishu/wecom/qq）
        user_input: 提取后的用户输入文本
        session_id: 会话唯一标识
        channel_user_id: 渠道用户标识（回复目标）
        raw_message: 原始报文（透传给管道供调试/追踪）
        **extra: 渠道特有附加键

    Returns:
        管道初始 state 字典
    """
    return {
        "user_input": user_input,
        StateKeys.CORE_TYPE: "llm_call",
        StateKeys.SESSION_ID: session_id,
        StateKeys.SHOULD_STOP: False,
        "iteration": 1,
        "_channel_type": channel_type,
        "_channel_user_id": channel_user_id,
        **extra,
        "_raw_message": raw_message,
    }


def unsupported_message_text(channel_type: str, msg_type: str) -> str:
    """未支持/未识别消息类型的统一拒收标记。

    非文本报文不得转储成字符串伪装成 user_input 喂给下游——
    返回带渠道与消息类型的显式标记，让调用方与用户能识别
    "这条消息未被解析"而非当作真实输入。

    Args:
        channel_type: 渠道标识（dingtalk/feishu/wecom 等）
        msg_type: 原始消息类型字段值

    Returns:
        形如 "[不支持的消息类型: dingtalk/picture]" 的标记串
    """
    return f"[不支持的消息类型: {channel_type}/{msg_type}]"


class QueuedChannelInputAdapter(IInputAdapter):
    """队列缓冲型输入适配器公共基类（dingtalk/feishu/wecom/qq 共用）。

    沉淀四渠道逐字复制的收消息机制：

    - asyncio.Queue 缓冲外部回调投递的原始报文；
    - enqueue_message(): 供渠道客户端的 on_message 回调写入；
    - receive(): 阻塞取下一条并经子类 _raw_to_state 转为管道 state。

    渠道差异点（原始报文字段解析）保留在子类 _raw_to_state，
    信封组装统一走 build_channel_state()。
    """

    def __init__(self) -> None:
        """初始化输入适配器：创建消息缓冲队列。"""
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def enqueue_message(self, raw_message: dict[str, Any]) -> None:
        """将原始消息放入处理队列。

        由各渠道客户端（Stream 客户端 / OneBot 客户端）的
        on_message 回调调用。

        Args:
            raw_message: 渠道原始消息事件数据
        """
        await self._message_queue.put(raw_message)

    async def receive(self) -> dict[str, Any]:
        """从队列中取出下一条渠道消息，转换为管道初始 state。

        阻塞等待直到有消息可用。

        Returns:
            管道初始 state 字典
        """
        return self._raw_to_state(await self._message_queue.get())

    @staticmethod
    @abstractmethod
    def _raw_to_state(raw: dict[str, Any]) -> dict[str, Any]:
        """将渠道原始消息转换为管道 state（渠道各自实现解析）。

        Args:
            raw: 渠道原始消息事件数据

        Returns:
            管道初始 state 字典（经 build_channel_state 组装）
        """
