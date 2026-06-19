"""管道消息总线 — 统一消息注入入口。

所有"给管道发消息"的操作都通过 send_pipeline_message() 进行。
路口不区分来源（WS/CLI/任务系统/触发器），只做 pipeline_id → 注册表找引擎 → 转发。
agent 由数据源决定，不在此解析。

公共接口:
    send_pipeline_message: 统一入口，接受 PipelineMessage 对象
    InjectResult: 注入结果数据类
    restore_pipelines_on_startup: 启动恢复

实现已拆分到子模块：
- pipeline_reviver.py: 管道复活与启动恢复
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
    from pipeline.registry import get_engine_registry
    entry = get_engine_registry().get(pipeline_id)
    if entry is None:
        return None, ""
    engine = entry.engine
    # BUG-FIX-fix_20260524_msg_render: 增加 running/suspended 状态检查
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
        from human_interaction import get_human_interaction_service
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

    if not pipeline_id:
        return InjectResult(success=False, error="pipeline_id 不能为空", method="failed")

    # 仅拦截非空但纯空白的消息
    if content is not None and len(content) > 0 and not content.strip():
        return InjectResult(success=False, error="message 不能仅包含空白字符", method="failed")

    engine, state = _find_engine(pipeline_id)

    # BUG-FIX-fix_20260531_sink_dead_thread_id_lost: 主动更新 registry 中缺失的 thread_id
    if thread_id and pipeline_id:
        try:
            from pipeline.registry import get_engine_registry as _reg_get
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
        )

    logger.warning("[MessageBus] 引擎未找到，尝试 revive | pipeline=%s", pipeline_id[:12])
    return await _revive_pipeline_message(
        pipeline_id, content,
        agent_config=request.agent_config, workspace=request.workspace,
        task_id=request.task_id, conversation_history=request.conversation_history,
        streaming=request.streaming, on_chunk=None,
        output_sink=request.output_sink, thread_id=thread_id,
        client_message_id=client_message_id,
    )


async def _inject_to_engine(
    pipeline_id: str, engine: Any, state: str, message: str,
    metadata: dict | None, agent_config: AgentConfig | None, workspace: str,
    task_id: str, conversation_history: list[dict] | None,
    output_sink: IOutputSink | None, thread_id: str,
    client_message_id: str = "",
) -> InjectResult:
    """向已存在的引擎注入消息。"""
    from pipeline.drain_manager import create_sink
    try:

        msg_source = (metadata or {}).get("source", "user")
        logger.info("[MessageBus] 消息注入: pipeline=%s state=%s source=%s msg=%.60s",
                     pipeline_id[:12], state, msg_source, message or "(empty)")

        # 非 user 消息：通过 bridge 推送 system_notification（和 AI stream 走同一通道，保证时序）。
        # emit_notification 是 async，在 inject_message 之前调度，保证 notification 在 stream chunk 之前。
        if msg_source != "user":
            from pipeline.registry import get_engine_registry as _reg_for_push
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
) -> InjectResult:
    """启动 idle 状态的引擎。

    agent 来源：引擎自带 self._agent_config（上次 run() 绑定的身份），或
    注册表 tags.agent_id（创建者注册时写入）。都没有说明创建者未正确注册，报错。
    """
    from pipeline.drain_manager import create_sink
    _sink = output_sink or create_sink(pipeline_id, thread_id=thread_id)
    if _sink is None:
        return InjectResult(success=False, error="无法创建 sink", method="failed", pipeline_id=pipeline_id)

    # 引擎自带身份（上次 run() 绑定的）
    _resolved_agent = engine.agent_config or agent_config

    # 注册表 tags.agent_id（创建者注册时写入）
    if _resolved_agent is None:
        from pipeline.registry import get_engine_registry
        from agents.global_registry import get_global_agent_registry_sync
        _entry = get_engine_registry().get(pipeline_id)
        _agent_id = _entry.tags.get("agent_id") if _entry else None
        if _agent_id:
            _registry = get_global_agent_registry_sync()
            if _registry:
                _resolved_agent = _registry.get(_agent_id)

    if _resolved_agent is None:
        # 诊断：输出每一步状态，定位注册失败点
        from pipeline.registry import get_engine_registry
        from agents.global_registry import get_global_agent_registry_sync
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

    from pipeline.registry import get_engine_registry
    _registry = get_engine_registry()

    # BUG-FIX-fix_20260619_idle_lost_task_id:
    # idle 重启会 build_initial_state 重建 state，task_id/workspace 若调用方
    # 未传则丢失。前端「停止→再发送」走 WS 路径（不知 task_id）、或其它非任务
    # 系统入口唤醒 idle 引擎时，L2 task_submit 会因 state[TASK_ID]='' 报
    # L2_REQUIRES_PARENT_TASK。
    # 修复：与 agent 身份恢复同源，从注册表 tags 补全 task_id/workspace。
    # tags 由管道创建者（task_executor 等）注册时写入，是上下文的权威来源。
    # 调用方显式传入的有效值优先（不覆盖）。
    if not task_id or not workspace:
        _ctx_entry = _registry.get(pipeline_id)
        if _ctx_entry and getattr(_ctx_entry, "tags", None):
            _tags = _ctx_entry.tags
            if not task_id:
                task_id = _tags.get("task_id", "") or ""
            if not workspace:
                workspace = _tags.get("workspace", "") or ""

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
    ))
    _idle_entry = _registry.get(pipeline_id)
    if _idle_entry:
        _idle_entry.engine_task = engine_future
    logger.info("[MessageBus] idle engine started (main loop) | pipeline=%s", pipeline_id[:12])
    return InjectResult(success=True, method="start", pipeline_id=pipeline_id, bridge=bridge)


# REFACTOR-20260614: _run_engine_in_thread 已删除。
# engine.run() 现在直接由 asyncio.ensure_future 调用。


async def _revive_pipeline_message(
    pipeline_id: str, message: str, *,
    agent_config: AgentConfig | None = None, workspace: str = "", task_id: str = "",
    conversation_history: list[dict] | None = None, streaming: bool = False,
    on_chunk: Callable | None = None, output_sink: IOutputSink | None = None,
    thread_id: str = "", client_message_id: str = "", **kwargs: Any,
) -> InjectResult:
    """走 revive 路径的消息注入。"""
    from pipeline.drain_manager import create_sink
    revive_bridge = None
    _revive_sink = output_sink or create_sink(pipeline_id, thread_id=thread_id)
    if _revive_sink is not None:
        from pipeline.stream_bridge import PipelineStreamBridge
        revive_bridge = PipelineStreamBridge(
            pipeline_id=pipeline_id, output_sink=_revive_sink,
        )

    from pipeline.pipeline_reviver import try_revive_pipeline
    revive_result = await try_revive_pipeline(
        pipeline_id, message, agent_config=agent_config, workspace=workspace,
        task_id=task_id, conversation_history=conversation_history,
        streaming=streaming or (revive_bridge is not None),
        on_chunk=None,
        revive_bridge=revive_bridge, **kwargs,
    )

    if revive_result.success and revive_bridge is not None:
        from pipeline.registry import get_engine_registry
        get_engine_registry().set_bridge(pipeline_id, revive_bridge)
        revive_result.bridge = revive_bridge
    return revive_result


# ---------------------------------------------------------------------------
# Re-export：保持外部导入路径不变
# ---------------------------------------------------------------------------
from pipeline.drain_manager import (  # noqa: E402, F401
    create_sink as _create_sink,  # noqa: F401
    restart_drain as _restart_drain,  # noqa: F401
    start_bg_drain as _start_bg_drain,  # noqa: F401
)
from pipeline.pipeline_reviver import restore_pipelines_on_startup  # noqa: E402

# 兼容别名：旧测试 patch("pipeline.message_bus._try_revive_pipeline") 需要此名称
_try_revive_pipeline = _revive_pipeline_message

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


__all__ = [
    "InjectResult",
    "send_pipeline_message",
    "emit",
    "restore_pipelines_on_startup",
]
