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
    if getattr(engine, "is_suspended", False):
        return engine, "suspended"
    return engine, "running"


def _update_bridge(pipeline_id: str, engine: Any, output_sink: Any) -> None:
    """为管道创建或复用 bridge，并关联到 EngineRegistry。

    当 output_sink 存在时，检查 EngineRegistry 中是否已有 bridge；
    若无则新建 PipelineStreamBridge 并注册，同时将 bridge.on_chunk
    设置到引擎的 _saved_on_chunk 和 _suspended_state 中。

    Args:
        pipeline_id: 管道 ID
        engine: PipelineEngine 实例
        output_sink: IOutputSink 实例
    """
    from pipeline.registry import get_engine_registry
    from pipeline.stream_bridge import PipelineStreamBridge

    registry = get_engine_registry()
    entry = registry.get(pipeline_id)
    if entry is None:
        return
    bridge = entry.bridge
    if bridge is None:
        import uuid

        bridge = PipelineStreamBridge(
            pipeline_id=pipeline_id,
            output_sink=output_sink,
            message_id=f"msg_{uuid.uuid4().hex[:12]}",
        )
        registry.set_bridge(pipeline_id, bridge)
    _set_streaming = getattr(engine, "set_streaming_context", None)
    if _set_streaming:
        _set_streaming(bridge.on_chunk, streaming=True)
    else:
        engine._saved_on_chunk = bridge.on_chunk
        engine._saved_streaming = True
    if getattr(engine, "_suspended_state", None) is not None:
        engine._suspended_state["on_chunk"] = bridge.on_chunk
        engine._suspended_state["streaming"] = True


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

    engine, state = _find_engine(pipeline_id)

    if engine is not None:
        try:
            engine.inject_message(message)
            # 如果有 output_sink，尝试设置 bridge
            if output_sink is not None:
                _update_bridge(pipeline_id, engine, output_sink)
            method = "wake" if state == "suspended" else "notification"
            logger.info(
                "[MessageBus] 消息已注入管道 | pipeline=%s | method=%s | preview=%.60s",
                pipeline_id, method, message,
            )
            # 获取关联的 bridge 用于返回
            from pipeline.registry import get_engine_registry
            bridge = get_engine_registry().get_bridge(pipeline_id)
            return InjectResult(
                success=True, method=method, pipeline_id=pipeline_id, bridge=bridge,
            )
        except Exception as exc:
            logger.warning("[MessageBus] 消息注入失败: %s", exc)

    return await _try_revive_pipeline(
        pipeline_id, message,
        agent_config=agent_config,
        workspace=workspace,
        task_id=task_id,
        conversation_history=conversation_history,
        streaming=streaming,
        on_chunk=on_chunk,
        **kwargs,
    )


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
        from pipeline.engine import PipelineEngine

        input_route_table = provider.get("input_route_table") if provider else None
        output_route_table = provider.get("output_route_table") if provider else None
        plugin_registry = provider.get("plugin_registry") if provider else None

        if not input_route_table or not output_route_table or not plugin_registry:
            logger.warning("[MessageBus] 缺少路由表或插件注册表，无法重建管道: pipeline=%s", pipeline_id)
            return InjectResult(
                success=False,
                error="缺少路由表或插件注册表",
                method="failed",
                pipeline_id=pipeline_id,
            )

        services = provider._services if provider else {}

        new_engine = PipelineEngine(
            input_route_table=input_route_table,
            output_route_table=output_route_table,
            plugin_registry=plugin_registry,
            services=services,
            checkpoint_manager=services.get("checkpoint_manager"),
        )
        new_engine._pipeline_id = pipeline_id

        await new_engine.run(
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

        logger.info("[MessageBus] 管道复活完成: pipeline=%s", pipeline_id)
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
