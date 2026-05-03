"""能力缺口自动识别与进化触发器。

在 Agent 执行任务的关键节点被调用：
- 工具调用失败时（工具不存在）
- Agent 报告能力不足时
- 用户请求不存在的功能时

职责：
- 检测能力缺口信号
- 调用 GapAnalyzer 分析
- 如果需要生成插件，通过 EvolutionEngine 执行闭环
- 记录触发事件到内部历史
- 通过 EventBus 广播触发事件

暴露接口：
- EvolutionTrigger: 触发器主类
- TriggerMode: 触发模式枚举（AUTO / SUGGEST）
"""

from __future__ import annotations

import logging
import threading
import time
from enum import Enum
from typing import Any

from evolution.trigger_types import TriggerEvent, TriggerResult, make_timestamp

logger = logging.getLogger(__name__)


class TriggerMode(Enum):
    """触发模式。

    - AUTO: 自动模式，检测到缺口后直接调用 EvolutionEngine 执行进化
    - SUGGEST: 建议模式，检测到缺口后只返回建议，不执行进化
    """

    AUTO = "auto"
    SUGGEST = "suggest"


class EvolutionTrigger:
    """能力缺口自动识别与进化触发器。

    桥接 Agent 运行时与 evolution 模块，在关键节点检测能力缺口并触发进化流程。

    关键设计：
    - 频率限制：防止无限循环进化（max_triggers_per_minute）
    - 双模式：自动模式直接执行，建议模式只报告
    - 线程安全：使用锁保护内部状态
    - 事件广播：通过 EventBus 发射触发事件

    集成点：
    - ToolCore 的 _execute_single_tool 中工具未找到时可调用 check_tool_not_found
    - Agent 可主动调用 check_capability_gap 报告能力不足

    Attributes:
        _engine: EvolutionEngine 实例
        _event_bus: EventBus 实例（可选）
        _mode: 触发模式
        _max_triggers_per_minute: 每分钟最大触发次数
        _trigger_timestamps: 触发时间戳列表（用于频率限制）
        _trigger_history: 触发事件历史
        _lock: 线程锁
        _event_queue: 待发射的事件队列
    """

    def __init__(
        self,
        evolution_engine: Any,
        event_bus: Any | None = None,
        *,
        mode: TriggerMode = TriggerMode.AUTO,
        max_triggers_per_minute: int = 3,
    ) -> None:
        """初始化触发器。

        Args:
            evolution_engine: EvolutionEngine 实例，需提供 evolve() 方法
            event_bus: EventBus 实例（可选），用于广播触发事件
            mode: 触发模式，AUTO 自动执行，SUGGEST 只建议
            max_triggers_per_minute: 每分钟最大触发次数，防止无限循环
        """
        self._engine = evolution_engine
        self._event_bus = event_bus
        self.mode = mode
        self.max_triggers_per_minute = max_triggers_per_minute
        self._trigger_timestamps: list[float] = []
        self._trigger_history: list[TriggerEvent] = []
        self._lock = threading.Lock()
        self._event_queue: list[TriggerEvent] = []

    def check_tool_not_found(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> TriggerResult:
        """工具未找到时调用，检查是否需要进化。

        在 ToolCore 的 _execute_single_tool 中工具未找到时调用。
        会构造能力描述并触发进化流程。

        Args:
            tool_name: 未找到的工具名称
            tool_args: 工具调用参数

        Returns:
            触发结果
        """
        if not tool_name or not tool_name.strip():
            return TriggerResult.not_triggered("工具名为空，跳过触发")

        capability = f"工具 '{tool_name}' 的执行能力"
        context: dict[str, Any] = {
            "trigger_source": "tool_not_found",
            "tool_name": tool_name,
            "tool_args": tool_args,
        }

        return self._handle_trigger(
            trigger_type="tool_not_found",
            capability=capability,
            context=context,
        )

    def check_capability_gap(
        self,
        capability_description: str,
        context: dict[str, Any],
    ) -> TriggerResult:
        """Agent 主动报告能力缺口时调用。

        Agent 在执行任务过程中发现自身缺少某种能力时调用。

        Args:
            capability_description: 缺失的能力描述
            context: 附加上下文信息

        Returns:
            触发结果
        """
        if not capability_description or not capability_description.strip():
            return TriggerResult.not_triggered("能力描述为空，跳过触发")

        enriched_context: dict[str, Any] = {
            **context,
            "trigger_source": "capability_gap",
        }

        return self._handle_trigger(
            trigger_type="capability_gap",
            capability=capability_description,
            context=enriched_context,
        )

    def trigger_evolution(
        self,
        capability: str,
        context: dict[str, Any],
    ) -> TriggerResult:
        """手动触发进化流程。

        不受建议模式限制，直接调用引擎执行进化。

        Args:
            capability: 需要的能力描述
            context: 附加上下文

        Returns:
            触发结果
        """
        enriched_context: dict[str, Any] = {
            **context,
            "trigger_source": "manual",
        }

        event = TriggerEvent(
            trigger_type="manual",
            capability=capability,
            context=enriched_context,
            timestamp=make_timestamp(),
        )
        self._record_event(event)

        try:
            evo_result = self._engine.evolve(capability, context=enriched_context)
            return TriggerResult(
                triggered=True,
                evolution_result=evo_result,
                message=f"手动触发进化完成: {evo_result.message}",
            )
        except Exception as exc:
            logger.error(
                "[EvolutionTrigger] 手动触发进化异常: capability='%s', error=%s",
                capability,
                exc,
            )
            return TriggerResult(
                triggered=True,
                evolution_result=None,
                message=f"手动触发进化失败: {exc}",
            )

    def should_auto_trigger(self) -> bool:
        """检查当前是否允许自动触发。

        受频率限制控制，超出限制时返回 False。

        Returns:
            是否允许自动触发
        """
        return self._check_rate_limit()

    def get_trigger_history(self) -> list[TriggerEvent]:
        """获取触发事件历史。

        Returns:
            触发事件列表（最新在前）
        """
        with self._lock:
            return list(reversed(self._trigger_history))

    # -- 内部方法 --------------------------------------------------------

    def _handle_trigger(
        self,
        trigger_type: str,
        capability: str,
        context: dict[str, Any],
    ) -> TriggerResult:
        """统一处理触发逻辑。

        根据模式（AUTO/SUGGEST）和频率限制决定是否执行进化。

        Args:
            trigger_type: 触发类型
            capability: 能力描述
            context: 上下文

        Returns:
            触发结果
        """
        # 建议模式：只返回建议
        if self.mode == TriggerMode.SUGGEST:
            event = TriggerEvent(
                trigger_type=trigger_type,
                capability=capability,
                context=context,
                timestamp=make_timestamp(),
            )
            self._record_event(event)
            return TriggerResult.suggest(
                message=f"检测到能力缺口: {capability}",
                suggestion=f"建议进化以获取 {capability} 能力",
            )

        # 自动模式：检查频率限制
        if not self._check_rate_limit():
            return TriggerResult.not_triggered(
                f"触发频率已达上限（{self.max_triggers_per_minute}次/分钟），跳过: {capability}"
            )

        # 记录触发时间戳
        now = time.time()
        with self._lock:
            self._trigger_timestamps.append(now)

        # 记录事件
        event = TriggerEvent(
            trigger_type=trigger_type,
            capability=capability,
            context=context,
            timestamp=make_timestamp(),
        )
        self._record_event(event)

        # 执行进化
        try:
            evo_result = self._engine.evolve(capability, context=context)
            return TriggerResult(
                triggered=True,
                evolution_result=evo_result,
                message=f"进化触发完成: {evo_result.message}",
            )
        except Exception as exc:
            logger.error(
                "[EvolutionTrigger] 进化触发异常: capability='%s', error=%s",
                capability,
                exc,
            )
            return TriggerResult(
                triggered=True,
                evolution_result=None,
                message=f"进化触发失败: {exc}",
            )

    def _check_rate_limit(self) -> bool:
        """检查频率限制。

        清理过期时间戳，检查当前是否在限制内。

        Returns:
            是否允许触发
        """
        now = time.time()
        window = 60.0  # 1 分钟窗口

        with self._lock:
            # 清理过期时间戳
            self._trigger_timestamps = [
                ts for ts in self._trigger_timestamps
                if now - ts < window
            ]
            return len(self._trigger_timestamps) < self.max_triggers_per_minute

    def _record_event(self, event: TriggerEvent) -> None:
        """记录触发事件到历史和事件队列。

        Args:
            event: 触发事件
        """
        with self._lock:
            self._trigger_history.append(event)
            self._event_queue.append(event)

        logger.info(
            "[EvolutionTrigger] 记录触发事件: type='%s', capability='%s', id='%s'",
            event.trigger_type,
            event.capability,
            event.trigger_id,
        )
