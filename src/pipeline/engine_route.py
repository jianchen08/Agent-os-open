"""管道引擎 — 路由决策与信号应用。

处理输出路由仲裁结果的应用逻辑（_apply_route），
以及输出插件列表的解析（_resolve_output_plugins）。

公共接口（均通过 PipelineEngine 方法调用）：
- apply_route: 将路由信号应用到管道状态
- resolve_output_plugins: 解析当前迭代的输出插件列表
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pipeline.types import RouteSignal, StateKeys

if TYPE_CHECKING:
    from pipeline.engine import PipelineEngine

logger = logging.getLogger(__name__)


def resolve_output_plugins(
    engine: PipelineEngine,
    state: dict[str, object],
    core_type: str,
) -> list:
    """解析当前迭代需要执行的 Output 插件列表。

    优先使用 output_route_table 的插件路由（与 input_routes 对称），
    当路由表中没有声明 plugins 字段时，回退到 registry 获取全部输出插件。
    兼容测试中使用的 Mock 路由表（无 has_plugin_routing 方法）。

    Args:
        engine: PipelineEngine 实例
        state: 管道当前状态字典
        core_type: 当前核心类型标识

    Returns:
        匹配的输出插件实例列表
    """
    from pipeline.plugin import IOutputPlugin  # noqa: PLC0415

    ort = engine.output_route_table
    if hasattr(ort, "has_plugin_routing") and ort.has_plugin_routing():
        plugin_names = ort.resolve_plugins(state)
        if plugin_names:
            plugins: list[IOutputPlugin] = []
            for name in plugin_names:
                plugin = engine.plugin_registry.get(name)
                if isinstance(plugin, IOutputPlugin):
                    plugins.append(plugin)
                else:
                    logger.debug(
                        "Output route plugin '%s' not found or not IOutputPlugin, skipping",
                        name,
                    )
            return sorted(plugins, key=lambda p: p.priority)

    return engine.plugin_registry.get_output_plugins(core_type=core_type)


async def apply_route(  # noqa: PLR0911
    engine: PipelineEngine,
    route: RouteSignal,
    state: dict[str, object],
) -> bool:
    """应用路由信号到管道状态。

    根据路由类型更新状态字典：
    - next_llm → state["core_type"] = "llm_call"
      但当 AI 纯文本输出（无 tool_calls）且无新用户输入时，
      自动降级为 wait（挂起等用户输入），防止管道空转。
    - next_tool → state["core_type"] = "tool_execute"
    - end → state["ended"] = True
    - wait → 保存挂起状态快照

    Args:
        engine: PipelineEngine 实例
        route: 仲裁后的路由信号
        state: 管道状态字典（原地修改）

    Returns:
        是否应中断管道循环（wait 时为 True）
    """
    route_type = route.route_type

    if route_type == "next_llm":
        # 检测 AI 纯文本输出 + 无新用户输入的组合，自动降级为 wait
        _raw_result = state.get("raw_result", "")
        _has_tool_calls = bool(state.get(StateKeys.RAW_TOOL_CALLS, []))
        # 增加 core_type 检查，避免 tool_execute 场景下的误判
        _core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
        _is_text_only = bool(_raw_result and not _has_tool_calls and _core_type == "llm_call")

        # 输出插件可能注入了新的系统消息，这是实质性的新输入
        _has_new_input = bool(state.pop("_has_new_llm_input", False))

        if _is_text_only and not _has_new_input:
            # AI 只输出了文本，没有调用任何工具。
            # 检查是否有新通知注入（如 task_event_receiver 的完成通知）
            # 通知注入统一走 consume_pending_notifications，不在路由分支内联重复。
            from pipeline.engine_iteration import consume_pending_notifications  # noqa: PLC0415
            if consume_pending_notifications(engine, state):
                return False

            # 无新输入，降级为 wait（挂起等用户反馈）
            logger.info(
                "Route next_llm + text-only output (no new input): "
                "downgrading to wait, suspending pipeline"
            )
            restored = await engine.suspend_and_wait(state)
            if restored:
                state[StateKeys.CORE_TYPE] = "llm_call"
                return False
            return True

        state[StateKeys.CORE_TYPE] = "llm_call"
        logger.debug("Route applied: next_llm")
        return False

    if route_type == "next_tool":
        state[StateKeys.CORE_TYPE] = "tool_execute"
        if route.target:
            state["tool_name"] = route.target
        logger.debug("Route applied: next_tool, target=%s", route.target)
        return False

    if route_type == "end":
        # 通知注入统一走 consume_pending_notifications，不在路由分支内联重复。
        from pipeline.engine_iteration import consume_pending_notifications  # noqa: PLC0415
        if consume_pending_notifications(engine, state):
            logger.info("[Engine] route=end 但有待处理通知，取消结束: %s", route.reason)
            return False
        state[StateKeys.ENDED] = True
        logger.debug("Route applied: end, reason=%s", route.reason)
        return False

    if route_type == "wait":
        state[StateKeys.ENDED] = False
        logger.debug("Route applied: wait, pipeline suspended")
        # 恢复逻辑已内置到 _suspend_and_wait
        restored = await engine.suspend_and_wait(state)
        if restored:
            logger.info("Pipeline woken up from output wait, resetting CORE_TYPE to llm_call")
            state[StateKeys.CORE_TYPE] = "llm_call"
            return False
        return True

    logger.warning("Unknown route type: %s, defaulting to end", route_type)
    state[StateKeys.ENDED] = True
    return False
