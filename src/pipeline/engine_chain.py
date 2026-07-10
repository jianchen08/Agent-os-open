"""管道引擎 — 插件链执行与核心插件重试逻辑。

将 Input/Core/Output 插件链的执行逻辑从 _run_loop 中提取，
降低主循环的圈复杂度。

公共接口（均通过 PipelineEngine 方法调用）：
- execute_input_chain: 执行 Input 插件链
- execute_core_plugin: 执行 Core 插件（含重试和错误追踪）
- execute_output_chain: 执行 Output 插件链并收集路由信号
- handle_no_route_signals: 处理无路由信号时的后续逻辑
- run_post_end_output_chain: 管道结束后再执行一次 Output 链
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pipeline.chain import PluginChain
from pipeline.plugin import (
    IInputPlugin,
    PluginContext,
)
from pipeline.types import RouteSignal, StateKeys

if TYPE_CHECKING:
    from pipeline.engine import PipelineEngine

logger = logging.getLogger(__name__)


async def execute_input_chain(
    engine: PipelineEngine,
    state: dict[str, Any],
    plugin_names: list[str],
) -> None:
    """获取并执行 Input 插件链。

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典（原地修改）
        plugin_names: 由路由表解析出的插件名列表
    """
    input_plugins: list[IInputPlugin] = []
    for name in plugin_names:
        plugin = engine.plugin_registry.get(name)
        if isinstance(plugin, IInputPlugin):
            input_plugins.append(plugin)

    if not input_plugins:
        return

    input_ctx = PluginContext(state=state, config={}, _services=engine.services)
    input_chain = PluginChain(input_plugins)
    await input_chain.execute(input_ctx)
    logger.debug("Input chain completed")


async def execute_core_plugin(
    engine: PipelineEngine,
    state: dict[str, Any],
    core_type: str,
) -> None:
    """执行 Core 插件（含指数退避重试和错误追踪）。

    成功时更新 state，失败时写入错误信息并追踪连续错误。
    连续错误超过阈值时强制结束管道。

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典（原地修改）
        core_type: 核心类型标识（如 llm_call, tool_execute）
    """
    core_plugin = engine.plugin_registry.get_core(core_type)
    if core_plugin is None:
        logger.warning("No core plugin registered for type: %s", core_type)
        return

    core_ctx = PluginContext(state=state, config={}, _services=engine.services)
    # Core plugin retry with exponential backoff
    max_core_retries = getattr(core_plugin, "max_retries", 3)
    core_retry_delay = getattr(core_plugin, "retry_delay", 1.0)
    core_error_policy = getattr(core_plugin, "error_policy", None)
    core_attempts = 0
    while True:
        core_attempts += 1
        try:
            core_result = await core_plugin.execute(core_ctx)
            if isinstance(core_result, dict):
                state.update(core_result)
            state.pop("raw_error", None)
            state.pop("llm_error_info", None)
            logger.debug("Core plugin executed: core_type=%s", core_type)
            engine.consecutive_core_errors = 0
            break  # success, exit retry loop
        except Exception as exc:
            _handle_core_error(
                engine,
                state,
                core_type,
                exc,
                core_error_policy,
                core_attempts,
                max_core_retries,
                core_retry_delay,
                core_plugin,
            )
            if _is_retryable(core_error_policy, core_attempts, max_core_retries, exc):
                import random as _rand  # noqa: PLC0415

                exc_lower = str(exc).lower()
                is_overload = "overloaded" in exc_lower or "529" in exc_lower
                if is_overload:
                    delay = getattr(core_plugin, "overload_retry_delay", 180.0)
                else:
                    delay = core_retry_delay * (2 ** (core_attempts - 1)) * (0.5 + _rand.random() * 0.5)
                logger.warning(
                    "[%s] Core retry %d/%d (delay=%.1fs%s): %s",
                    core_type,
                    core_attempts,
                    max_core_retries,
                    delay,
                    " [OVERLOAD]" if is_overload else "",
                    exc,
                )
                await asyncio.sleep(delay)
                continue
            # Non-retryable or exhausted retries → error already handled
            break  # exit retry loop after error handling


def _is_retryable(
    error_policy: Any,
    attempts: int,
    max_retries: int,
    exc: Exception,
) -> bool:
    """判断核心插件错误是否可重试。"""
    from pipeline.types import ErrorPolicy as _EP  # noqa: N814,PLC0415

    return error_policy == _EP.RETRY and attempts < max_retries + 1


def _handle_core_error(
    engine: PipelineEngine,
    state: dict[str, Any],
    core_type: str,
    exc: Exception,
    error_policy: Any,
    attempts: int,
    max_retries: int,
    retry_delay: float,
    core_plugin: Any,
) -> None:
    """处理核心插件执行错误：记录日志、追踪连续错误、构建 llm_error_info。"""
    logger.error("Core plugin error: %s", exc)
    state[StateKeys.RAW_ERROR] = str(exc)
    state[StateKeys.RAW_RESULT] = None

    # 判断是否为可恢复错误（不计入连续错误）
    # 基于 error_classifier 的 ErrorKind 派生，替代旧的字符串嗅探：
    # transient = 临时性错误（限流/服务不可用/网络），不应计入连续错误强制结束
    # fixable = LLM 可自行修复的错误（参数错误），也不计入
    from llm.error_classifier import classify_error  # noqa: PLC0415

    _info = classify_error(exc)
    _transient_kinds = ("rate_limit", "service_down", "network", "server_error")
    is_transient = _info.kind.value in _transient_kinds
    is_fixable = _info.kind.value == "bad_request"

    should_count = core_type == "llm_call" and not is_transient and not is_fixable

    if should_count:
        engine.consecutive_core_errors += 1
        if engine.consecutive_core_errors >= engine.max_consecutive_core_errors:
            logger.error(
                "Pipeline force-ending: %d consecutive core errors",
                engine.consecutive_core_errors,
            )
            state[StateKeys.ENDED] = True
    else:
        logger.info(
            "[%s] error not counting as consecutive (transient=%s, fixable=%s): %s",
            core_type,
            is_transient,
            is_fixable,
            exc,
        )

    # 构建 llm_error_info（仅 llm_call 类型）
    if core_type == "llm_call":
        _build_llm_error_info(state, exc, core_type)


def _build_llm_error_info(
    state: dict[str, Any],
    exc: Exception,
    core_type: str,
) -> None:
    """构建 llm_error_info 字典并存入 state，并追加到错误历史。

    用统一的 error_classifier.classify_error 做分类（替代旧的字符串嗅探），
    ErrorKind 决定 transient/fixable 语义，供 engine_chain 计数和
    llm_error_recovery 插件按类型分支处理。

    同时把每轮错误追加到 state[LLM_ERROR_HISTORY]，作为单一数据源，
    task_post_pipeline / track / watchdog 等任意消费方都从这里取统计。
    """
    from llm.error_classifier import classify_error  # noqa: PLC0415

    info = classify_error(exc)
    error_msg = str(exc)
    error_lower = error_msg.lower()

    # context_overflow 是业务约束（上下文超长），不在 ErrorKind 里，
    # 就地判定后覆盖 error_type（优先级高于 ErrorKind）。
    is_context_overflow = (
        "context window exceeds" in error_lower
        or "context_length_exceeded" in error_lower
        or "context length" in error_lower
        or ("max_tokens" in error_lower and "exceed" in error_lower)
        or ("token" in error_lower and "limit" in error_lower)
    )

    if is_context_overflow:
        error_type = "context_overflow"
    else:
        error_type = info.kind.value

    state["llm_error_info"] = {
        "error_msg": error_msg,
        "error_type": error_type,
        "core_type": core_type,
    }

    # 追加到错误历史（单一数据源，任意消费方可读）
    from datetime import datetime  # noqa: PLC0415

    history = state.setdefault(StateKeys.LLM_ERROR_HISTORY, [])
    history.append(
        {
            "iteration": state.get(StateKeys.ITERATION, 0),
            "kind": error_type,
            "msg": error_msg[:200],
            "ts": datetime.now().isoformat(),
        }
    )


async def execute_output_chain(
    engine: PipelineEngine,
    state: dict[str, Any],
    core_type: str,
) -> list[RouteSignal]:
    """执行 Output 插件链并收集路由信号。

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典（原地修改）
        core_type: 当前核心类型标识

    Returns:
        收集到的路由信号列表
    """
    from pipeline.engine_route import resolve_output_plugins  # noqa: PLC0415

    output_plugins = resolve_output_plugins(engine, state, core_type)
    route_signals: list[RouteSignal] = []

    # Core 插件（ToolCore）可直接通过 state 注入路由信号。
    # 例：human_interaction 对话模式返回后，ToolCore 写入
    # _pending_route_signal = {"route_type": "wait", ...}
    # 此处取出与 Output 插件信号一起参与仲裁，walk 优先级高于 next_llm。
    pending_raw = state.pop("_pending_route_signal", None)
    if pending_raw and isinstance(pending_raw, dict):
        route_signals.append(RouteSignal(**pending_raw))
        logger.debug("Injected route signal from core: %s", pending_raw.get("route_type"))

    if not output_plugins:
        return route_signals

    plugin_names = [getattr(p, "name", type(p).__name__) for p in output_plugins]
    logger.debug("Output plugins for core_type=%s: %s", core_type, plugin_names)

    output_ctx = PluginContext(state=state, config={}, _services=engine.services)
    output_chain = PluginChain(output_plugins)
    output_results = await output_chain.execute(output_ctx)
    for result in output_results:
        if result.route_signal is not None:
            route_signals.append(result.route_signal)

    signal_summary = ", ".join(f"{s.route_type}({s.reason[:60]})" for s in route_signals) if route_signals else "none"
    logger.debug(
        "Output chain: %d plugins, %d signals [%s], ended=%s",
        len(output_results),
        len(route_signals),
        signal_summary,
        state.get(StateKeys.ENDED, False),
    )
    return route_signals


async def handle_no_route_signals(
    engine: PipelineEngine,
    state: dict[str, Any],
    core_type: str,
    iteration: int,
) -> str:
    """处理无路由信号时的后续逻辑。

    Returns:
        "continue" 继续循环；"end" 结束管道。
    """
    if core_type == "tool_execute" or state.get("thinking_retry_needed"):
        if state.get("thinking_retry_needed"):
            retry_count = state.get("thinking_retry_count", 0)
            logger.info("Thinking truncated, retrying LLM call (retry=%d)", retry_count)
        else:
            logger.debug("No route signals after tool execution, defaulting to next_llm")
        state.pop("thinking_retry_needed", None)
        state[StateKeys.CORE_TYPE] = "llm_call"
        return "continue"

    # 统一走 consume_pending_notifications（engine_iteration），
    # 不再内联重复 drain/过滤/拼接逻辑。
    from pipeline.engine_iteration import consume_pending_notifications  # noqa: PLC0415

    if await consume_pending_notifications(engine, state):
        return "continue"

    _has_active_triggers = _check_active_triggers(state, engine.pipeline_id)
    if _has_active_triggers:
        logger.info(
            "[Engine] 管道即将结束但存在活跃触发器，挂起等待触发器唤醒 (iter=%d)",
            iteration,
        )
        state[StateKeys.CORE_TYPE] = "llm_call"
        state["user_input"] = ""

    else:
        logger.info(
            "No route signals after LLM response (iter=%d), suspending pipeline to wait for next message.",
            iteration,
        )
        state["user_input"] = ""

    if state["user_input"]:
        state.setdefault("messages", []).append({"role": "user", "content": state["user_input"]})

    resumed = await engine.suspend_and_wait(state)
    if not resumed:
        logger.info(
            "[Engine] suspend_and_wait 返回 False，管道结束 (iter=%d)",
            iteration,
        )
        state[StateKeys.ENDED] = True
        return "end"
    return "continue"


def _check_active_triggers(state: dict[str, Any], engine_pipeline_id: str) -> bool:
    """检查是否有活跃的触发器绑定到当前管道。"""
    try:
        from triggers.manager import get_trigger_manager  # noqa: PLC0415

        _tm = get_trigger_manager()
        _pipeline_id = state.get(StateKeys.PIPELINE_ID, engine_pipeline_id)
        return any(t.pipeline_id == _pipeline_id and t.status.value == "active" for t in _tm._triggers.values())
    except Exception:
        return False


async def run_post_end_output_chain(
    engine: PipelineEngine,
    state: dict[str, Any],
) -> None:
    """管道结束后，再执行一次 Output 插件链以保存 PipelineRunSummary 等终态数据。

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典
    """
    from pipeline.engine_route import resolve_output_plugins  # noqa: PLC0415

    core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
    output_plugins = resolve_output_plugins(engine, state, core_type)
    if not output_plugins:
        return
    try:
        output_ctx = PluginContext(state=state, config={}, _services=engine.services)
        output_chain = PluginChain(output_plugins)
        await output_chain.execute(output_ctx)
    except Exception as exc:
        logger.debug("Post-end output chain failed (non-critical): %s", exc)
