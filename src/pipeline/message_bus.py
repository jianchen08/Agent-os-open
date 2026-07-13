"""管道消息总线 — 统一消息注入入口。"""

from __future__ import annotations

import asyncio
import logging
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
    """查找目标管道引擎实例。返回 (engine, state) 元组。"""
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
    """统一消息注入入口 — 接受 PipelineMessage 对象。"""
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
    """核心注入逻辑 — 接受 PipelineRequest 对象。"""
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

    # 主动更新 registry 中缺失的 thread_id
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
            pipeline_id,
            engine,
            state,
            content,
            metadata,
            request.agent_config,
            request.workspace,
            request.task_id,
            request.conversation_history,
            request.output_sink,
            thread_id,
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
    """I6：投递控制信号到引擎（不进 inject 队列，不删 entry）。"""
    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    entry = get_engine_registry().get(pipeline_id)
    if entry is None:
        return InjectResult(
            success=False,
            error="管道未注册，无法投递信号",
            method="rejected",
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
            pipeline_id[:12],
            (msg.metadata or {}).get("signal_type", "?"),
        )
        return InjectResult(success=True, method="signal", pipeline_id=pipeline_id)
    logger.debug("[MessageBus] 引擎不支持信号投递（无 deliver_signal）: pipeline=%s", pipeline_id[:12])
    return InjectResult(success=False, error="引擎不支持信号投递", method="rejected", pipeline_id=pipeline_id)


async def _inject_to_engine(
    pipeline_id: str,
    engine: Any,
    state: str,
    message: str,
    metadata: dict | None,
    agent_config: AgentConfig | None,
    workspace: str,
    task_id: str,
    conversation_history: list[dict] | None,
    output_sink: IOutputSink | None,
    thread_id: str,
    client_message_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> InjectResult:
    """向已存在的引擎注入消息。"""
    try:
        msg_source = (metadata or {}).get("source", "user")
        logger.info(
            "[MessageBus] 消息注入: pipeline=%s state=%s source=%s msg=%.60s",
            pipeline_id[:12],
            state,
            msg_source,
            message or "(empty)",
        )

        # 非 user 消息（系统通知）的前端推送统一由 consume_pending_notifications
        # 在「消息出队列进下一轮迭代」时推送 —— 那是唯一的 system 通知推送点。
        # 此处（注入入口）不再推送，避免「注入入口推一次 + 历史接口再渲染一次」的重复。

        if state == "idle":
            return await _start_idle_engine(
                pipeline_id,
                engine,
                message,
                agent_config=agent_config,
                workspace=workspace,
                task_id=task_id,
                conversation_history=conversation_history,
                output_sink=output_sink,
                thread_id=thread_id,
                client_message_id=client_message_id,
                attachments=attachments,
            )

        engine.inject_message(message, source=msg_source, client_message_id=client_message_id)
        logger.info(
            "[MessageBus] 已注入引擎: pipeline=%s source=%s method=%s queue=%d",
            pipeline_id[:12],
            msg_source,
            "wake" if state == "suspended" else "notification",
            engine.inject_queue_size,
        )
        method = "wake" if state == "suspended" else "notification"

        if state == "running" and msg_source == "user":
            await _auto_complete_interaction(pipeline_id)

        logger.info("[MessageBus] 消息已注入 | pipeline=%s method=%s", pipeline_id[:12], method)
        return InjectResult(success=True, method=method, pipeline_id=pipeline_id, bridge=None)
    except Exception as exc:
        logger.warning("[MessageBus] 消息注入失败: %s", exc)
        return InjectResult(success=False, error=str(exc), method="failed", pipeline_id=pipeline_id)


async def _start_idle_engine(
    pipeline_id: str,
    engine: Any,
    message: str,
    *,
    agent_config: AgentConfig | None = None,
    workspace: str = "",
    task_id: str = "",
    conversation_history: list[dict] | None = None,
    output_sink: IOutputSink | None = None,
    thread_id: str = "",
    client_message_id: str = "",
    attachments: list[dict[str, Any]] | None = None,
) -> InjectResult:
    """启动 idle 状态的引擎。"""
    from pipeline.drain_manager import create_sink  # noqa: PLC0415

    _sink = output_sink or create_sink(pipeline_id, thread_id=thread_id)
    if _sink is None:
        return InjectResult(success=False, error="无法创建 sink", method="failed", pipeline_id=pipeline_id)

    # 优先从 registry 解析：tags.agent_id 指向配置 ID（创建者注册时写入，整个 pipeline
    # 生命周期稳定），重新查询总能拿到热重载后的最新配置，确保同会话内改 YAML 立即生效。
    # 仅当 registry 查不到（如 tags 未写 agent_id 的历史创建路径）时，才回退到
    # 引擎/调用方缓存的配置，保留兼容。
    from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415
    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    _resolved_agent = None
    _entry = get_engine_registry().get(pipeline_id)
    _agent_id = _entry.tags.get("agent_id") if _entry else None
    if _agent_id:
        _registry = get_global_agent_registry_sync()
        if _registry:
            _resolved_agent = _registry.get(_agent_id)
    if _resolved_agent is None:
        _resolved_agent = engine.agent_config or agent_config

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
            pipeline_id[:12],
            thread_id[:12] if thread_id else "?",
            engine.agent_config is not None,
            _diag_entry is not None,
            _diag_tags,
            _diag_reg_count,
        )
        return InjectResult(
            success=False,
            error="idle 引擎重启失败：创建者未注册 agent_id 到注册表 tags",
            method="failed",
            pipeline_id=pipeline_id,
        )

    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    _registry = get_engine_registry()

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
        pipeline_id,
        _sink,
        engine=engine,
    )
    # Phase 1: on_chunk 由引擎的流式输出口 StreamingOutput（engine._streaming）处理，
    # 不再从 bridge 读取。详见 pipeline/engine_streaming.py。
    # engine 在主循环运行，不再创建独立线程。
    engine_future = asyncio.ensure_future(
        engine.run(
            user_input=message,
            agent_config=_resolved_agent,
            conversation_history=conversation_history or [],
            task_id=task_id,
            workspace=workspace,
            project_root="",
            streaming=True,
            on_chunk=None,
            client_message_id=client_message_id,
            attachments=attachments,
            user_id=_ctx_user_id,
            session_id=_ctx_session_id,
        )
    )
    _idle_entry = _registry.get(pipeline_id)
    if _idle_entry:
        _idle_entry.engine_task = engine_future
    logger.info("[MessageBus] idle engine started (main loop) | pipeline=%s", pipeline_id[:12])
    return InjectResult(success=True, method="start", pipeline_id=pipeline_id, bridge=bridge)


# _revive_pipeline_message 已删除：send 遇到未注册管道直接拒绝（I4），
# 不再走自动 revive。引擎重建是持有者的责任（register）。


# Re-export：保持外部导入路径不变
from pipeline.drain_manager import (  # noqa: E402, F401
    create_sink as _create_sink,  # noqa: F401
    restart_drain as _restart_drain,  # noqa: F401
    start_bg_drain as _start_bg_drain,  # noqa: F401
)

# restore_pipelines_on_startup re-export 已删除：启动恢复由各持有者负责
# （会话模块 restore_session_pipelines 注册主管道；TaskWorker._recover_running_tasks
# 将 running/pending 任务标记 suspended）。路由模块不越权恢复。


async def emit(
    message: PipelineMessage,
    **kwargs: Any,
) -> InjectResult:
    """向管道发送消息的便捷公共接口。"""
    return await send_pipeline_message(message, **kwargs)


async def stop(pipeline_id: str) -> InjectResult:
    """唯一停止入口（I1 原子级联）。"""
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


async def run_once(
    message: PipelineMessage,
    *,
    cleanup: bool = True,
) -> tuple[InjectResult, dict[str, Any]]:
    """同步执行并拿结果：send → 等引擎结束 → 读 final state → (可选 stop)。"""
    from pipeline.registry import get_engine_registry  # noqa: PLC0415

    registry = get_engine_registry()
    pipeline_id = message.pipeline_id

    # ① send（触发 run；未注册则 send 拒绝，由持有者负责 register）
    inject_result = await send_pipeline_message(message)
    if not inject_result.success:
        return (inject_result, {})

    # ② 等引擎主循环结束（经 entry 访问 engine_task，使用者不持有 engine）
    entry = registry.get(pipeline_id)
    if entry is not None and entry.engine_task is not None:
        await entry.engine_task

    # ③ 读 final state（经 entry 访问，使用者不持有 engine）
    final_state = entry.engine.last_state if entry is not None else {}

    # ④ 可选 stop 清理（一次性场景）
    if cleanup:
        try:
            await stop(pipeline_id)
        except Exception as exc:
            logger.debug("[MessageBus] run_once cleanup 失败（非致命）: %s", exc)

    return (inject_result, final_state)


__all__ = [
    "InjectResult",
    "send_pipeline_message",
    "emit",
    "stop",
    "run_once",
]
