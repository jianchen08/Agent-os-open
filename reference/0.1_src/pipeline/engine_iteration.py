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


def _get_bridge_for_pipeline(pipeline_id: str) -> Any | None:
    """通过公开注册表 API 获取管道的 bridge（供通知推送用）。

    consume_pending_notifications 不直接访问 engine 私有成员，
    统一走 registry.get_bridge 公开接口。
    """
    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    try:
        return get_engine_registry().get_bridge(pipeline_id)
    except Exception:
        return None


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

    # 1. 消费待处理通知。每轮迭代开头检查队列，队列有什么就消费什么（按时序）。
    # tool_execute 轮 consume 内部跳过（保护 tool_call 配对）。
    # consume 内部统一做 emit_finish 流式分割（推送通知前关当前流），保证通知排在
    # 旧 AI 气泡之后、新 AI 气泡之前。
    await consume_pending_notifications(engine, state, prepend=True)

    # 1.5 流式续接：若上一轮 apply_route 的 next_llm 分支已 emit_finish 关流（注入分割），
    # 本轮 LLM 输出前 emit_start 开新流。
    # 只在 llm_call 轮且流未开时触发：
    # - 工具结果触发（tool_execute→llm_call）：流还开着（工具链共用一个流），不触发（续流）
    # - 注入触发（apply_route emit_finish 后）：流关了，触发 emit_start 开新流
    if state.get(StateKeys.CORE_TYPE, "llm_call") == "llm_call":
        _bridge_for_start = _get_bridge_for_pipeline(engine.pipeline_id)
        if _bridge_for_start is not None and not getattr(_bridge_for_start, "_stream_started", False):
            try:
                await _bridge_for_start.emit_start(state)
            except Exception as exc:
                logger.warning("[Engine] emit_start 续接失败（非致命）: %s", exc)

    # 2. 解析插件列表 + 执行 Input 链
    plugin_names = engine.input_route_table.resolve_plugins(state)
    logger.debug("Input route resolved plugins: %s", plugin_names)
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
    logger.debug(
        "Input route resolved target: %s (entry=%s)",
        target,
        matched_entry.name if matched_entry else "none",
    )

    if target == "end":
        return await _handle_target_end(engine, state, matched_entry)

    if target == "wait":
        return await _handle_target_wait(engine, state)

    return IterationAction.CONTINUE


async def consume_pending_notifications(
    engine: PipelineEngine,
    state: dict[str, Any],
    *,
    prepend: bool = False,
) -> bool:
    """统一的待处理通知注入入口（state 注入 + 前端推送 + 流式分割的唯一函数）。

    将 drain_inject_queue 取出的 (message, source) 过滤空白后注入 state，
    按 source 分流：

    - source=user（用户注入）：写 user_input + messages。
      track 插件据此落 type="user" 记录（历史接口正确返回用户气泡）。
    - source!=user（系统通知，如触发器/子任务完成）：写 user_input + messages
      并标 _last_inject_sources，track 据此落 type="system" 记录（历史接口返回
      system 气泡）；同时在此推送 system_notification —— 这是系统通知的【唯一推送点】。

    ★ id 契约（与 AI 消息对称，避免刷新后 system 气泡重复渲染）：
    emit_notification 生成 record_id（唯一 id 来源），逐条事件 payload 带上它，
    并把本轮 record_id 写入 state["_pending_system_record_id"]。track 落 system
    记录时复用它作 record_id，保证：事件 record_id == 落库 record_id == 前端消息 id。

    ★ 流式分割（fix_20260705_notification_after_reply）：
    推送 system 通知前，如果当前 AI 流还开着（_stream_started=True），先 emit_finish
    关闭它（落库为独立气泡）。这样通知排在旧 AI 气泡和新 AI 气泡之间。
    下一轮 run_iteration 1.5 会 emit_start 开新流。

    所有 consume 调用点（apply_route next_llm、_handle_target_end、handle_no_route_signals）
    都走这个统一函数，注入分割逻辑只此一处实现，不会遗漏。

    core_type 强制 llm_call（在调用方设置）；tool_execute 时由调用方跳过（不调本函数）。

    推送时序：消息出队列、进入下一轮迭代的边界点推送，与 LLM 流式输出
    共用 bridge 通道。除此之外不应存在其它 system 通知推送出口。

    Args:
        engine: PipelineEngine 实例
        state: 管道状态字典（原地修改）
        prepend: True=前置追加到现有 user_input（迭代开头调用用）；
                 False=覆盖现有 user_input（target=end/无路由兜底用）

    Returns:
        True 表示有待处理通知已注入（调用方应继续循环）；
        False 表示无待处理通知或被 tool_execute 跳过（调用方可真正结束/挂起）。
    """
    if state.get(StateKeys.CORE_TYPE) == "tool_execute":
        return False

    _queued = engine.drain_inject_queue()
    if not _queued:
        return False

    # 按 source 分流：user 注入 vs 系统通知
    _user_msgs: list[str] = []
    _system_notifs: list[tuple[str, str]] = []
    for _msg, _source in _queued:
        if not _msg or not _msg.strip():
            continue
        if _source == "user":
            _user_msgs.append(_msg)
        else:
            _system_notifs.append((_msg, _source))

    if not _user_msgs and not _system_notifs:
        return False

    # ── user 注入：写 user_input + messages ──
    if _user_msgs:
        _combined_user = "\n\n".join(_user_msgs)
        _existing_input = state.get("user_input", "")
        if prepend and _existing_input:
            state["user_input"] = f"{_combined_user}\n\n{_existing_input}"
        else:
            state["user_input"] = _combined_user
        state.setdefault("messages", []).append({"role": "user", "content": _combined_user})

    # ── 系统通知：只写 messages（喂 LLM），不写 user_input（track 不落 type=system） ──
    if _system_notifs:
        _combined_sys = "\n\n".join(_m for _m, _ in _system_notifs)
        state.setdefault("messages", []).append({"role": "user", "content": _combined_sys})

        # ★ system 通知走 state 通道持久化（与其他类型统一）：
        # consume 把通知内容 + source 写入 state，track 在 output 链统一落库。
        # 不用临时字段（如 _pending_system_notifs），而是复用 user_input 的增量检测机制：
        # system 通知也写 user_input（prepend），track 的 _extract_injected_content 会提取增量。
        # 落库时 track 通过 source 标记区分 type=system vs type=user。
        if prepend:
            _existing = state.get("user_input", "")
            state["user_input"] = f"{_combined_sys}\n\n{_existing}" if _existing else _combined_sys
        else:
            state["user_input"] = _combined_sys
        # 标记本轮 user_input 增量里含 system 通知（供 track 区分 type）
        state["_last_inject_sources"] = [s for _, s in _system_notifs]

        # ★ 唯一推送点：消息出队列进下一轮时推送 ★
        # 系统通知的前端气泡只此一处产生，不再有 message_bus 注入入口的推送、
        # 也不进 user_input 让 track 落库二次渲染。
        _bridge = _get_bridge_for_pipeline(engine.pipeline_id)
        if _bridge is not None:
            # ★ 流式分割：推送 system 通知前，如果当前 AI 流还开着，先 emit_finish 关闭它。
            # 这样通知排在旧 AI 气泡之后、新 AI 气泡之前（下一轮 run_iteration 1.5 emit_start 开新流）。
            # emit_finish 有幂等保护（_stream_started=False 时跳过）。
            # 这是统一的分割点 —— 所有 consume 调用点（apply_route、_handle_target_end、
            # handle_no_route_signals）都自动获得分割能力，不会遗漏。
            if getattr(_bridge, "_stream_started", False):
                try:
                    await _bridge.emit_finish(state)
                    logger.info("[Engine] consume 流式分割：emit_finish 关闭当前 AI 流（推送通知前）")
                except Exception as exc:
                    logger.warning("[Engine] consume 流式分割 emit_finish 失败（非致命）: %s", exc)

            for _content, _source in _system_notifs:
                try:
                    _notif_rid = await _bridge.emit_notification(_content, source=_source, level="info")
                except Exception as exc:
                    logger.warning("[Engine] 通知推送失败: %s", exc)
                    _notif_rid = ""
                # 记录本轮 system 通知的 record_id（emit_notification 是唯一 id 来源），
                # 供 track 插件落库时复用 —— 保证事件 record_id == 落库 record_id。
                # 多条 system 通知在 track 里合并落一条记录（_combined_sys），故取最后一个
                # record_id 覆盖，使合并记录与至少一条事件气泡 id 对齐。
                if _notif_rid:
                    state["_pending_system_record_id"] = _notif_rid

    state[StateKeys.CORE_TYPE] = "llm_call"
    state.pop("raw_result", None)
    state.pop("error_analysis", None)

    # 同步前端乐观消息 ID 到 state，供 track 插件持久化时写入 user_record
    _pending_cmids = getattr(engine, "_pending_client_message_id", "")
    if _pending_cmids:
        state["client_message_id"] = _pending_cmids
        engine._pending_client_message_id = ""

    logger.info(
        "[Engine] 消费通知：user=%d system=%d，注入 state 继续循环 (prepend=%s)",
        len(_user_msgs),
        len(_system_notifs),
        prepend,
    )
    return True


async def _handle_target_end(
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
    if await consume_pending_notifications(engine, state):
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

    logger.debug("Pipeline woken up, resuming loop iteration")
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
            engine,
            state,
            core_type,
            iteration,
        )
        return IterationAction.BREAK if _no_route_action == "end" else IterationAction.CONTINUE

    resolved = engine.output_route_table.arbitrate(route_signals, state)
    logger.debug(
        "Route arbitrated: type=%s, target=%s, reason=%s",
        resolved.route_type,
        resolved.target,
        resolved.reason,
    )
    should_break = await apply_route(engine, resolved, state)
    return IterationAction.BREAK if should_break else IterationAction.CONTINUE
