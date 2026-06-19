"""管道复活与生命周期恢复逻辑。

当引擎不在内存时，从持久化存储加载历史记录，
创建新 PipelineEngine 并以消息为 user_input 运行。

同时提供启动时的管道恢复（restore_pipelines_on_startup）。
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 管道引擎独立线程运行器（与 task_executor._run_engine_isolated 同源）
# 避免跨模块循环导入（message_bus ← task_executor ← message_bus）
# REFACTOR-20260614: run_engine_in_thread 已删除。engine.run() 现在作为
# asyncio.Task 在主事件循环中运行，不再需要独立线程/事件循环。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 管道复活
# ---------------------------------------------------------------------------

async def try_revive_pipeline(
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
    **kwargs: Any,
) -> Any:
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
        revive_bridge: 复活用的 bridge 实例

    Returns:
        InjectResult 注入结果
    """
    # 延迟导入避免循环依赖
    from pipeline.message_bus import InjectResult, _find_engine

    engine, _ = _find_engine(pipeline_id)
    if engine is not None:
        logger.warning("[Reviver] 引擎已存在，跳过: pipeline=%s", pipeline_id[:12])
        return InjectResult(success=False, error="引擎已存在", method="failed", pipeline_id=pipeline_id)

    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
    except Exception as exc:
        logger.warning("[Reviver] 获取 ServiceProvider 失败: %s", exc)
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

    # agent 解析链——引擎层只管注册表来源：
    # - 创建者传入（调用方传 agent_config）
    # - 子任务：task.metadata["target_id"]（任务数据，创建者注册时写入）
    # - 兜底：检查点 agent_config_id
    if agent_config is None and provider and task_id:
        agent_config = _load_agent_config(task_id, provider)

    # 第二数据源：从检查点恢复 agent 身份。
    if agent_config is None and provider:
        agent_config = await _load_agent_from_checkpoint(pipeline_id, provider)

    # BUG-FIX-fix_20260609_agent_fallback:
    # 找不到 agent_config 直接返回失败，禁止静默降级。
    if agent_config is None:
        logger.error(
            "[Reviver] 管道复活失败：无法确定 Agent 配置: pipeline=%s",
            pipeline_id[:12],
        )
        return InjectResult(
            success=False,
            error="管道复活失败：缺少 Agent 配置（agent_config、task_id、检查点均无法确定），"
                  "禁止静默回退到默认 Agent",
            method="failed",
            pipeline_id=pipeline_id,
        )

    try:
        from pipeline.registry import get_engine_registry

        _registry = get_engine_registry()
        input_route_table = provider.get("input_route_table") if provider else None
        output_route_table = provider.get("output_route_table") if provider else None
        plugin_registry = provider.get("plugin_registry") if provider else None
        services = provider.get_all_services() if provider else {}

        entry = _registry.revive_pipeline(
            pipeline_id,
            input_route_table=input_route_table,
            output_route_table=output_route_table,
            plugin_registry=plugin_registry,
            services=services,
        )
        if entry is None:
            logger.warning("[Reviver] 管道恢复失败（缺少必要参数）: pipeline=%s", pipeline_id[:12])
            return InjectResult(
                success=False, error="管道恢复失败",
                method="failed", pipeline_id=pipeline_id,
            )

        new_engine = entry.engine

        # REFACTOR: 回主事件循环 — engine.run() 作为协程在主循环运行，
        # 不再创建独立线程/事件循环。死锁已于 2026-06-14 验证解除。
        engine_future = asyncio.ensure_future(new_engine.run(
            user_input=message,
            agent_config=agent_config,
            conversation_history=conversation_history,
            task_id=task_id,
            workspace=workspace,
            project_root="",
            streaming=streaming,
            on_chunk=None,
        ))

        # Phase 1: drain_loop 已删除，engine 主动 emit 推送事件。
        # start_bg_drain 为兼容空实现，保留调用仅为不破坏导入链。
        if revive_bridge is not None:
            from pipeline.drain_manager import start_bg_drain
            start_bg_drain(pipeline_id, revive_bridge, new_engine, engine_task=engine_future)
            _revive_entry = get_engine_registry().get(pipeline_id)
            if _revive_entry:
                _revive_entry.engine_task = engine_future

        logger.info("[Reviver] 管道复活已启动(异步): pipeline=%s", pipeline_id[:12])
        return InjectResult(success=True, method="revive", pipeline_id=pipeline_id)

    except Exception as exc:
        logger.error("[Reviver] 管道复活失败: pipeline=%s, error=%s", pipeline_id, exc)
        return InjectResult(
            success=False, error=f"管道复活失败: {exc}",
            method="failed", pipeline_id=pipeline_id,
        )


# ---------------------------------------------------------------------------
# 启动恢复
# ---------------------------------------------------------------------------

async def restore_pipelines_on_startup() -> int:
    """应用启动时恢复 running/pending 状态的管道。

    Returns:
        恢复的管道数量
    """
    try:
        from infrastructure.service_provider import get_service_provider
        provider = get_service_provider()
    except Exception:
        logger.warning("[Reviver] restore_pipelines_on_startup: ServiceProvider 不可用")
        return 0

    task_service = provider.get("task_service") if provider else None
    if not task_service:
        logger.info("[Reviver] restore_pipelines_on_startup: task_service 不可用，跳过")
        return 0

    restored = 0
    for status in ("running", "pending"):
        try:
            tasks = task_service.list_tasks(status=status) if hasattr(task_service, "list_tasks") else []
        except Exception as exc:
            logger.debug("restore_pipelines list_tasks 失败 (status=%s): %s", status, exc)
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
            services = provider.get_all_services() if provider else {}

            entry = registry.revive_pipeline(
                pipeline_id,
                input_route_table=input_route_table,
                output_route_table=output_route_table,
                plugin_registry=plugin_registry,
                services=services,
                tags={
                    "mode": "interactive",
                    "task_id": task.get("task_id", "") if isinstance(task, dict) else getattr(task, "task_id", ""),
                    "source": "startup_restore",
                },
            )
            if entry:
                restored += 1
                logger.info(
                    "[Reviver] 启动恢复: pipeline=%s status=%s",
                    pipeline_id[:12], status,
                )

    if restored:
        logger.info("[Reviver] restore_pipelines_on_startup 完成: 恢复 %d 个管道", restored)
    return restored


# ---------------------------------------------------------------------------
# 辅助：从存储加载历史和 Agent 配置
# ---------------------------------------------------------------------------

def _load_history_from_storage(
    pipeline_id: str,
    provider: Any | None,
) -> list[dict[str, Any]] | None:
    """从 execution_record_storage 加载管道历史记录。"""
    if not provider:
        return None

    exec_storage = provider.get("execution_record_storage")
    if not exec_storage:
        return None

    try:
        records = exec_storage.list_by_pipeline(pipeline_id)[0]
    except Exception as exc:
        logger.debug("加载管道历史记录失败 (pipeline=%s): %s", pipeline_id[:12], exc)
        return None

    if not records:
        return None

    history: list[dict[str, Any]] = []
    _type_to_role = {"user": "user", "ai": "assistant", "tool": "tool", "system": "system"}
    for r in records:
        role = r.role or _type_to_role.get(r.type, "user")
        msg: dict[str, Any] = {"role": role, "content": r.content}
        if getattr(r, "sequence", 0) > 0:
            msg["_record_sequence"] = r.sequence
        if getattr(r, "name", None):
            msg["name"] = r.name
        if getattr(r, "tool_call_id", None):
            msg["tool_call_id"] = r.tool_call_id
        if getattr(r, "tool_input", None):
            msg["tool_input"] = r.tool_input
        if getattr(r, "tool_calls_json", None):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                msg["tool_calls"] = json.loads(r.tool_calls_json)
        history.append(msg)

    try:
        from infrastructure.task_worker import _reconstruct_tool_calls
        _reconstruct_tool_calls(history)
    except ImportError:
        pass

    logger.info(
        "[Reviver] 从存储恢复 %d 条历史记录: pipeline=%s",
        len(history), pipeline_id,
    )
    return history


def _load_agent_config(task_id: str, provider: Any) -> Any | None:
    """从 task_service 加载任务的 agent_config（子任务管道的数据源）。

    agent_id 来自 task.metadata["target_id"]（任务数据决定）。
    """
    task_service = provider.get("task_service")
    if not task_service:
        return None

    try:
        task_obj = task_service.get_task(task_id)
        if not task_obj:
            return None
        # BUG-FIX-fix_20260619_target_id_attribute:
        # TaskModel 没有 target_id 属性，target_id 存在 metadata 里。
        # 原 getattr(task_obj, "target_id") 永远返回 None，导致子管道 agent 解析失败。
        target_id = None
        if hasattr(task_obj, "metadata") and isinstance(task_obj.metadata, dict):
            target_id = task_obj.metadata.get("target_id")
        if not target_id:
            target_id = getattr(task_obj, "target_id", None)
        if not target_id:
            return None
        from agents.global_registry import get_global_agent_registry_sync
        agent_registry = get_global_agent_registry_sync()
        if agent_registry:
            return agent_registry.get(target_id)
    except Exception as exc:
        logger.warning(
            "[_load_agent_config] 加载 agent_config 失败 (task_id=%s): %s",
            task_id, exc,
        )

    return None


async def _load_agent_from_checkpoint(pipeline_id: str, provider: Any) -> Any | None:
    """从检查点恢复管道的 agent_config（task.target_id 失败时的第二数据源）。

    检查点每轮 auto-save 都会持久化 agent_config_id（pipeline_checkpoint.py 白名单），
    这是管道实际使用的 Agent 身份的可靠记录。当 task.target_id 关联断裂或
    与管道实际 Agent 不一致时，用检查点的 agent_config_id 恢复正确身份。

    Args:
        pipeline_id: 管道 ID
        provider: ServiceProvider 实例

    Returns:
        对应的 AgentConfig 实例，未找到返回 None（由上层 fail-closed）
    """
    checkpoint_manager = provider.get("checkpoint_manager") if provider else None
    if checkpoint_manager is None or not hasattr(checkpoint_manager, "get_latest"):
        return None

    try:
        checkpoint_data = await checkpoint_manager.get_latest(pipeline_id)
    except Exception as exc:
        logger.debug(
            "[_load_agent_from_checkpoint] 读取检查点失败 (pipeline=%s): %s",
            pipeline_id[:12], exc,
        )
        return None

    if not checkpoint_data:
        return None

    saved_state = checkpoint_data.get("state", {})
    agent_config_id = saved_state.get("agent_config_id")
    if not agent_config_id:
        return None

    agent_registry = provider.get("agent_registry") if provider else None
    if agent_registry is None:
        return None

    agent_config = agent_registry.get(agent_config_id)
    if agent_config is not None:
        logger.info(
            "[_load_agent_from_checkpoint] 从检查点恢复 Agent: pipeline=%s agent_config_id=%s",
            pipeline_id[:12], agent_config_id,
        )
    return agent_config
