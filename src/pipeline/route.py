"""路由表与路由信号定义。

实现输入路由表（可叠加匹配）和输出路由表（互斥优先级仲裁），
控制管道循环中插件的选取和信号的决策。
"""

from __future__ import annotations

import logging
import re
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
        result: 拦截原因模板，支持点号路径访问 state 嵌套字段
    """

    name: str
    condition: str = ""
    target: str = "core"
    plugins: list[str] = field(default_factory=list)
    priority: int = 0
    result: str | None = None

    def format_result(self, state: dict[str, Any]) -> str:
        """格式化拦截原因模板。

        支持点号路径访问 state 嵌套字段，例如模板
        ``{security.decision.reason}`` 会从 state 中逐层查找
        ``state["security"]["decision"]["reason"]`` 的值。
        找不到的路径用空字符串替代。

        Args:
            state: 管道状态字典

        Returns:
            填充后的字符串；result 为 None 时返回空字符串
        """
        if self.result is None:
            return ""

        def _resolve(path: str) -> str:
            """按点号分割路径，从 state 中逐层查找值。"""
            keys = path.split(".")
            value: Any = state
            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return ""
            return str(value)

        return re.sub(r"\{([^}]+)\}", lambda m: _resolve(m.group(1)), self.result)


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

    def resolve_plugins(self, state: dict[str, Any]) -> list[str]:
        """根据 state 解析需要执行的 input 插件列表。

        遍历所有条目，收集所有条件匹配的条目的插件列表。
        插件列表去重保序。

        Args:
            state: 管道当前状态字典

        Returns:
            去重保序的插件名称列表
        """
        matched_entries = [
            e for e in self.entries if _eval_condition(e.condition, state)
        ]

        if not matched_entries:
            return []

        # 插件去重保序
        seen: set[str] = set()
        plugins: list[str] = []
        for entry in matched_entries:
            for plugin_name in entry.plugins:
                if plugin_name not in seen:
                    seen.add(plugin_name)
                    plugins.append(plugin_name)

        return plugins

    def resolve_target(self, state: dict[str, Any]) -> tuple[str, InputRouteEntry | None]:
        """根据 state 解析路由目标和匹配的条目。

        遍历所有条件匹配的条目，按优先级决定目标：
        - "end" 立即结束管道
        - "wait" 挂起管道
        - "core" 继续执行核心阶段
        end/wait 具有最高优先级：任一条目指定 end/wait 即生效。

        Args:
            state: 管道当前状态字典

        Returns:
            元组 (target, matched_entry)：
            - target: "core" / "end" / "wait"
            - matched_entry: 优先级最高的匹配条目（用于读取 result 模板）；
              无匹配时为 None
        """
        matched_entries = sorted(
            [e for e in self.entries if _eval_condition(e.condition, state)],
            key=lambda e: e.priority,
        )

        if not matched_entries:
            return "core", None

        # 默认取优先级最高的匹配条目
        best_entry = matched_entries[0]
        target = best_entry.target

        # end 和 wait 具有最高优先级：任一条目指定 end/wait 即生效
        for entry in matched_entries:
            if entry.target == "end":
                target = "end"
                best_entry = entry
                break
            if entry.target == "wait":
                target = "wait"
                best_entry = entry
                break

        return target, best_entry

    def resolve(self, state: dict[str, Any]) -> tuple[list[str], str]:
        """根据当前状态解析输入路由（兼容方法）。

        内部委托给 resolve_plugins() 和 resolve_target()。

        Args:
            state: 管道当前状态字典

        Returns:
            元组 (去重保序的插件名称列表, 目标字符串)
        """
        plugins = self.resolve_plugins(state)
        target, _ = self.resolve_target(state)
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
