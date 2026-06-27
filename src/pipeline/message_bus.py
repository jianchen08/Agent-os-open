"""管道消息总线 — 统一消息注入入口。

所有"给管道发消息"的操作都通过 send_pipeline_message() 进行。
路口不区分来源（WS/CLI/任务系统/触发器），只做 pipeline_id → 注册表找引擎 → 转发。
agent 由数据源决定，不在此解析。

公共接口:
    send_pipeline_message: 统一入口，接受 PipelineMessage 对象
    InjectResult: 注入结果数据类
    stop: 持有者级别的管道终结（原子级联）

设计原则（I1-I6）：
- 注册表是引擎生命唯一权威；send 遇未注册直接拒绝（不建引擎）
- CONTROL 信号走 state（pending_signals），由插件自治处理
- 引擎死亡是注册表内部自治（lazy 重建）；启动恢复由持有者（TaskWorker）负责

实现拆分：
- drain_manager.py: Drain 生命周期管理（Sink 创建、drain 启停）
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agents.types import AgentConfig
    from pipeline.sink import IOutputSink

from pipeline.message_types import (
    MessageType,
    PipelineMessage,
    PipelineRequest,
)

logger = logging.getLogger(__name__)


@dataclass
class InjectResult:
    """消息注入结果。"""
    success: bool
    method: str = ""
    pipeline_id: str = ""
    error: str = ""
    bridge: Any = None


def _find_engine(pipeline_id: str) -> tuple[Any | None, str]:
    """查找目标管道引擎实例。返回 (engine, state) 元组。

    I3 改造：僵尸引擎检测已删除。引擎死亡是注册表内部自治事务，
    由 entry.ensure_engine 在内部 lazy 重建保证可用性。
    本函数只做"查注册表 + 报状态"，不穿透引擎私有成员。
    """
    from pipeline.registry import get_engine_registry  # noqa: PLC0415
    entry = get_engine_registry().get(pipeline_id)
    if entry is None:
        return None, ""
    engine = entry.engine

    if engine.is_suspended:
        return engine, "suspended"
    if engine.is_running:
        return engine, "running"
    if engine.is_idle:
        return engine, "idle"
    return None, ""


async def _auto_complete_interaction(pipeline_id: str) -> None:
    """自动完成管道的 pending conversation 模式交互请求。"""
    try:
        from human_interaction import get_human_interaction_service  # noqa: PLC0415
        service = get_human_interaction_service()
        if service is None:
            return
        count = await service.auto_complete_conversation_for_pipeline(pipeline_id)
        if count > 0:
            logger.info("[MessageBus] 自动完成 %d 个 conversation 交互 | pipeline=%s", count, pipeline_id[:12])
    except Exception as exc:
        logger.debug("[MessageBus] 自动完成交互检查失败（可忽略）: %s", exc)


async def send_pipeline_message(
    message: PipelineMessage,
    *,
    agent_config: AgentConfig | None = None,
    output_sink: IOutputSink | None = None,
    conversation_history: list[dict] | None = None,
    workspace: str = "",
    task_id: str = "",
) -> InjectResult:
    """统一消息注入入口 — 接受 PipelineMessage 对象。

    所有来源（WS/CLI/任务系统/触发器）统一走此入口。路口不区分来源，
    只做 pipeline_id → 注册表找引擎 → 转发消息。agent 由数据源决定，
    不在此解析。

    Args:
        message: 标准内部消息对象（必须经过 parse_frontend_message 构造，
                 或直接构造 PipelineMessage）
        agent_config: Agent 配置（可选，仅创建者首次绑定身份时传；
                      WS 入口不传，引擎 idle 用自带身份，revive 从持久化数据重建）
        output_sink: IOutputSink 实例（可选，自动创建）
        conversation_history: 对话历史（可选，revive 场景使用）
        workspace: 工作目录（可选，任务管道用）
        task_id: 关联任务 ID（可选，任务管道用）

    Returns:
        InjectResult 注入结果
    """
    request = PipelineRequest(
        message=message,
        agent_config=agent_config,
        output_sink=output_sink,
        conversation_history=conversation_history,
        streaming=True,
        workspace=workspace,
        task_id=task_id,
    )
    return await _inject_request(request)


async def _inject_request(request: PipelineRequest) -> InjectResult:
    """核心注入逻辑 — 接受 PipelineRequest 对象。

    从 PipelineRequest 中提取字段，复用已有的引擎注入和 revive 逻辑。
    """
    msg = request.message
    pipeline_id = msg.pipeline_id
    content = msg.content
    thread_id = msg.thread_id
    metadata = msg.metadata
    client_message_id = msg.client_message_id
    attachments = msg.attachments

    if not pipeline_id:
        return InjectResult(success=False, error="pipeline_id 不能为空", method="failed")

    # 仅拦截非空但纯空白的消息
    if content is not None and len(content) > 0 and not content.strip():
        return InjectResult(success=False, error="message 不能仅包含空白字符", method="failed")

    # I6：CONTROL 信号分流。停止生成等控制信号走信号投递，不进 inject 队列。
    # 信号内容承载在 metadata（开放式 tags，插件自定义 signal_type 等）。
    if msg.type == MessageType.CONTROL:
        return await _deliver_control_signal(pipeline_id, msg)

    engine, state = _find_engine(pipeline_id)

    # BUG-FIX-fix_20260531_sink_dead_thread_id_lost: 主动更新 registry 中缺失的 thread_id
    if thread_id and pipeline_id:
        try:
            from pipeline.registry import get_engine_registry as _reg_get  # noqa: PLC0415
            _reg_entry = _reg_get().get(pipeline_id)
            if _reg_entry and not _reg_entry.thread_id:
                _reg_entry.thread_id = thread_id
        except Exception as exc:
            logger.warning("[MessageBus] thread_id 更新失败: pipeline=%s err=%s", pipeline_id[:12], exc)

    if engine is not None:
        return await _inject_to_engine(
            pipeline_id, engine, state, content, metadata,
            request.agent_config, request.workspace, request.task_id,
            request.conversation_history, request.output_sink, thread_id,
            client_message_id=client_message_id,
            attachments=attachments,
        )

    # I4：未注册直接拒绝，不建引擎。这是持有者的责任——持有者必须保证
    # 发消息前 entry 已在注册表（首次 register，重启后重新 register）。
    # 原来的 revive 自动重建会掩盖"持有者未正确恢复注册表"的 bug，已删除。
    logger.warning(
        "[MessageBus] 管道未注册，拒绝消息（持有者未 register）: pipeline=%s",
        pipeline_id[:12],
    )
    return InjectResult(
        success=False,
        error=f"管道 {pipeline_id[:12]} 未注册，无法发送消息（请联系持有者先 register）",
        method="rejected",
        pipeline_id=pipeline_id,
    )


async def _deliver_control_signal(pipeline_id: str, msg: PipelineMessage) -> InjectResult:
    """I6：投递控制信号到引擎（不进 inject 队列，不删 entry）。

    信号内容承载在 msg.metadata（开放式 tags，如 signal_type=stop_generation）。
    引擎通过 deliver_signal 将信号写入 state 并投递给当前插件。
    未注册的管道拒绝信号（I4：send 不建引擎）。
    """
    from pipeline.registry import get_engine_registry  # noqa: PLC0415
    entry = get_engine_registry().get(pipeline_id)
    if entry is None:
        return InjectResult(
            success=False, error="管道未注册，无法投递信号", method="rejected",
            pipeline_id=pipeline_id,
        )
    engine = entry.engine
    # 引擎暴露 deliver_signal 才支持信号机制；否则降级为日志
    if hasattr(engine, "deliver_signal"):
        try:
            engine.deliver_signal(msg.metadata or {})
        except Exception as exc:
            logger.warning("[MessageBus] 信号投递失败: pipeline=%s err=%s", pipeline_id[:12], exc)
            return InjectResult(success=False, error=str(exc), method="failed", pipeline_id=pipeline_id)
        logger.info(
            "[MessageBus] 信号已投递: pipeline=%s signal_type=%s",
            pipeline_id[:12], (msg.metadata or {}).get("signal_type", "?"),
        )
        return InjectResult(success=True, method="signal", pipeline_id=pipeline_id)
    logger.debug("[MessageBus] 引擎不支持信号投递（无 deliver_signal）: pipeline=%s", pipeline_id[:12])
    return InjectResult(success=False, error="引擎不支持信号投递", method="rejected", pipeline_id=pipeline_id)


async def _inject_to_engine(
    pipeline_id: str, engine: Any, state: str, message: str,
    metadata: dict | None, agent_config: AgentConfig | None, workspace: str,
    task_id: str, conversation_history: list[dict] | None,
    output_sink: IOutputSink | None, thread_id: str,
    client_message_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> InjectResult:
    """向已存在的引擎注入消息。"""
    from pipeline.drain_manager import create_sink  # noqa: PLC0415
    try:

        msg_source = (metadata or {}).get("source", "user")
        logger.info("[MessageBus] 消息注入: pipeline=%s state=%s source=%s msg=%.60s",
                     pipeline_id[:12], state, msg_source, message or "(empty)")

        # 非 user 消息：通过 bridge 推送 system_notification（和 AI stream 走同一通道，保证时序）。
        # emit_notification 是 async，在 inject_message 之前调度，保证 notification 在 stream chunk 之前。
        if msg_source != "user":
            from pipeline.registry import get_engine_registry as _reg_for_push  # noqa: PLC0415
            _notif_bridge = _reg_for_push().get_bridge(pipeline_id)
            if _notif_bridge is not None:
                try:
                    await _notif_bridge.emit_notification(message, source=msg_source, level="info")
                except Exception as exc:
                    logger.warning("[MessageBus] bridge 通知推送失败: %s", exc)
            else:
                # bridge 不存在（idle/首次）→ sink 直推
                _push_sink = output_sink or create_sink(pipeline_id, thread_id=thread_id)
                if _push_sink is not None:
                    try:
                        await _push_sink.send_event({
                            "type": "system_notification",
                            "data": {
                                "pipeline_id": pipeline_id,
                                "content": message,
                                "source": msg_source,
                                "level": "info",
                                "notificationType": f"{msg_source}_notification",
                            },
                        })
                    except Exception as exc:
                        logger.warning("[MessageBus] sink 推送通知失败: %s", exc)

        if state == "idle":
            return await _start_idle_engine(
                pipeline_id, engine, message, agent_config=agent_config,
                workspace=workspace, task_id=task_id,
                conversation_history=conversation_history,
                output_sink=output_sink, thread_id=thread_id,
                client_message_id=client_message_id,
                attachments=attachments,
            )

        engine.inject_message(message, source=msg_source, client_message_id=client_message_id)
        logger.info("[MessageBus] 已注入引擎: pipeline=%s source=%s method=%s queue=%d",
                     pipeline_id[:12], msg_source,
                     "wake" if state == "suspended" else "notification",
                     engine.inject_queue_size)
        method = "wake" if state == "suspended" else "notification"

        if state == "running" and msg_source == "user":
            await _auto_complete_interaction(pipeline_id)

        logger.info("[MessageBus] 消息已注入 | pipeline=%s method=%s", pipeline_id[:12], method)
        return InjectResult(success=True, method=method, pipeline_id=pipeline_id, bridge=None)
    except Exception as exc:
        logger.warning("[MessageBus] 消息注入失败: %s", exc)
        return InjectResult(success=False, error=str(exc), method="failed", pipeline_id=pipeline_id)


async def _start_idle_engine(
    pipeline_id: str, engine: Any, message: str, *,
    agent_config: AgentConfig | None = None, workspace: str = "", task_id: str = "",
    conversation_history: list[dict] | None = None,
    output_sink: IOutputSink | None = None, thread_id: str = "",
    client_message_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> InjectResult:
    """启动 idle 状态的引擎。

    agent 来源：引擎自带 self._agent_config（上次 run() 绑定的身份），或
    注册表 tags.agent_id（创建者注册时写入）。都没有说明创建者未正确注册，报错。
    """
    from pipeline.drain_manager import create_sink  # noqa: PLC0415
    _sink = output_sink or create_sink(pipeline_id, thread_id=thread_id)
    if _sink is None:
        return InjectResult(success=False, error="无法创建 sink", method="failed", pipeline_id=pipeline_id)

    # 引擎自带身份（上次 run() 绑定的）
    _resolved_agent = engine.agent_config or agent_config

    # 注册表 tags.agent_id（创建者注册时写入）
    if _resolved_agent is None:
        from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415
        from pipeline.registry import get_engine_registry  # noqa: PLC0415
        _entry = get_engine_registry().get(pipeline_id)
        _agent_id = _entry.tags.get("agent_id") if _entry else None
        if _agent_id:
            _registry = get_global_agent_registry_sync()
            if _registry:
                _resolved_agent = _registry.get(_agent_id)

    if _resolved_agent is None:
        # 诊断：输出每一步状态，定位注册失败点
        from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415
        from pipeline.registry import get_engine_registry  # noqa: PLC0415
        _diag_entry = get_engine_registry().get(pipeline_id)
        _diag_tags = _diag_entry.tags if _diag_entry else "NO_ENTRY"
        _diag_reg = get_global_agent_registry_sync()
        _diag_reg_count = len(_diag_reg.list_all()) if _diag_reg else 0
        logger.error(
            "[MessageBus] idle agent 解析失败诊断: pipeline=%s thread=%s "
            "engine.agent_config=%s entry_exists=%s tags=%s agent_registry_count=%d",
            pipeline_id[:12], thread_id[:12] if thread_id else "?",
            engine.agent_config is not None,
            _diag_entry is not None, _diag_tags, _diag_reg_count,
        )
        return InjectResult(
            success=False,
            error="idle 引擎重启失败：创建者未注册 agent_id 到注册表 tags",
            method="failed", pipeline_id=pipeline_id,
        )

    from pipeline.registry import get_engine_registry  # noqa: PLC0415
    _registry = get_engine_registry()

    # BUG-FIX-fix_20260619_idle_lost_task_id:
    # idle 重启会 build_initial_state 重建 state，task_id/workspace 若调用方
    # 未传则丢失。前端「停止→再发送」走 WS 路径（不知 task_id）、或其它非任务
    # 系统入口唤醒 idle 引擎时，L2 task_submit 会因 state[TASK_ID]='' 报
    # L2_REQUIRES_PARENT_TASK。
    # 修复：与 agent 身份恢复同源，从注册表 tags 补全 task_id/workspace。
    # tags 由管道创建者（task_executor 等）注册时写入，是上下文的权威来源。
    # 调用方显式传入的有效值优先（不覆盖）。
    _tags_entry = _registry.get(pipeline_id)
    _tags = getattr(_tags_entry, "tags", None) or {}
    if not task_id:
        task_id = _tags.get("task_id", "") or ""
    if not workspace:
        workspace = _tags.get("workspace", "") or ""
    # user_id / session_id 随上下文同源恢复，播种到管道 state
    # （task_submit 继承身份、task_status_update / task_status_changed 定位投递目标）
    _ctx_user_id = _tags.get("user_id", "") or ""
    _ctx_session_id = _tags.get("session_id", "") or ""

    # Phase 1 改造：仅创建/复用 bridge，engine 主动 emit 事件，不再启动 drain_loop。
    bridge = _registry.ensure_bridge(
        pipeline_id, _sink, engine=engine,
    )
    # Phase 1: on_chunk 由 engine 内部 _on_chunk_adapter 处理，不再从 bridge 读取。
    # REFACTOR-20260614: engine 在主循环运行，不再创建独立线程。
    engine_future = asyncio.ensure_future(engine.run(
        user_input=message, agent_config=_resolved_agent,
        conversation_history=conversation_history or [],
        task_id=task_id, workspace=workspace, project_root="",
        streaming=True, on_chunk=None,
        client_message_id=client_message_id,
        attachments=attachments,
        user_id=_ctx_user_id,
        session_id=_ctx_session_id,
    ))
    _idle_entry = _registry.get(pipeline_id)
    if _idle_entry:
        _idle_entry.engine_task = engine_future
    logger.info("[MessageBus] idle engine started (main loop) | pipeline=%s", pipeline_id[:12])
    return InjectResult(success=True, method="start", pipeline_id=pipeline_id, bridge=bridge)


# REFACTOR-20260614: _run_engine_in_thread 已删除。
# engine.run() 现在直接由 asyncio.ensure_future 调用。

# _revive_pipeline_message 已删除：send 遇到未注册管道直接拒绝（I4），
# 不再走自动 revive。引擎重建是持有者的责任（register）。


# ---------------------------------------------------------------------------
# Re-export：保持外部导入路径不变
# ---------------------------------------------------------------------------
from pipeline.drain_manager import (  # noqa: E402, F401
    create_sink as _create_sink,  # noqa: F401
    restart_drain as _restart_drain,  # noqa: F401
    start_bg_drain as _start_bg_drain,  # noqa: F401
)
# restore_pipelines_on_startup re-export 已删除：启动恢复移交持有者
# （TaskWorker.restore_running_pipelines），路由模块不再越权恢复。

# 兼容别名已删除：_try_revive_pipeline / _revive_pipeline_message 不再存在，
# 旧测试 patch 此名称将失败（这些测试随 revive 路径一并清理）。

async def emit(
    message: PipelineMessage,
    **kwargs: Any,
) -> InjectResult:
    """向管道发送消息的便捷公共接口。

    send_pipeline_message 的简洁别名，推荐外部模块（特别是 tools/）使用此接口，
    避免直接依赖 send_pipeline_message 的长函数名。

    Args:
        message: 标准内部消息对象
        **kwargs: 传递给 send_pipeline_message 的关键字参数

    Returns:
        InjectResult 注入结果
    """
    return await send_pipeline_message(message, **kwargs)


async def stop(pipeline_id: str) -> InjectResult:
    """唯一停止入口（I1 原子级联）。

    持有者级别的管道终结：cancel engine_task → 停 bridge → 移除 entry。
    用户点"停止生成"不走本函数，走 send(CONTROL) 信号路径（不删 entry）。
    本函数彻底移除 entry，下次发消息将走 register 重建。

    Args:
        pipeline_id: 要停止的管道 ID。

    Returns:
        InjectResult 注入结果。
    """
    from pipeline.registry import get_engine_registry  # noqa: PLC0415
    entry = get_engine_registry().get(pipeline_id)
    if entry is None:
        return InjectResult(success=False, error="管道未注册", method="rejected", pipeline_id=pipeline_id)

    # ① cancel engine_task（真正停 run 协程）
    if entry.engine_task is not None and not entry.engine_task.done():
        entry.engine_task.cancel()
    # ② 停 bridge（如有）
    if entry.bridge is not None:
        try:
            entry.bridge.stop()
        except Exception as exc:
            logger.debug("[MessageBus] bridge.stop 失败（非致命）: %s", exc)
    # ③ 引擎公开清理（不穿透私有成员）
    if hasattr(entry.engine, "cleanup"):
        try:
            await entry.engine.cleanup()
        except Exception as exc:
            logger.debug("[MessageBus] engine.cleanup 失败（非致命）: %s", exc)
    # ④ 移除 entry（I1：注册表无 = 引擎不存在）
    get_engine_registry().unregister(pipeline_id)
    logger.info("[MessageBus] 管道已停止: pipeline=%s", pipeline_id[:12])
    return InjectResult(success=True, method="stop", pipeline_id=pipeline_id)


__all__ = [
    "InjectResult",
    "send_pipeline_message",
    "emit",
    "stop",
]
