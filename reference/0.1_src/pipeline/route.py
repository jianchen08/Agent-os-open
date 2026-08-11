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

    路由只做决策（返回 target），不生成内容、不写状态。
    拦截原因等内容生成由执行点（如 tool_core 的工具失败结果）负责。

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
    该条目生效。同时可通过 plugins 字段声明该条目关联的输出插件列表，
    与 InputRouteEntry.plugins 对称，实现按 core_type 区分输出插件。

    Attributes:
        name: 路由条目名称（标识用途）
        route_type: 匹配的路由信号类型
        condition: Python 布尔表达式字符串，空字符串视为始终匹配
        priority: 优先级，数值越小优先级越高
        target_core: 路由到核心插件时指定的核心类型
        plugins: 该条目匹配时需要执行的输出插件名称列表
    """

    name: str = ""
    route_type: str = ""
    condition: str = ""
    priority: int = 0
    target_core: str | None = None
    plugins: list[str] = field(default_factory=list)


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
        matched_entries = [e for e in self.entries if _eval_condition(e.condition, state)]

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
        self.entries: list[OutputRouteEntry] = sorted(entries or [], key=lambda e: e.priority)

    def resolve_plugins(self, state: dict[str, Any]) -> list[str]:
        """根据 state 解析需要执行的 output 插件列表。

        遍历所有条目，收集所有条件匹配的条目的插件列表。
        插件列表去重保序，与 InputRouteTable.resolve_plugins() 对称。

        当没有任何条目声明 plugins 字段时返回空列表，
        调用方应回退到 registry.get_output_plugins() 获取全部插件。

        Args:
            state: 管道当前状态字典

        Returns:
            去重保序的插件名称列表；无匹配或无 plugins 声明时返回空列表
        """
        matched_entries = [e for e in self.entries if _eval_condition(e.condition, state)]

        if not matched_entries:
            return []

        seen: set[str] = set()
        plugins: list[str] = []
        for entry in matched_entries:
            for plugin_name in entry.plugins:
                if plugin_name not in seen:
                    seen.add(plugin_name)
                    plugins.append(plugin_name)

        return plugins

    def has_plugin_routing(self) -> bool:
        """检查是否有任何条目声明了 plugins 字段。

        用于调用方判断是否启用基于路由表的插件过滤，
        还是回退到 registry 获取全部插件。

        Returns:
            存在声明 plugins 的条目时返回 True
        """
        return any(e.plugins for e in self.entries)

    def arbitrate(self, signals: list[RouteSignal], state: dict[str, Any]) -> RouteSignal:
        """仲裁输出路由信号。

        遍历排序后的条目，寻找第一个同时匹配 route_type 和 condition 的条目。
        匹配规则：条目的 route_type 与某个信号的 route_type 相同，
        且条目的 condition 对当前 state 求值为 True。

        end 信号具有最高优先级：当 end 和 next_llm 同时存在时，
        end 始终胜出，避免关键结束信号被低优先级信号覆盖。

        Args:
            signals: 输出插件产生的路由信号列表
            state: 管道当前状态字典

        Returns:
            仲裁后的路由信号；无匹配时返回 fallback 信号
        """
        signal_types = {s.route_type for s in signals}

        # end 信号最高优先：任何插件发出 end 时立即生效，
        # 防止 next_llm 等继续信号覆盖终止意图
        if "end" in signal_types:
            for entry in self.entries:
                if entry.route_type == "end" and _eval_condition(entry.condition, state):
                    matched_signal = next(s for s in signals if s.route_type == "end")
                    return RouteSignal(
                        route_type="end",
                        target=entry.target_core,
                        reason=matched_signal.reason or "matched route entry: end",
                        payload=matched_signal.payload,
                    )

        for entry in self.entries:
            if entry.route_type in signal_types and _eval_condition(entry.condition, state):
                matched_signal = next(s for s in signals if s.route_type == entry.route_type)
                result = RouteSignal(
                    route_type=entry.route_type,
                    target=entry.target_core,
                    reason=matched_signal.reason or f"matched route entry: {entry.route_type}",
                    payload=matched_signal.payload,
                )
                return result

        # 无匹配，返回 fallback
        return RouteSignal(route_type="end", reason="fallback")
