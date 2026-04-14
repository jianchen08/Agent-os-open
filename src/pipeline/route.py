"""路由表与路由信号定义。

实现输入路由表（可叠加匹配）和输出路由表（互斥优先级仲裁），
控制管道循环中插件的选取和信号的决策。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pipeline.condition_parser import parse_condition
from pipeline.types import RouteSignal

logger = logging.getLogger(__name__)


def _eval_condition(condition: str, state: dict[str, Any]) -> bool:
    """安全求值条件表达式。

    使用 condition_parser 替代 eval()，杜绝代码注入风险。

    Args:
        condition: 条件表达式字符串
        state: 管道状态字典，作为求值上下文

    Returns:
        条件求值结果
    """
    return parse_condition(condition, state)


@dataclass
class InputRouteEntry:
    """输入路由条目。

    定义一个输入阶段的路由规则：当条件满足时，
    选取指定插件并将管道引向指定目标。

    Attributes:
        name: 路由条目名称
        condition: Python 布尔表达式字符串，空字符串视为始终匹配
        target: 路由目标：core / end / wait
        plugins: 要执行的插件名称列表
        priority: 优先级，数值越小越先匹配
    """

    name: str
    condition: str = ""
    target: str = "core"
    plugins: list[str] = field(default_factory=list)
    priority: int = 0


@dataclass
class OutputRouteEntry:
    """输出路由条目。

    定义一个输出阶段的路由规则：当 route_type 和条件同时满足时，
    该条目生效。

    Attributes:
        route_type: 匹配的路由信号类型
        condition: Python 布尔表达式字符串，空字符串视为始终匹配
        priority: 优先级，数值越小优先级越高
        target_core: 路由到核心插件时指定的核心类型
    """

    route_type: str
    condition: str = ""
    priority: int = 0
    target_core: str | None = None


class InputRouteTable:
    """输入路由表。

    可叠加匹配：遍历所有条目，条件为 True 的全部收集。
    插件列表去重保序，target 按最高优先级的匹配条目决定。

    Attributes:
        entries: 输入路由条目列表
    """

    def __init__(self, entries: list[InputRouteEntry] | None = None) -> None:
        self.entries: list[InputRouteEntry] = entries or []

    def resolve(self, state: dict[str, Any]) -> tuple[list[str], str]:
        """根据当前状态解析输入路由。

        遍历所有条目，条件匹配的条目贡献其插件和目标。
        插件列表去重保序；target 由优先级最高的匹配条目决定：
        - "end" 立即结束管道
        - "wait" 挂起管道
        - "core" 继续执行核心阶段

        Args:
            state: 管道当前状态字典

        Returns:
            元组 (去重保序的插件名称列表, 目标字符串)
        """
        matched_entries = sorted(
            [e for e in self.entries if _eval_condition(e.condition, state)],
            key=lambda e: e.priority,
        )

        if not matched_entries:
            return [], "core"

        # 插件去重保序
        seen: set[str] = set()
        plugins: list[str] = []
        for entry in matched_entries:
            for plugin_name in entry.plugins:
                if plugin_name not in seen:
                    seen.add(plugin_name)
                    plugins.append(plugin_name)

        # target 取优先级最高的匹配条目
        target = matched_entries[0].target

        # end 和 wait 具有最高优先级：任一条目指定 end/wait 即生效
        for entry in matched_entries:
            if entry.target == "end":
                target = "end"
                break
            if entry.target == "wait":
                target = "wait"
                break

        return plugins, target


class OutputRouteTable:
    """输出路由表。

    互斥优先级仲裁：按 priority 排序，
    第一个同时匹配 route_type 和 condition 的条目生效。
    无匹配时返回 fallback 信号。

    Attributes:
        entries: 输出路由条目列表
    """

    def __init__(self, entries: list[OutputRouteEntry] | None = None) -> None:
        self.entries: list[OutputRouteEntry] = sorted(
            entries or [], key=lambda e: e.priority
        )

    def arbitrate(self, signals: list[RouteSignal], state: dict[str, Any]) -> RouteSignal:
        """仲裁输出路由信号。

        遍历排序后的条目，寻找第一个同时匹配 route_type 和 condition 的条目。
        匹配规则：条目的 route_type 与某个信号的 route_type 相同，
        且条目的 condition 对当前 state 求值为 True。

        Args:
            signals: 输出插件产生的路由信号列表
            state: 管道当前状态字典

        Returns:
            仲裁后的路由信号；无匹配时返回 fallback 信号
        """
        signal_types = {s.route_type for s in signals}

        for entry in self.entries:
            if entry.route_type in signal_types:
                if _eval_condition(entry.condition, state):
                    # 找到匹配的信号
                    matched_signal = next(
                        s for s in signals if s.route_type == entry.route_type
                    )
                    result = RouteSignal(
                        route_type=entry.route_type,
                        target=entry.target_core,
                        reason=matched_signal.reason or f"matched route entry: {entry.route_type}",
                        payload=matched_signal.payload,
                    )
                    return result

        # 无匹配，返回 fallback
        return RouteSignal(route_type="end", reason="fallback")
