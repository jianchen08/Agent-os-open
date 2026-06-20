"""管道引擎 — 单轮迭代调度。

从 _run_loop 的 while 主体中提取的"一轮迭代调度流程"。
engine 只保留状态管理骨架（迭代计数/检查点/idle timer/异常处理），
本模块负责每轮迭代的调度：通知消费 → Input 链 → target 分发 →
Core 执行 → Output 链 → 路由仲裁。

公共接口（通过 PipelineEngine 调用）：
- run_iteration: 执行一轮管道迭代，返回是否应中断循环
- IterationAction: 迭代结果枚举

设计原则（来自消息系统改造方案 MSG-REF-001 C-01）：
- 引擎职责 = 状态管理 + 异常处理
- 迭代调度职责 = 本模块（独立于 engine 的状态管理）
- 工具执行职责 = tool_core 插件（已是 Core 插件，不在 engine 也不在此处）
- 路由决策职责 = engine_route.apply_route（已是独立模块）
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Any

from pipeline.engine_chain import (
    execute_core_plugin,
    execute_input_chain,
    execute_output_chain,
    handle_no_route_signals,
)
from pipeline.engine_route import apply_route
from pipeline.types import StateKeys

if TYPE_CHECKING:
    from pipeline.engine import PipelineEngine

logger = logging.getLogger(__name__)


class IterationAction(Enum):
    """一轮迭代结束后，engine while 循环应执行的动作。"""

    CONTINUE = "continue"
    BREAK = "break"


async def run_iteration(
    engine: PipelineEngine,
    state: dict[str, Any],
    iteration: int,
) -> IterationAction:
    """执行一轮管道迭代。

    每轮迭代的完整调度流程：
    1. 消费待处理通知（注入到 state）
    2. 解析 Input 插件列表 + 执行 Input 链
    3. 解析 target（core/end/wait）并分发
    4. 执行 Core 插件
    5. 执行 Output 链 + 路由仲裁

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典（原地修改）
        iteration: 当前迭代序号（用于日志）

    Returns:
        IterationAction.CONTINUE 继续循环；IterationAction.BREAK 中断循环。
    """

    # 1. 消费待处理通知（tool_execute 迭代中跳过，避免破坏工具调用配对）
    _consume_notifications(engine, state, iteration)

    # 2. 解析插件列表 + 执行 Input 链
    plugin_names = engine.input_route_table.resolve_plugins(state)
    logger.info("Input route resolved plugins: %s", plugin_names)
    await execute_input_chain(engine, state, plugin_names)

    # Input 插件可能设 ENDED 提前终止
    if state.get(StateKeys.ENDED, False):
        logger.info("Pipeline ended by input plugin (ENDED=True)")
        return IterationAction.BREAK

    # 3. 解析 target 并分发
    target_action = await _dispatch_input_target(engine, state, iteration)
    if target_action == IterationAction.BREAK:
        return IterationAction.BREAK

    # 4. 执行 Core 插件
    core_type = state.get(StateKeys.CORE_TYPE, "llm_call")
    await execute_core_plugin(engine, state, core_type)

    # 5. 执行 Output 链 + 路由仲裁
    return await _execute_core_and_route(engine, state, core_type, iteration)


def _consume_notifications(
    engine: PipelineEngine,
    state: dict[str, Any],
    iteration: int,
) -> None:
    """消费待处理通知并注入 state。

    tool_execute 迭代中跳过消费，避免在 assistant(tool_calls) 与 tool(result)
    之间插入 user 消息，破坏配对（会触发 Minimax API 2013 错误）。

    非空通知注入后，core_type 强制为 llm_call。
    """
    _core_type = state.get(StateKeys.CORE_TYPE, state.get("core_type"))
    if _core_type == "tool_execute":
        return

    _iter_notifs = engine.drain_inject_queue()
    if not _iter_notifs:
        return

    # 过滤空白通知，避免空消息进入对话历史
    _filtered = [n for n in _iter_notifs if n and n.strip()]
    if not _filtered:
        return

    _combined = "\n\n".join(_filtered)
    _existing_input = state.get("user_input", "")
    if _existing_input:
        state["user_input"] = f"{_combined}\n\n{_existing_input}"
    else:
        state["user_input"] = _combined
    state.setdefault("messages", []).append(
        {"role": "user", "content": _combined}
    )
    state[StateKeys.CORE_TYPE] = "llm_call"
    state.pop("raw_result", None)
    state.pop("error_analysis", None)
    # 同步前端乐观消息 ID 到 state，供 track 插件持久化时写入 user_record
    _pending_cmids = getattr(engine, "_pending_client_message_id", "")
    if _pending_cmids:
        state["client_message_id"] = _pending_cmids
        engine._pending_client_message_id = ""
    logger.info(
        "[Engine] 迭代 %d 开始时消费 %d 条待处理通知，注入 state: %s",
        iteration, len(_iter_notifs),
        _combined[:80] if _combined else "(empty)",
    )


async def _dispatch_input_target(
    engine: PipelineEngine,
    state: dict[str, Any],
    iteration: int,
) -> IterationAction:
    """解析 target（core/end/wait）并执行对应分发。

    - target=end：若有待处理通知则取消结束继续循环；否则写 RAW_RESULT 并结束
    - target=wait：保存挂起快照，挂起等待唤醒；唤醒后据 raw_tool_calls 决定 core_type
    - target=core：继续执行

    Returns:
        IterationAction.CONTINUE 继续迭代；IterationAction.BREAK 结束循环。
    """
    target, matched_entry = engine.input_route_table.resolve_target(state)
    logger.info(
        "Input route resolved target: %s (entry=%s)",
        target, matched_entry.name if matched_entry else "none",
    )

    if target == "end":
        return _handle_target_end(engine, state, matched_entry)

    if target == "wait":
        return await _handle_target_wait(engine, state)

    return IterationAction.CONTINUE


def consume_pending_notifications(engine: PipelineEngine, state: dict[str, Any]) -> bool:
    """消费待处理通知：非空则注入 state 继续循环，空则返回 False。

    统一的"通知注入"入口——把 drain_inject_queue 取出的待处理消息过滤空白后
    注入 messages/user_input/core_type。这是引擎调度层唯一的消息注入点，
    消除原先散落在 _handle_target_end / apply_route 的三处重复逻辑。

    引擎调度层原则：可改状态字段（CORE_TYPE 等），但通知注入集中在此函数，
    不再在各路由分支内联重复。

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典（原地修改）

    Returns:
        True 表示有待处理通知已注入（调用方应继续循环）；
        False 表示无待处理通知（调用方可真正结束/挂起）。
    """
    _notifs = engine.drain_inject_queue()
    if not _notifs:
        return False

    _filtered = [n for n in _notifs if n and n.strip()]
    if not _filtered:
        return False

    _combined = "\n\n".join(_filtered)
    state["user_input"] = _combined
    state.setdefault("messages", []).append(
        {"role": "user", "content": _combined}
    )
    state[StateKeys.CORE_TYPE] = "llm_call"
    logger.info(
        "[Engine] 消费 %d 条待处理通知，注入 state 继续循环",
        len(_filtered),
    )
    return True


def _handle_target_end(
    engine: PipelineEngine,
    state: dict[str, Any],
    matched_entry: Any,
) -> IterationAction:
    """处理 target=end：待处理通知优先，否则真正结束。

    引擎调度层职责：根据路由决策（end）决定循环去留。
    - 有待处理通知 → consume_pending_notifications 注入后继续循环
    - 无通知 → 设 ENDED 结束循环

    不生成内容（不写 RAW_RESULT）、不内联注入消息——通知注入统一走
    consume_pending_notifications。
    """
    if consume_pending_notifications(engine, state):
        return IterationAction.CONTINUE

    state[StateKeys.ENDED] = True
    logger.info("Pipeline ended by input route (target=end)")
    return IterationAction.BREAK


async def _handle_target_wait(
    engine: PipelineEngine,
    state: dict[str, Any],
) -> IterationAction:
    """处理 target=wait：保存快照、检查点、挂起、唤醒后设置 core_type。

    挂起前保存 _suspended_state 与 _wake_event（避免 inject_message 窗口丢消息）。
    唤醒后若存在 raw_tool_calls 则走 tool_execute，否则 llm_call。
    """
    import asyncio  # noqa: PLC0415

    engine._suspended_state = engine._suspend_copy_state(state)
    # 在设置 _suspended_state 的同时创建 _wake_event，避免 inject_message
    # 在 _suspended_state 已设置但 _wake_event 还是 None 的窗口内 set() 丢失。
    engine._wake_event = asyncio.Event()
    logger.info("Pipeline suspended by input route (target=wait), state saved")

    if engine._checkpoint_manager is not None:
        try:
            _s_pid = state.get(StateKeys.PIPELINE_ID, "default")
            await engine._checkpoint_manager.save(_s_pid, state, phase="suspended")
        except Exception as exc:
            logger.debug("Checkpoint suspended-save failed: %s", exc)

    # 恢复逻辑已内置到 _suspend_and_wait，无需手动恢复
    if not await engine._suspend_and_wait(state):
        state[StateKeys.ENDED] = True
        logger.info("Pipeline ended: suspend_and_wait returned False (no new content)")
        return IterationAction.BREAK

    logger.info("Pipeline woken up, resuming loop iteration")
    # 唤醒时若有待执行的工具调用，必须先执行完工具再处理注入的消息，
    # 否则通知会插入 assistant(tool_calls) 与 tool(result) 之间破坏配对。
    if state.get(StateKeys.RAW_TOOL_CALLS):
        state[StateKeys.CORE_TYPE] = "tool_execute"
    else:
        state[StateKeys.CORE_TYPE] = "llm_call"
    return IterationAction.CONTINUE


async def _execute_core_and_route(
    engine: PipelineEngine,
    state: dict[str, Any],
    core_type: str,
    iteration: int,
) -> IterationAction:
    """执行 Output 插件链并应用路由仲裁结果。

    有路由信号 → 仲裁 → apply_route → 据 should_break 决定 BREAK/CONTINUE。
    无路由信号 → handle_no_route_signals → 据 end/continue 决定。
    """
    route_signals = await execute_output_chain(engine, state, core_type)

    if not route_signals:
        _no_route_action = await handle_no_route_signals(
            engine, state, core_type, iteration,
        )
        return IterationAction.BREAK if _no_route_action == "end" else IterationAction.CONTINUE

    resolved = engine.output_route_table.arbitrate(route_signals, state)
    logger.info(
        "Route arbitrated: type=%s, target=%s, reason=%s",
        resolved.route_type, resolved.target, resolved.reason,
    )
    should_break = await apply_route(engine, resolved, state)
    return IterationAction.BREAK if should_break else IterationAction.CONTINUE
