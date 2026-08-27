"""输出适配器基类模块（channel_common 渠道共享包）。

单一事实源（channel_{cli,dingtalk,feishu,qq,wecom} 各渠道共用一份全集版）。
消费方经 server.py 把本目录 sys.path.**append** 注入（绝不 insert(0)——模块名
抢占事故纪律，见 docs/working/渠道合流C1C2与CLI插件化方案_20260819.md §三）。
防复发守卫：scripts/check_channel_copy_guard.py 禁止本模块名重回 channel_* 目录。

定义所有输出适配器的抽象接口，负责将管道最终 state
或流式 chunk 转换为外部系统可识别的响应格式。

标准接口：
- send(): 输出管道最终 state（抽象）
- send_stream(): 流式输出 chunk（抽象）
- health_check(): 检查适配器是否健康
- is_connected: 适配器是否已连接
- get_status(): 获取适配器状态信息

具体实现沉淀：
- BufferedChannelOutputAdapter: 缓冲型 IM 渠道（dingtalk/feishu/wecom/qq）
  共用的发送骨架，渠道差异经 _resolve_target/_deliver 扩展点注入。
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from agentos_plugin_sdk.pipeline_types import StateKeys

logger = logging.getLogger(__name__)


class IOutputAdapter(ABC):
    """输出适配器抽象基类。

    负责将管道引擎的处理结果转换为特定外部系统（CLI、API、
    消息队列等）可识别的响应格式。支持一次性输出和流式输出。

    Subclasses:
        CLIOutputAdapter: 命令行输出适配器（支持 rich 彩色输出）
    """

    @abstractmethod
    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state。

        将管道引擎处理完毕的最终 state 转换为外部系统的
        响应格式并输出。

        Args:
            state: 管道引擎的最终 state 字典，通常包括：
                - raw_result: 核心插件的处理结果
                - should_stop: 是否应停止管道循环
                - error: 错误信息（如存在）
        """
        ...

    @abstractmethod
    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出一个 chunk。

        在管道处理过程中，逐 chunk 输出中间结果，
        适用于 LLM 逐 token 生成等场景。

        Args:
            chunk: 流式输出的一个数据块，通常包括：
                - text: 当前 chunk 的文本内容
                - type: chunk 类型（如 "token"、"error"、"system"）
        """
        ...

    async def health_check(self) -> bool:
        """检查输出适配器是否健康。

        默认实现返回 True。网络类适配器应重写此方法，
        执行实际的连接检查。

        Returns:
            True 表示适配器可正常发送消息
        """
        return True

    @property
    def is_connected(self) -> bool:
        """输出适配器是否已连接。

        默认返回 True（如 CLI 适配器始终可用）。
        网络类适配器应重写此属性以反映实际连接状态。

        Returns:
            True 表示适配器已连接并可发送消息
        """
        return True

    def get_status(self) -> dict[str, Any]:
        """获取输出适配器状态信息。

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


class BufferedChannelOutputAdapter(IOutputAdapter):
    """缓冲型渠道输出适配器公共基类（dingtalk/feishu/wecom/qq 共用）。

    沉淀四渠道逐字复制的发送骨架，外部行为契约：

    - send(): 目标解析 → 错误直发 → 正常结果直发；底层渠道 API 发送失败
      时异常原样传播（不吞错、不静默丢消息），管道/gateway 调用方可感知未送达。
    - send_stream(): 累积 chunk 文本，flush 或 type=="end" 到达时一次性投递
      并清空缓冲（渠道不支持逐 token 推送的统一降级语义）。

    渠道差异经以下注入点：

    - channel_name: 渠道名（日志文案），如 "dingtalk"/"QQ"。
    - _resolve_target(): 目标标识校验/规范化（默认恒等；QQ 覆写为整数化）。
    - _deliver(): 渠道客户端投递调用（各渠道客户端 API 形态不同）。

    Attributes:
        _channel_user_id: 兜底目标用户 ID（state 未携带时使用）
        _accumulated_text: 流式累积的文本
    """

    #: 渠道名，日志文案用；子类覆写。
    channel_name: ClassVar[str] = "channel"

    def __init__(self) -> None:
        """初始化输出适配器。"""
        self._channel_user_id: str = ""
        self._accumulated_text: str = ""

    def set_channel_user_id(self, user_id: str) -> None:
        """设置当前消息的兜底目标用户 ID。

        Args:
            user_id: 渠道用户标识
        """
        self._channel_user_id = user_id

    def _resolve_target(self, raw_user_id: str) -> Any | None:
        """校验/规范化发送目标。

        Args:
            raw_user_id: state 内解析到的原始用户标识

        Returns:
            规范化后的目标标识；None 表示无效（调用方跳过本次投递）。
            默认恒等返回；对目标格式有额外要求的渠道覆写。
        """
        return raw_user_id

    @abstractmethod
    async def _deliver(self, target: Any, text: str, state: dict[str, Any]) -> None:
        """投递一条已决策的外发文本到渠道客户端。

        Args:
            target: 经 _resolve_target 规范化后的目标标识
            text: 待投递文本（错误提示或正常结果）
            state: send() 路径传入当前管道 state（供按消息取投递参数的渠道，
                如 QQ 的 _message_type）；send_stream() 路径无管道 state，
                传空 dict（需要回退实例配置的渠道自行处理）
        """

    async def send(self, state: dict[str, Any]) -> None:
        """输出管道最终 state 到渠道。

        失败语义契约：底层渠道 API 发送失败时异常原样传播（不吞错、
        不静默丢消息），管道/gateway 调用方可感知未送达。

        Args:
            state: 管道最终 state 字典

        Raises:
            RuntimeError: 渠道 API 发送失败（经各渠道客户端 send_message 契约上抛）
        """
        raw_target = state.get("_channel_user_id", self._channel_user_id)
        if not raw_target:
            logger.warning("No user_id for %s output, skipping", self.channel_name)
            return

        target = self._resolve_target(raw_target)
        if target is None:
            return

        error = state.get(StateKeys.RAW_ERROR)
        if error:
            await self._deliver(target, f"❌ 错误: {error}", state)
            return

        # 发送正常结果
        result = state.get(StateKeys.RAW_RESULT, "")
        if result:
            await self._deliver(target, str(result), state)

    async def send_stream(self, chunk: dict[str, Any]) -> None:
        """流式输出 chunk：累积文本，flush/end 时一次性投递。

        Args:
            chunk: 流式数据块
        """
        self._accumulated_text += chunk.get("text", "")

        # 如果标记了 flush 或 stream end 且有目标与累积内容，发送并清空
        if (
            (chunk.get("flush", False) or chunk.get("type") == "end")
            and self._channel_user_id
            and self._accumulated_text
        ):
            target = self._resolve_target(self._channel_user_id)
            if target is None:
                return
            await self._deliver(target, self._accumulated_text, {})
            self._accumulated_text = ""
