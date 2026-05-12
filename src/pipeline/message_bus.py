"""管道消息总线 — 统一消息注入入口。

所有"给管道发消息"的操作都通过 send_pipeline_message() 进行，
不再由各调用方自行判断管道状态和选择注入方式。

内部自动判断管道状态并选择最佳注入策略：
1. 运行中引擎 → inject_notification()（下一轮迭代消费）
2. 挂起引擎   → inject_and_wake()（立即唤醒）
3. 无引擎+有历史 → 从历史重建引擎 + run()
4. 无引擎+无历史 → 返回失败
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


@dataclass
class InjectResult:
    """消息注入结果。"""

    success: bool
    method: str = ""
    pipeline_id: str = ""
    error: str = ""


def _find_engine(pipeline_id: str) -> tuple[Any | None, str]:
    """查找目标管道引擎实例。

    按优先级从多个来源查找：
    1. ServiceProvider 中的运行态引擎
    2. 全局挂起引擎注册表
    3. ServiceProvider 中的挂起态引擎

    Args:
        pipeline_id: 目标管道 ID

    Returns:
        (engine_instance, state) 元组，state 为 "running" 或 "suspended"；
        未找到返回 (None, "")
    """
    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
    except Exception:
        provider = None

    running_key = f"__running_engine_{pipeline_id}"
    if provider:
        engine = provider.get(running_key)
        if engine is not None:
            # BUG-FIX-20260511: 引擎挂起时 __running_engine_ 注册未清除，
            # 导致 _find_engine 错误返回 "running"，调用方走普通路径调用
            # engine.run() 而非唤醒路径，重置 _suspended_state 丢失对话历史。
            # 修复: 检查 is_suspended 属性，挂起时返回 "suspended"。
            if getattr(engine, "is_suspended", False):
                return engine, "suspended"
            return engine, "running"

    try:
        from pipeline.engine import get_global_suspended_engine
        engine = get_global_suspended_engine(pipeline_id)
        if engine is not None:
            return engine, "suspended"
    except Exception:
        pass

    suspended_key = f"__suspended_engine_{pipeline_id}"
    if provider:
        engine = provider.get(suspended_key)
        if engine is not None:
            return engine, "suspended"

    return None, ""


def _inject_notification_to_engine(engine: Any, msg: str) -> None:
    """向运行中的管道引擎注入通知消息。

    直接操作引擎的 _pending_notifications 列表（线程安全追加），
    若管道处于挂起状态则顺便唤醒。

    Args:
        engine: PipelineEngine 实例
        msg: 通知消息文本
    """
    if not msg:
        return
    engine._pending_notifications.append(msg)
    logger.info(
        "[MessageBus] 通知已入队 (queue=%d): %.80s",
        len(engine._pending_notifications), msg,
    )
    if engine._wake_event is not None:
        engine._wake_event.set()


def _inject_and_wake_engine(engine: Any, user_input: str) -> None:
    """向挂起的管道引擎注入消息并唤醒。

    将 user_input 注入到引擎的 _suspended_state，
    然后设置 _wake_event 唤醒管道。

    Args:
        engine: PipelineEngine 实例
        user_input: 要注入的消息文本
    """
    if engine._suspended_state is not None and user_input:
        orig = engine._suspended_state.get("user_input", "")
        # BUG-FIX-fix_20260511: 不再将 orig 拼接到用户消息中。
        # orig 是挂起时的内部占位文本，不应暴露给 LLM。
        engine._suspended_state["user_input"] = user_input
        engine._suspended_state.setdefault("messages", []).append(
            {"role": "user", "content": user_input}
        )
        logger.info(
            "[MessageBus] 消息已注入到挂起引擎 state (%d 字符)", len(user_input)
        )
    if engine._wake_event is not None:
        engine._wake_event.set()


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
    **kwargs,
) -> InjectResult:
    """统一消息注入入口 — 所有外部调用方都使用此函数。

    自动判断管道状态并选择最佳注入策略：
    1. 运行中引擎 → inject_notification()（下一轮迭代消费）
    2. 挂起引擎   → inject_and_wake()（立即唤醒）
    3. 无引擎+有历史 → 从历史重建引擎 + run()
    4. 无引擎+无历史 → 返回 InjectResult(success=False)

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

    Returns:
        InjectResult 注入结果
    """
    if not pipeline_id:
        return InjectResult(success=False, error="pipeline_id 不能为空", method="failed")

    if not message:
        return InjectResult(success=False, error="message 不能为空", method="failed")

    engine, state = _find_engine(pipeline_id)

    if state == "running" and engine is not None:
        try:
            _inject_notification_to_engine(engine, message)
            logger.info(
                "[MessageBus] 消息已注入运行中管道 | pipeline=%s | method=notification | preview=%.60s",
                pipeline_id, message,
            )
            return InjectResult(success=True, method="notification", pipeline_id=pipeline_id)
        except Exception as exc:
            logger.warning("[MessageBus] notification 注入失败: %s", exc)

    if state == "suspended" and engine is not None:
        try:
            _inject_and_wake_engine(engine, message)
            logger.info(
                "[MessageBus] 消息已注入挂起管道并唤醒 | pipeline=%s | method=wake | preview=%.60s",
                pipeline_id, message,
            )
            return InjectResult(success=True, method="wake", pipeline_id=pipeline_id)
        except Exception as exc:
            logger.warning("[MessageBus] wake 注入失败: %s", exc)

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

    conversation_history.append({"role": "user", "content": message})

    if agent_config is None and provider and task_id:
        agent_config = _load_agent_config(task_id, provider)

    try:
        from pipeline.engine import PipelineEngine
        from pipeline.route import InputRouteTable, OutputRouteTable
        from pipeline.registry import PluginRegistry

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
            allow_default_fallback=False,
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
