"""管道消息总线 — 统一消息注入入口。

所有"给管道发消息"的操作都通过 send_pipeline_message() 进行，
不再由各调用方自行判断管道状态和选择注入方式。

内部通过 EngineRegistry 一次查找引擎，并使用 engine.inject_message()
统一注入，自动根据引擎状态选择注入路径：
1. 运行中引擎 → inject_message() → notification 路径
2. 挂起引擎   → inject_message() → wake 路径
3. 无引擎+有历史 → 从历史重建引擎 + run()
4. 无引擎+无历史 → 返回失败
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)





@dataclass
class InjectResult:
    """消息注入结果。"""

    success: bool
    method: str = ""
    pipeline_id: str = ""
    error: str = ""
    bridge: Any = None  # 关联的 PipelineStreamBridge


def _find_engine(pipeline_id: str) -> tuple[Any | None, str]:
    """查找目标管道引擎实例。

    通过 EngineRegistry 一次查找。所有引擎在 _run_loop 启动和
    _suspend_and_wait 挂起时已同时写入 EngineRegistry，无需旧路径回退。

    Args:
        pipeline_id: 目标管道 ID

    Returns:
        (engine_instance, state) 元组，state 为 "running" 或 "suspended"；
        未找到返回 (None, "")
    """
    from pipeline.registry import get_engine_registry

    entry = get_engine_registry().get(pipeline_id)
    if entry is None:
        return None, ""
    engine = entry.engine

    # BUG-FIX-fix_20260524_msg_render:
    # 问题根因: 引擎 run() 完成后既不是 running 也不是 suspended（已完成状态），
    #   但函数仍然返回 (engine, "running")，导致后续对死引擎调用 inject_message 无效，
    #   消息丢失且不触发 revive 路径。
    # 修复方案: 增加 running/suspended 状态检查，仅当引擎真正处于活跃状态时才返回；
    #   对已完成的引擎返回 (None, "") 让消息走 revive 路径。
    # 影响范围: 所有消息注入路径（send_pipeline_message 中 _find_engine 调用）。
    # 修复日期: 2026-05-24
    if getattr(engine, "is_suspended", False):
        return engine, "suspended"
    if getattr(engine, "is_running", False):
        return engine, "running"
    if not getattr(engine, "_run_started", False):
        return engine, "idle"
    return None, ""


async def _auto_complete_interaction(pipeline_id: str) -> None:
    """自动完成管道的 pending conversation 模式交互请求。

    当引擎处于 running 状态且阻塞在 human_interaction (conversation 模式) 的
    wait_for_choice() 上时，新消息通过 notification 路径注入后无法被消费，
    因为 _run_loop 卡在 execute_core_plugin 中。通过自动完成交互请求，
    工具返回 conversation_mode=True，管道挂起后立即发现 notification 并唤醒。

    Args:
        pipeline_id: 管道 ID
    """
    try:
        from human_interaction import get_human_interaction_service
        service = get_human_interaction_service()
        if service is None:
            return
        count = await service.auto_complete_conversation_for_pipeline(pipeline_id)
        if count > 0:
            logger.info(
                "[MessageBus] 自动完成 %d 个 conversation 交互 | pipeline=%s",
                count, pipeline_id[:12],
            )
    except Exception as exc:
        logger.debug("[MessageBus] 自动完成交互检查失败（可忽略）: %s", exc)


async def _send_received_event(
    event_sink: Any,
    pipeline_id: str,
    thread_id: str,
    message_id: str,
    content: str,
    source: str,
) -> None:
    """发送 pipeline_received 事件到前端。

    用户消息的 sequence 由 WS 流式推送（stream_start）分配，
    pipeline_received 事件不额外分配 sequence，避免同一消息被分配两次。
    """
    if event_sink is None:
        return
    try:
        await event_sink.send_event({
            "type": "pipeline_received",
            "data": {
                "pipeline_id": pipeline_id,
                "thread_id": thread_id,
                "message_id": message_id,
                "content": content,
                "source": source,
            },
        })
    except Exception:
        pass


async def send_pipeline_message(
    pipeline_id: str,
    message: str,
    *,
    priority: int = 0,
    metadata: dict[str, Any] | None = None,
    agent_config: Any = None,
    workspace: str = "",
    task_id: str = "",
    conversation_history: list[dict] | None = None,
    streaming: bool = False,
    on_chunk: Callable | None = None,
    output_sink: Any = None,
    message_id: str = "",
    thread_id: str = "",
    **kwargs,
) -> InjectResult:
    """统一消息注入入口 — 所有外部调用方都使用此函数。

    自动判断管道状态并选择最佳注入策略：
    1. 引擎存在（running/suspended） → engine.inject_message() 统一注入
    2. 无引擎+有历史 → 从历史重建引擎 + run()
    3. 无引擎+无历史 → 返回 InjectResult(success=False)

    Args:
        pipeline_id: 目标管道 ID
        message: 消息内容
        priority: 消息优先级（预留）
        metadata: 消息元数据（预留）
        agent_config: Agent 配置（仅 revive 场景需要）
        workspace: 工作目录（仅 revive 场景需要）
        task_id: 关联任务 ID（仅 revive 场景需要）
        conversation_history: 对话历史（仅 revive 场景需要）
        streaming: 是否流式输出（仅 revive 场景需要）
        on_chunk: 流式回调（仅 revive 场景需要）
        output_sink: IOutputSink 实例，提供时自动创建/复用 bridge

    Returns:
        InjectResult 注入结果
    """
    if not pipeline_id:
        return InjectResult(success=False, error="pipeline_id 不能为空", method="failed")

    if not message:
        return InjectResult(success=False, error="message 不能为空", method="failed")

    _msg_source = (metadata or {}).get("source", "user")

    _event_sink = output_sink
    if _event_sink is None:
        try:
            from pipeline.registry import get_engine_registry as _greg
            _e = _greg().get(pipeline_id)
            if _e and _e.bridge and getattr(_e.bridge, "output_sink", None):
                _event_sink = _e.bridge.output_sink
        except Exception:
            pass
    if _event_sink is None:
        _event_sink = _create_sink(pipeline_id)

    if _msg_source != "user":
        # BUG-FIX-fix_20260529_notification_order:
        # 问题根因: system_notification 在流式输出期间立即推送 WS 事件，
        #   前端收到后分配到与正在流式的 AI 消息相同或相近的 sequence，
        #   导致通知"顶掉"正在输出的消息气泡。
        # 修复方案: 如果当前 bridge 正在流式输出，将通知缓冲到 bridge 的
        #   _pending_notifications 队列，stream_end 后统一刷出；
        #   否则立即推送（非流式期间无冲突风险）。
        try:
            from pipeline.registry import get_engine_registry as _greg
            _e = _greg().get(pipeline_id)
            _bridge = _e.bridge if _e else None
        except Exception:
            _bridge = None

        _is_streaming = (
            _bridge is not None
            and getattr(_bridge, '_stream_started', False)
            and _event_sink is not None
        )

        if _is_streaming:
            # 流式进行中，缓冲通知到 bridge
            _pending = getattr(_bridge, '_pending_notifications', None)
            if _pending is not None:
                _level = "info" if _msg_source in ("system", "trigger") else "warning"
                _pending.append({
                    "content": message,
                    "level": _level,
                    "notificationType": f"{_msg_source}_notification",
                })
                logger.info(
                    "[MessageBus] system_notification BUFFERED (streaming active): "
                    "pipeline=%s source=%s queue_len=%d content=%.60s",
                    pipeline_id[:12], _msg_source, len(_pending),
                    message[:60],
                )
            else:
                # bridge 无缓冲队列字段，降级为立即推送
                if _event_sink is not None:
                    try:
                        _level = "info" if _msg_source in ("system", "trigger") else "warning"
                        await _event_sink.send_event({
                            "type": "system_notification",
                            "data": {
                                "content": message,
                                "level": _level,
                                "notificationType": f"{_msg_source}_notification",
                                "pipeline_id": pipeline_id,
                            },
                        })
                    except Exception:
                        pass
        else:
            _level = "info" if _msg_source in ("system", "trigger") else "warning"
            _notif_event = {
                "type": "system_notification",
                "data": {
                    "content": message,
                    "level": _level,
                    "notificationType": f"{_msg_source}_notification",
                    "pipeline_id": pipeline_id,
                },
            }
            if _event_sink is not None:
                try:
                    await _event_sink.send_event(_notif_event)
                    logger.info(
                        "[MessageBus] system_notification SENT | pipeline=%s source=%s content=%.60s",
                        pipeline_id[:12], _msg_source,
                        message[:60],
                    )
                except Exception as _sn_err:
                    logger.warning(
                        "[MessageBus] system_notification FAILED | pipeline=%s source=%s error=%s",
                        pipeline_id[:12], _msg_source, _sn_err,
                    )
            else:
                try:
                    from pipeline.stream_bridge import send_frontend_event
                    _fallback_ok = await send_frontend_event(pipeline_id, _notif_event)
                    if _fallback_ok:
                        logger.info(
                            "[MessageBus] system_notification SENT (fallback) | pipeline=%s source=%s",
                            pipeline_id[:12], _msg_source,
                        )
                    else:
                        logger.warning(
                            "[MessageBus] system_notification SKIP: no sink & fallback failed | pipeline=%s source=%s",
                            pipeline_id[:12], _msg_source,
                        )
                except Exception as _fb_err:
                    logger.warning(
                        "[MessageBus] system_notification SKIP: no sink & fallback error | pipeline=%s source=%s error=%s",
                        pipeline_id[:12], _msg_source, _fb_err,
                    )

    engine, state = _find_engine(pipeline_id)

    if engine is not None:
        try:
            logger.info("[MessageBus] inject ENTER | pipeline=%s state=%s", pipeline_id[:12], state)

            if state == "idle":
                _sink = output_sink or _create_sink(pipeline_id)
                if _sink is not None:
                    from pipeline.registry import get_engine_registry
                    _registry = get_engine_registry()
                    bridge = _registry.ensure_bridge(pipeline_id, _sink)
                    _on_chunk = bridge.on_chunk if bridge else lambda chunk: None
                    engine_task = asyncio.create_task(
                        engine.run(
                            user_input=message,
                            agent_config=agent_config,
                            conversation_history=conversation_history or [],
                            task_id=task_id,
                            workspace=workspace,
                            project_root="",
                            streaming=True,
                            on_chunk=_on_chunk,
                        )
                    )
                    _start_bg_drain(pipeline_id, bridge, engine, engine_task=engine_task)
                    logger.info("[MessageBus] idle engine started | pipeline=%s", pipeline_id[:12])
                    if _msg_source == "user":
                        await _send_received_event(_event_sink, pipeline_id, thread_id, message_id, message, _msg_source)
                    return InjectResult(success=True, method="start", pipeline_id=pipeline_id, bridge=bridge)
                return InjectResult(success=False, error="无法创建 sink", method="failed", pipeline_id=pipeline_id)

            msg_source = (metadata or {}).get("source", "user")
            engine.inject_message(message, source=msg_source)
            method = "wake" if state == "suspended" else "notification"

            from pipeline.registry import get_engine_registry
            registry = get_engine_registry()
            bridge = registry.get_bridge(pipeline_id)
            if state == "suspended":
                _sink = output_sink or _create_sink(pipeline_id)
                if _sink is not None:
                    bridge = registry.ensure_bridge(
                        pipeline_id, _sink,
                        auto_start_drain=True,
                        engine=engine,
                    )
                    logger.info("[MessageBus] ensure_bridge for suspended | pipeline=%s has_bridge=%s", pipeline_id[:12], bridge is not None)
                else:
                    logger.warning("[MessageBus] FAILED to create sink for suspended | pipeline=%s", pipeline_id[:12])

            if state == "running" and msg_source == "user":
                await _auto_complete_interaction(pipeline_id)

            logger.info(
                "[MessageBus] 消息已注入 | pipeline=%s method=%s",
                pipeline_id[:12], method,
            )
            if _msg_source == "user":
                await _send_received_event(_event_sink, pipeline_id, thread_id, message_id, message, _msg_source)
            return InjectResult(
                success=True, method=method, pipeline_id=pipeline_id, bridge=bridge,
            )
        except Exception as exc:
            logger.warning("[MessageBus] 消息注入失败: %s", exc)

    logger.warning(
        "[MessageBus] 引擎未找到，尝试 revive | pipeline=%s",
        pipeline_id[:12],
    )

    revive_bridge = None
    _revive_sink = output_sink or _create_sink(pipeline_id)
    if _revive_sink is not None:
        from pipeline.stream_bridge import PipelineStreamBridge
        revive_bridge = PipelineStreamBridge(
            pipeline_id=pipeline_id,
            output_sink=_revive_sink,
        )

    revive_result = await _try_revive_pipeline(
        pipeline_id, message,
        agent_config=agent_config,
        workspace=workspace,
        task_id=task_id,
        conversation_history=conversation_history,
        streaming=streaming or (revive_bridge is not None),
        on_chunk=revive_bridge.on_chunk if revive_bridge else on_chunk,
        revive_bridge=revive_bridge,
        **kwargs,
    )

    if revive_result.success and revive_bridge is not None:
        from pipeline.registry import get_engine_registry
        get_engine_registry().set_bridge(pipeline_id, revive_bridge)
        revive_result.bridge = revive_bridge

    if _msg_source == "user" and revive_result.success:
        await _send_received_event(_event_sink, pipeline_id, thread_id, message_id, message, _msg_source)

    return revive_result


async def _try_revive_pipeline(
    pipeline_id: str,
    message: str,
    *,
    agent_config: Any = None,
    workspace: str = "",
    task_id: str = "",
    conversation_history: list[dict] | None = None,
    streaming: bool = False,
    on_chunk: Callable | None = None,
    revive_bridge: Any = None,
    **kwargs,
) -> InjectResult:
    """尝试从历史记录恢复管道。

    当引擎不在内存时，从 execution_record_storage 加载历史记录，
    创建新 PipelineEngine 并以 message 为 user_input 运行。

    Args:
        pipeline_id: 目标管道 ID
        message: 消息内容
        agent_config: Agent 配置
        workspace: 工作目录
        task_id: 关联任务 ID
        conversation_history: 对话历史（优先使用，为空则从存储加载）
        streaming: 是否流式输出
        on_chunk: 流式回调

    Returns:
        InjectResult 注入结果
    """
    engine, _ = _find_engine(pipeline_id)
    if engine is not None:
        logger.warning("[MessageBus] _try_revive 时引擎已存在，跳过: pipeline=%s", pipeline_id)
        return InjectResult(success=False, error="引擎已存在", method="failed", pipeline_id=pipeline_id)

    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
    except Exception:
        provider = None

    if conversation_history is None:
        conversation_history = _load_history_from_storage(pipeline_id, provider)
        if conversation_history is None:
            return InjectResult(
                success=False,
                error=f"管道 {pipeline_id} 不存在且无历史记录",
                method="failed",
                pipeline_id=pipeline_id,
            )

    if agent_config is None and provider and task_id:
        agent_config = _load_agent_config(task_id, provider)

    # BUG-FIX-fix_20260514_history_rebuild:
    # 问题根因: agent_config 为 None 时（调用方未传入且 task_id 为空），
    #   allow_default_fallback=False 会导致 engine.run() 直接抛出 ValueError，
    #   管道复活永远失败。重启后通过 send_pipeline_message 路径发消息时
    #   通常没有 task_id，因此 agent_config 始终为 None。
    # 修复方案: 当 agent_config 为 None 时，尝试从 ServiceProvider 加载
    #   默认 Agent 配置；如果仍为 None，将 allow_default_fallback 改为 True
    #   让 engine.run() 内部回退到系统默认 Agent（灵汐）。
    _allow_default_fallback = False
    if agent_config is None:
        try:
            _agent_reg = provider.get("agent_registry") if provider else None
            if _agent_reg:
                for _candidate in ["default", "lingxi"]:
                    agent_config = _agent_reg.get(_candidate)
                    if agent_config:
                        break
        except Exception:
            pass
        if agent_config is None:
            _allow_default_fallback = True
            logger.info(
                "[MessageBus] 管道复活无 agent_config，回退到系统默认: pipeline=%s",
                pipeline_id[:12],
            )

    try:
        from pipeline.registry import get_engine_registry

        _registry = get_engine_registry()

        input_route_table = provider.get("input_route_table") if provider else None
        output_route_table = provider.get("output_route_table") if provider else None
        plugin_registry = provider.get("plugin_registry") if provider else None
        services = provider._services if provider else {}

        entry = _registry.revive_pipeline(
            pipeline_id,
            input_route_table=input_route_table,
            output_route_table=output_route_table,
            plugin_registry=plugin_registry,
            services=services,
        )
        if entry is None:
            logger.warning("[MessageBus] 管道恢复失败（缺少必要参数）: pipeline=%s", pipeline_id[:12])
            return InjectResult(
                success=False,
                error="管道恢复失败",
                method="failed",
                pipeline_id=pipeline_id,
            )

        new_engine = entry.engine

        # BUG-FIX-fix_20260524_deadlock:
        # 问题根因: await engine.run() 在消息处理后引擎会进入 suspended 状态，
        #   run() 方法不会返回而是进入 _wake_event.wait() 等待循环。
        #   这导致 _try_revive_pipeline 永远阻塞，send_pipeline_message 不返回，
        #   app_factory.py 中的 drain_loop 永远不启动。
        # 修复方案: 用 asyncio.create_task 异步启动 engine.run()，
        #   _try_revive_pipeline 立即返回，让 app_factory.py 启动 drain_loop。
        #   engine.run() 在后台执行，通过 on_chunk 回调将 LLM 输出发送到 bridge。
        engine_task = asyncio.create_task(
            new_engine.run(
                user_input=message,
                agent_config=agent_config,
                conversation_history=conversation_history,
                task_id=task_id,
                workspace=workspace,
                project_root="",
                allow_default_fallback=_allow_default_fallback,
                streaming=streaming,
                on_chunk=on_chunk or (lambda chunk: None),
            )
        )

        # BUG-FIX-fix_20260525_realtime_render:
        # revive 路径也需要 drain_loop 消费队列推送到前端
        if revive_bridge is not None:
            _start_bg_drain(pipeline_id, revive_bridge, new_engine, engine_task)

        logger.info("[MessageBus] 管道复活已启动(异步): pipeline=%s", pipeline_id[:12])
        return InjectResult(success=True, method="revive", pipeline_id=pipeline_id)

    except Exception as exc:
        logger.error("[MessageBus] 管道复活失败: pipeline=%s, error=%s", pipeline_id, exc)
        return InjectResult(
            success=False,
            error=f"管道复活失败: {exc}",
            method="failed",
            pipeline_id=pipeline_id,
        )


def _load_history_from_storage(
    pipeline_id: str,
    provider: Any | None,
) -> list[dict[str, Any]] | None:
    """从 execution_record_storage 加载管道历史记录。

    Args:
        pipeline_id: 管道 ID
        provider: ServiceProvider 实例

    Returns:
        历史记录列表，无记录返回 None
    """
    if not provider:
        return None

    exec_storage = provider.get("execution_record_storage")
    if not exec_storage:
        return None

    try:
        records = exec_storage.list_by_pipeline(pipeline_id)
    except Exception:
        return None

    if not records:
        return None

    history: list[dict[str, Any]] = []
    for r in records:
        msg: dict[str, Any] = {"role": r.role, "content": r.content}
        if getattr(r, "name", None):
            msg["name"] = r.name
        if getattr(r, "tool_call_id", None):
            msg["tool_call_id"] = r.tool_call_id
        if getattr(r, "tool_input", None):
            msg["tool_input"] = r.tool_input
        if getattr(r, "tool_calls_json", None):
            try:
                msg["tool_calls"] = json.loads(r.tool_calls_json)
            except (json.JSONDecodeError, TypeError):
                pass
        history.append(msg)

    try:
        from infrastructure.task_worker import _reconstruct_tool_calls
        _reconstruct_tool_calls(history)
    except ImportError:
        pass

    logger.info(
        "[MessageBus] 从存储恢复 %d 条历史记录: pipeline=%s",
        len(history), pipeline_id,
    )
    return history


def _load_agent_config(task_id: str, provider: Any) -> Any | None:
    """从 task_service 加载任务的 agent_config。

    Args:
        task_id: 任务 ID
        provider: ServiceProvider 实例

    Returns:
        AgentConfig 实例，未找到返回 None
    """
    task_service = provider.get("task_service")
    if not task_service:
        return None

    try:
        task_obj = task_service.get_task(task_id)
        if not task_obj:
            return None
        target_id = getattr(task_obj, "target_id", None)
        if not target_id:
            return None
        agent_registry = provider.get("agent_registry")
        if agent_registry:
            return agent_registry.get(target_id)
    except Exception:
        pass

    return None


def _create_sink(pipeline_id: str) -> Any | None:
    """从 registry 获取 thread_id 创建 TargetedSink。

    BUG-FIX-fix_20260526_sink_notifier:
    问题根因: 原代码通过 service_provider.get("ws_interaction_notifier") 获取 notifier，
      但 ws_interaction_notifier 是 ws_handler.py 中的全局单例，从未注册到 service_provider。
      导致 _create_sink 永远返回 None，所有 system_notification 无法推送。
    修复方案: 直接 import ws_handler.ws_interaction_notifier 全局单例。
    """
    try:
        from pipeline.registry import get_engine_registry
        from pipeline.stream_bridge import TargetedSink

        registry = get_engine_registry()
        entry = registry.get(pipeline_id)
        thread_id = entry.thread_id if entry else ""

        from ws_handler import ws_interaction_notifier
        if not ws_interaction_notifier:
            logger.warning("[MessageBus] _create_sink: notifier is None | pipeline=%s", pipeline_id[:12])
            return None

        if not thread_id:
            logger.warning("[MessageBus] _create_sink: no thread_id, using empty (will broadcast) | pipeline=%s", pipeline_id[:12])

        return TargetedSink(ws_interaction_notifier, thread_id)
    except Exception as _cs_err:
        logger.warning("[MessageBus] _create_sink FAILED: pipeline=%s error=%s", pipeline_id[:12], _cs_err)
        return None


def _start_bg_drain(
    pipeline_id: str,
    bridge: Any,
    engine: Any,
    engine_task: asyncio.Task | None = None,
) -> None:
    """后台启动 drain_loop 消费 bridge 队列，推送事件到前端。

    创建 drain 任务并将 asyncio.Task 引用存入 EngineRegistry，
    供 ensure_bridge 复用 bridge 时取消旧 drain，以及
    stop_generation 场景即时停止流式输出。
    """
    async def _engine_tracker() -> None:
        await asyncio.sleep(0.5)
        while True:
            if not getattr(engine, 'is_running', False) and not getattr(engine, 'is_suspended', False):
                break
            await asyncio.sleep(0.3)

    tracker = engine_task or asyncio.create_task(_engine_tracker())

    async def _drain_and_cleanup() -> None:
        try:
            result = await bridge.drain_loop(
                tracker,
                heartbeat_interval=5.0,
                suspend_check=lambda: getattr(engine, "is_suspended", False),
            )
            content = result.get("accumulated_content", "")
            if content:
                _ai_seq = getattr(bridge, '_last_ai_sequence', 0)
                if _ai_seq <= 0:
                    _ai_seq = getattr(bridge, '_current_msg_seq', 0)
                await bridge.send_new_message(content, sequence=_ai_seq)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("[MessageBus] bg drain 异常: pipeline=%s error=%s", pipeline_id[:12], exc)
        finally:
            if engine_task is None:
                tracker.cancel()
            if not getattr(engine, 'is_running', False) and not getattr(engine, 'is_suspended', False):
                try:
                    from pipeline.registry import get_engine_registry
                    reg = get_engine_registry()
                    # 清理 drain_task 引用（仅当仍是自身时）
                    entry = reg.get(pipeline_id)
                    if entry and entry.drain_task is asyncio.current_task():
                        entry.drain_task = None
                    reg.unregister(pipeline_id)
                except Exception:
                    pass
            else:
                # 引擎仍在运行/挂起，仅清理 drain_task 引用
                try:
                    from pipeline.registry import get_engine_registry
                    entry = get_engine_registry().get(pipeline_id)
                    if entry and entry.drain_task is asyncio.current_task():
                        entry.drain_task = None
                except Exception:
                    pass

    task = asyncio.create_task(_drain_and_cleanup())
    # 将 drain task 引用存入 EngineRegistry，供外部取消
    try:
        from pipeline.registry import get_engine_registry
        entry = get_engine_registry().get(pipeline_id)
        if entry:
            entry.drain_task = task
    except Exception:
        pass


async def restore_pipelines_on_startup() -> int:
    """应用启动时恢复 running/pending 状态的管道。

    从持久化存储加载未完成的任务，为每个任务创建引擎并注册到 Registry，
    使 send_pipeline_message 能直接找到引擎而无需走 revive 路径。

    Returns:
        恢复的管道数量
    """
    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
    except Exception:
        logger.warning("[MessageBus] restore_pipelines_on_startup: ServiceProvider 不可用")
        return 0

    task_service = provider.get("task_service") if provider else None
    if not task_service:
        logger.info("[MessageBus] restore_pipelines_on_startup: task_service 不可用，跳过")
        return 0

    restored = 0
    for status in ("running", "pending"):
        try:
            tasks = task_service.list_tasks(status=status) if hasattr(task_service, "list_tasks") else []
        except Exception:
            tasks = []

        for task in tasks:
            pipeline_id = task.get("pipeline_id") if isinstance(task, dict) else getattr(task, "pipeline_id", None)
            if not pipeline_id:
                continue

            from pipeline.registry import get_engine_registry
            registry = get_engine_registry()

            if registry.get(pipeline_id):
                continue

            input_route_table = provider.get("input_route_table") if provider else None
            output_route_table = provider.get("output_route_table") if provider else None
            plugin_registry = provider.get("plugin_registry") if provider else None
            services = provider._services if provider else {}

            entry = registry.revive_pipeline(
                pipeline_id,
                input_route_table=input_route_table,
                output_route_table=output_route_table,
                plugin_registry=plugin_registry,
                services=services,
                tags={"mode": "interactive", "task_id": task.get("task_id", "") if isinstance(task, dict) else getattr(task, "task_id", ""), "source": "startup_restore"},
            )
            if entry:
                restored += 1
                logger.info(
                    "[MessageBus] 启动恢复: pipeline=%s status=%s",
                    pipeline_id[:12], status,
                )

    if restored:
        logger.info("[MessageBus] restore_pipelines_on_startup 完成: 恢复 %d 个管道", restored)
    return restored
