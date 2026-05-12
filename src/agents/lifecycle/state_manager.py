"""
状态管理器 - 负责 Agent 状态的维护和转换

职责：
- 管理 Agent 的当前状态
- 处理状态转换
- 提供状态查询接口
- 处理停止请求
"""

import logging

from src.agents.types import AgentLifecycleState, ToolCallRecord

logger = logging.getLogger(__name__)


class StateManager:
    """
    状态管理器

    负责 Agent 状态的维护和转换
    """

    def __init__(self, initial_state: AgentLifecycleState = AgentLifecycleState.IDLE):
        """
        初始化状态管理器

        Args:
            initial_state: 初始状态
        """
        self._state = initial_state
        self._stop_requested = False
        self._pause_requested = False
        self._tool_calls: list[ToolCallRecord] = []

    @property
    def state(self) -> AgentLifecycleState:
        """获取当前状态"""
        return self._state

    @property
    def stop_requested(self) -> bool:
        """是否请求停止"""
        return self._stop_requested

    @property
    def pause_requested(self) -> bool:
        """是否请求暂停"""
        return self._pause_requested

    @property
    def tool_calls(self) -> list[ToolCallRecord]:
        """获取工具调用记录"""
        return self._tool_calls.copy()

    def set_state(self, state: AgentLifecycleState) -> None:
        """
        设置状态

        Args:
            state: 新状态
        """
        old_state = self._state
        self._state = state
        logger.debug(f"[StateManager] 状态转换 | {old_state.value} -> {state.value}")

    def request_stop(self) -> None:
        """请求停止执行"""
        self._stop_requested = True
        logger.debug("[StateManager] 请求停止执行")

    def request_pause(self) -> None:
        """请求暂停执行"""
        self._pause_requested = True
        logger.debug("[StateManager] 请求暂停执行")

    def resume(self) -> None:
        """恢复执行"""
        self._pause_requested = False
        logger.debug("[StateManager] 恢复执行")

    def reset(self) -> None:
        """重置状态（用于新的执行）"""
        self._state = AgentLifecycleState.IDLE
        self._stop_requested = False
        self._pause_requested = False
        self._tool_calls = []
        logger.debug("[StateManager] 状态已重置")

    def prepare_for_execution(self) -> None:
        """准备执行（设置运行状态）"""
        self._state = AgentLifecycleState.RUNNING
        self._stop_requested = False
        self._pause_requested = False
        self._tool_calls = []
        logger.debug("[StateManager] 准备执行")

    def add_tool_call(self, tool_call: ToolCallRecord) -> None:
        """
        添加工具调用记录

        Args:
            tool_call: 工具调用记录
        """
        self._tool_calls.append(tool_call)

    def set_tool_calls(self, tool_calls: list[ToolCallRecord]) -> None:
        """
        设置工具调用记录列表

        Args:
            tool_calls: 工具调用记录列表
        """
        self._tool_calls = tool_calls.copy()

    def get_tool_calls_count(self) -> int:
        """
        获取工具调用次数

        Returns:
            工具调用次数
        """
        return len(self._tool_calls)

    def should_stop(self) -> bool:
        """
        是否应该停止执行

        Returns:
            True 如果应该停止
        """
        return self._stop_requested

    def cleanup(self) -> None:
        """清理状态管理器资源"""
        logger.debug("[StateManager] 状态管理器资源已清理")
