"""流式响应处理模块。

包含管道引擎上下文管理、流式引擎响应和挂起唤醒响应逻辑。

从 start_server.py 拆分而来，保持向后兼容。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 将 src 目录加入 sys.path，确保模块可被正确导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from fastapi import WebSocket

from channels.api.memory_store import store as api_store
from src.pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

from ws_handler import ws_interaction_notifier

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent

class PipelineContext:
    """管道引擎上下文，按 pipeline_id 管理独立的引擎实例。

    由 ``_init_pipeline_context()`` 创建，在 WebSocket 处理中通过
    ``_pipeline_ctx`` 全局变量访问。

    每调用 ``get_or_create_engine(pipeline_id)`` 时，若该 pipeline_id
    尚无引擎实例，则基于共享的 pipeline_config / plugin_registry / services
    创建一个新的 PipelineEngine，确保管道之间状态完全隔离。

    Attributes:
        engine: 默认 PipelineEngine 实例（向后兼容）
        agent_config: 默认 Agent 配置（灵汐）
        services: 共享服务字典
        available: 是否成功初始化
        pipeline_config: 管道配置（用于创建新引擎）
        plugin_registry: 插件注册表（用于创建新引擎）
    """

    def __init__(
        self,
        engine: Any | None = None,
        agent_config: Any | None = None,
        services: dict[str, Any] | None = None,
        available: bool = False,
        pipeline_config: Any | None = None,
        plugin_registry: Any | None = None,
        app: Any | None = None,
    ) -> None:
        """初始化管道上下文。

        Args:
            engine: 默认 PipelineEngine 实例
            agent_config: Agent 配置
            services: 共享服务字典
            available: 是否成功初始化
            pipeline_config: 管道配置（用于创建新引擎）
            plugin_registry: 插件注册表（用于创建新引擎）
            app: Application 实例（用于创建新引擎）
        """
        self.engine = engine
        self.agent_config = agent_config
        self.services = services or {}
        self.available = available
        self.pipeline_config = pipeline_config
        self.plugin_registry = plugin_registry
        self.app = app
        self._engines: dict[str, Any] = {}
        # BUG-FIX-fix_20260523_engine_memory_leak:
        # 记录每个引擎的最后活跃时间，供 cleanup_idle_engines 清理使用。
        self._engine_last_active: dict[str, float] = {}
        if engine is not None:
            engine._pipeline_id = ""
            self.engine = engine

    def get_or_create_engine(self, pipeline_id: str) -> Any:
        """获取或创建指定 pipeline_id 的独立引擎实例。

        每个 pipeline_id 对应一个独立的 PipelineEngine 实例，
        确保管道之间状态完全隔离，通知不会串线。
        """
        if pipeline_id in self._engines:
            # BUG-FIX-fix_20260523_engine_memory_leak: 更新最后活跃时间
            self._engine_last_active[pipeline_id] = time.monotonic()
            return self._engines[pipeline_id]
        new_engine = self.app.create_pipeline_engine(
            self.pipeline_config,
            self.plugin_registry,
            self.services,
        )
        new_engine._pipeline_id = pipeline_id
        self._engines[pipeline_id] = new_engine
        self._engine_last_active[pipeline_id] = time.monotonic()
        return new_engine

    def cleanup_engine(self, pipeline_id: str) -> bool:
        """清理指定 pipeline_id 的引擎实例，释放资源。

        BUG-FIX-fix_20260523_engine_memory_leak:
        问题根因: _engines 字典只增不减，长时间运行后引擎实例累积导致内存泄漏。
        修复方案: 提供显式清理方法，在引擎完成或管道挂起超时后调用。

        Args:
            pipeline_id: 要清理的管道 ID

        Returns:
            是否成功清理（True 表示该引擎存在并已被移除）
        """
        engine = self._engines.pop(pipeline_id, None)
        self._engine_last_active.pop(pipeline_id, None)
        if engine is not None:
            logger.info(
                "cleanup_engine: 已清理引擎 pipeline=%s, 剩余引擎数=%d",
                pipeline_id[:12], len(self._engines),
            )
            return True
        return False

    def cleanup_idle_engines(self, max_age_seconds: float = 3600) -> int:
        """清理所有超过指定时间未活跃的引擎实例。

        BUG-FIX-fix_20260523_engine_memory_leak:
        遍历所有引擎，清理超过 max_age_seconds 未被 get_or_create_engine 访问的实例。

        Args:
            max_age_seconds: 最大空闲秒数，默认 3600（1小时）

        Returns:
            清理的引擎数量
        """
        now = time.monotonic()
        to_remove = [
            pid for pid, last_active in self._engine_last_active.items()
            if (now - last_active) > max_age_seconds
        ]
        for pid in to_remove:
            self._engines.pop(pid, None)
            self._engine_last_active.pop(pid, None)
        if to_remove:
            logger.info(
                "cleanup_idle_engines: 清理 %d 个空闲引擎（阈值=%.0fs）, 剩余=%d",
                len(to_remove), max_age_seconds, len(self._engines),
            )
        return len(to_remove)


# 全局管道上下文（延迟初始化）
# Module-level var (_task_worker set by _init_pipeline_context)
_task_worker = None
_cached_call_timeout: int | None = None




def _init_pipeline_context() -> PipelineContext:
    """初始化管道引擎上下文。

    按照以下步骤组装管道：
    1. 加载管道配置（config/pipelines/default.yaml）
    2. 构建插件注册表
    3. 加载 Agent 配置（config/agents/）
    4. 构建共享服务字典
    5. 创建 PipelineEngine 实例

    如果任何步骤失败，返回 available=False 的上下文，
    WebSocket 处理将回退到模拟回复模式。

    Returns:
        PipelineContext 实例
    """
    try:
        from config.models import ModelConfigLoader
        from pipeline.config import build_plugin_registry, load_pipeline_config

        # 确定管道配置路径
        config_path = _PROJECT_ROOT / "config" / "pipelines" / "default.yaml"
        if not config_path.exists():
            # 回退到 src/ 下的 config/pipelines/
            fallback = _PROJECT_ROOT / "src" / "config" / "pipelines" / "default.yaml"
            if fallback.exists():
                config_path = fallback
            else:
                logger.error("管道配置文件不存在: %s", config_path)
                return PipelineContext(available=False)

        logger.info("加载管道配置: %s", config_path)

        from application import Application
        model_loader = ModelConfigLoader()

        # 加载管道配置
        pipeline_config = load_pipeline_config(config_path, model_loader=model_loader)

        # 构建插件注册表
        plugin_registry = build_plugin_registry(pipeline_config, model_loader=model_loader)

        # 加载 Agent 配置
        from agents.registry import AgentRegistry
        agent_registry = AgentRegistry()
        agent_config_dir = _PROJECT_ROOT / "config" / "agents"
        if agent_config_dir.exists():
            agent_registry.load_directory(agent_config_dir)

        # 构建共享服务（通过 Application 容器）
        _app = Application(project_root=_PROJECT_ROOT)
        services = _app.build_services(agent_registry=agent_registry)

        # 如果 ToolCore 存在，注册工具
        tool_core = plugin_registry.get_core("tool_execute")
        if tool_core is not None:
            tool_registry = services.get("tool_registry")
            if tool_registry is not None:
                try:
                    from tools.builtin import register_core_tools
                    registered = register_core_tools(tool_registry, session=None)
                    logger.info("ToolCore 注册了 %d 个核心工具", len(registered))
                except Exception as exc:
                    logger.warning("register_core_tools 失败: %s", exc)
                tool_core.register_tools_from_registry(tool_registry)

        # 获取默认 Agent 配置（灵汐）
        agent_config = None
        for candidate in ["default", "lingxi"]:
            agent_config = agent_registry.get(candidate)
            if agent_config:
                break

        if agent_config:
            logger.info(
                "Agent 配置已加载: %s (%s)",
                agent_config.config_id,
                agent_config.display_name,
            )
        else:
            logger.warning("未找到默认 Agent 配置，将使用原始 LLM 调用")

        # 创建管道引擎（通过 Application 容器）
        engine = _app.create_pipeline_engine(pipeline_config, plugin_registry)

        # 注册路由表和插件注册表到 ServiceProvider，供 MessageBus 重建管道使用
        try:
            from infrastructure.service_provider import get_service_provider
            _sp = get_service_provider()
            _sp.register("input_route_table", pipeline_config.input_route_table)
            _sp.register("output_route_table", pipeline_config.output_route_table)
            _sp.register("plugin_registry", plugin_registry)
            logger.info("路由表和插件注册表已注册到 ServiceProvider")
        except Exception as exc:
            logger.warning("注册路由表到 ServiceProvider 失败: %s", exc)

        # 初始化 TaskWorker（通过 Application 容器）
        try:
            _task_worker = _app.create_task_worker(pipeline_config, plugin_registry)
            if _task_worker is not None:
                # 存储为模块全局变量，供 WebSocket handler 启动
                globals()["_task_worker"] = _task_worker
                # create_pipeline_factory 内部已注册到 ServiceProvider，无需 sys 全局变量
                _app.create_pipeline_factory(
                    pipeline_config, plugin_registry,
                )
                logger.info("TaskWorker 初始化完成（将在首次请求时启动）")
            else:
                logger.warning("TaskWorker 创建返回 None，详见上方 application 日志")
        except Exception as exc:
            logger.warning("TaskWorker 初始化失败: %s", exc)

        # 注册 WebSocket 交互通知器到 HumanInteractionService
        try:
            from human_interaction import get_human_interaction_service
            # 导入 desktop_notifier — 触发 install_hook()，接入 OS 桌面通知（含提示音）
            try:
                import human_interaction.desktop_notifier  # noqa: F401
            except Exception:
                pass
            human_svc = get_human_interaction_service()
            ws_interaction_notifier.set_service(human_svc)
            human_svc.set_notifier(ws_interaction_notifier)
            services["ws_interaction_notifier"] = ws_interaction_notifier
            logger.info("WebSocketInteractionNotifier 已注册到 HumanInteractionService 和 services")
        except Exception as exc:
            logger.warning("注册 WebSocket 交互通知器失败: %s", exc)

        return PipelineContext(
            engine=engine,
            agent_config=agent_config,
            services=services,
            available=True,
            pipeline_config=pipeline_config,
            plugin_registry=plugin_registry,
            app=_app,
        )

    except Exception as exc:
        logger.warning("管道引擎初始化失败，将回退到模拟回复模式: %s", exc, exc_info=True)
        return PipelineContext(available=False)



# ---------------------------------------------------------------------------
# 流式响应辅助函数
# ---------------------------------------------------------------------------


def _get_call_timeout() -> int:
    """从 llm.yaml defaults.call_timeout 读取超时秒数，默认 120 秒。"""
    global _cached_call_timeout
    if _cached_call_timeout is not None:
        return _cached_call_timeout
    try:
        from config.models import ModelConfigLoader
        loader = ModelConfigLoader()
        defaults = loader._load_llm_data().get("defaults", {})
        _cached_call_timeout = int(defaults.get("call_timeout", 120))
    except Exception:
        _cached_call_timeout = 120
    return _cached_call_timeout


# _route_to_sub_pipeline 已移除，子管道路由统一使用 pipeline.message_bus.send_pipeline_message


# ---------------------------------------------------------------------------
# 流式响应共享辅助函数
# ---------------------------------------------------------------------------

_VALID_ROLES = {"user", "assistant", "tool"}


def _register_pipeline_thread(pipeline_id: str, engine: Any, thread_id: str) -> None:
    """注册 pipeline_id 到 thread_id 的映射到 EngineRegistry。

    如果 pipeline_id 已注册，更新其 thread_id；
    否则，新建注册条目。
    """
    from pipeline.registry import get_engine_registry
    _registry = get_engine_registry()
    _entry = _registry.get(pipeline_id)
    if _entry:
        _entry.thread_id = thread_id
    else:
        _registry.register(pipeline_id, engine, thread_id=thread_id)


def _sync_conversation_history(
    conversation_history: list[dict[str, Any]],
    messages: list[dict] | None,
    fallback_content: str = "",
    fallback_id: str = "",
) -> None:
    """从引擎消息列表同步外部 conversation_history。

    如果 messages 非空，过滤有效角色（user/assistant/tool）后替换
    conversation_history；否则，如果有 fallback_content，追加一条
    assistant 消息。
    """
    if messages:
        filtered = [
            msg for msg in messages
            if isinstance(msg, dict) and msg.get("role") in _VALID_ROLES
        ]
        conversation_history.clear()
        conversation_history.extend(filtered)
    elif fallback_content:
        conversation_history.append({
            "role": "assistant",
            "content": fallback_content,
            "id": fallback_id,
        })


async def _cancel_engine_task(engine_task: asyncio.Task) -> None:
    """取消引擎任务并等待其退出。

    安全地取消 asyncio.Task，捕获 CancelledError 和其他异常。
    """
    engine_task.cancel()
    try:
        await engine_task
    except (asyncio.CancelledError, Exception):
        pass


async def _run_drain_and_finalize(
    bridge: Any,
    engine_task: asyncio.Task,
    conversation_history: list[dict[str, Any]],
    message_id: str,
    pipeline_id: str,
    call_timeout: int,
    suspend_check: Any = None,
    heartbeat_callback: Any = None,
) -> dict:
    """运行 drain_loop 并完成结果提取、历史同步、new_message 发送。

    封装了管道引擎的 drain_loop 调用、超时处理、内容提取、
    conversation_history 同步和最终消息发送的完整流程。

    此函数从 _stream_engine_response 中提取，覆盖以下逻辑：
    - drain_loop 调用及超时处理（asyncio.TimeoutError + drain_result.timed_out）
    - 引擎结果提取（engine_task.result()）
    - 内容提取优先级：drain_accumulated > final_messages assistant > raw_result
    - conversation_history 同步（使用 _sync_conversation_history）
    - new_message 发送（bridge.send_new_message）

    Args:
        bridge: PipelineStreamBridge 实例
        engine_task: 引擎异步任务
        conversation_history: 对话历史列表（会被就地更新）
        message_id: 消息 UUID
        pipeline_id: 管道 ID
        call_timeout: 调用超时秒数
        suspend_check: 挂起检查回调
        heartbeat_callback: 心跳回调

    Returns:
        包含 actual_content、result、drain_result 的字典。

    Raises:
        TimeoutError: 当引擎执行超时时
    """
    engine_timed_out = False

    try:
        drain_result = await asyncio.wait_for(
            bridge.drain_loop(
                engine_task,
                heartbeat_callback=heartbeat_callback,
                heartbeat_interval=5.0,
                suspend_check=suspend_check,
                call_timeout=call_timeout,
            ),
            # BUG-FIX-fix_20260523_timeout_too_large:
            # 问题根因: _call_timeout * 50 默认120s时为6000s（100分钟），过长。
            # 修复方案: 改为 _call_timeout * 10（默认20分钟），仍留有足够余量。
            timeout=call_timeout * 10,
        )
        if drain_result.get("timed_out"):
            engine_timed_out = True
            logger.error("LLM 活动超时 (%ds): pipeline=%s", call_timeout, message_id)
            await _cancel_engine_task(engine_task)
    except asyncio.TimeoutError:
        engine_timed_out = True
        logger.error(
            "管道引擎执行超时 (%ds)，取消任务: message_id=%s",
            call_timeout, message_id,
        )
        await _cancel_engine_task(engine_task)

    if engine_timed_out:
        raise TimeoutError(f"LLM 调用超时（{call_timeout}s），请稍后重试")

    # 获取引擎结果
    result = {}
    if engine_task.done():
        result = engine_task.result()
    else:
        logger.info(
            "管道未完成（可能已挂起），使用 drain_result: pipeline=%s",
            str(pipeline_id)[:12] if pipeline_id else "?",
        )

    # BUG-FIX-20260515: 内容提取优先级调整
    # 问题根因: 原先优先使用 final_messages 中的 assistant 消息，
    #   但引擎返回的 assistant 内容可能为空（引擎未正确填充），
    #   而流式累积的 drain_result['accumulated_content'] 有内容。
    #   由于 elif 链，actual_content 变成空字符串。
    #   然后 drain_loop 已发送 stream_end，又补发 stream_chunk，
    #   打破了事件序列（stream_end 后不应再有 stream_chunk），
    #   导致前端抖动、消息变空。
    # 修复方案:
    #   1. 优先使用流式累积内容（用户实际看到的就是流式内容）
    #   2. 移除 stream_end 之后的 stream_chunk 补发（改为 stream_end 前处理）
    #   3. send_new_message 内部增加空内容保底
    final_messages = result.get("messages", [])
    drain_accumulated = drain_result.get("accumulated_content", "")
    actual_content = ""

    if drain_accumulated:
        # 优先使用流式累积内容——这是用户实际看到的
        actual_content = drain_accumulated
        # 同步 conversation_history
        _sync_conversation_history(conversation_history, final_messages)
        if not final_messages:
            # 引擎挂起时 final_messages 为空，追加 assistant 回复
            conversation_history.append({
                "role": "assistant",
                "content": actual_content,
                "id": message_id,
            })
    elif final_messages:
        # 无流式累积时，从引擎 messages 提取
        for msg in reversed(final_messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                actual_content = msg.get("content", "")
                break
        _sync_conversation_history(conversation_history, final_messages)
    else:
        actual_content = result.get("raw_result", "")

    # ---- new_message 最终消息（通过 bridge 发送）----
    # BUG-FIX-20260515: 移除了 stream_end 之后的 stream_chunk 补发
    # 问题根因: drain_loop 已发送 stream_end，在 stream_end 之后补发
    #   stream_chunk 打破了事件序列（应为 stream_start→chunks→stream_end→new_message），
    #   前端收到 stream_end 后开始渲染最终内容，又收到 stream_chunk 重新渲染，
    #   导致抖动。同时 new_message 的内容与 stream_chunk 累积不一致时会导致消息变空。
    # 修复: send_new_message 内部有空内容保底（使用 _accumulated_content），
    #   不再需要在 stream_end 后补发 stream_chunk。
    await bridge.send_new_message(actual_content, sequence=1)

    return {"actual_content": actual_content, "result": result, "drain_result": drain_result}



# ---------------------------------------------------------------------------
# 流式响应主函数
# ---------------------------------------------------------------------------


async def _stream_wake_response(
    websocket: WebSocket,
    user_content: str,
    message_id: str,
    stop_event: asyncio.Event,
    engine: Any,
    pipeline_id: str,
    thread_id: str = "",
    ws_notifier: Any = None,
    conversation_history: list[dict[str, Any]] | None = None,
    pre_created_bridge: Any = None,
) -> None:
    """管道挂起唤醒后的流式响应。

    管道被 send_pipeline_message 唤醒后，需要新的流式桥接来捕获
    后续的 LLM 输出并发送到前端。本函数使用预创建的 PipelineStreamBridge，
    其 on_chunk 已在唤醒引擎之前注入到引擎的 _saved_on_chunk。

    Args:
        websocket: WebSocket 连接实例
        user_content: 用户消息文本
        message_id: 本轮回复的消息 UUID
        stop_event: 取消事件
        engine: 已唤醒的 PipelineEngine 实例
        pipeline_id: 管道 ID
        thread_id: WebSocket 连接的 thread_id
        ws_notifier: WebSocketInteractionNotifier 实例
        conversation_history: 对话历史列表（会被就地更新），用于追加唤醒轮的 assistant 回复
        pre_created_bridge: 在唤醒引擎之前预创建的 StreamBridge 实例
    """
    from pipeline.engine import _current_pipeline_id
    _current_pipeline_id.set(pipeline_id)
    logger.info(
        "[wake_response] 开始: pipeline=%s thread_id=%s msg=%s pre_bridge=%s bridge_pid=%s",
        pipeline_id[:12], (thread_id or "")[:12], message_id[:12],
        "yes" if pre_created_bridge else "no",
        pre_created_bridge.pipeline_id[:12] if pre_created_bridge else "n/a",
    )

    # 注册 pipeline_id → thread_id 映射到 EngineRegistry
    _register_pipeline_thread(pipeline_id, engine, thread_id)

    if pre_created_bridge is not None:
        bridge = pre_created_bridge
        bridge.pipeline_id = pipeline_id
        if ws_notifier is not None:
            bridge.output_sink = TargetedSink(ws_notifier, thread_id)
    else:
        bridge = PipelineStreamBridge(
            pipeline_id=pipeline_id,
            output_sink=TargetedSink(ws_notifier, thread_id),
            message_id=message_id,
        )
        engine.set_streaming_context(bridge.on_chunk, streaming=True)
        if engine._suspended_state is not None:
            engine._suspended_state["on_chunk"] = bridge.on_chunk
            engine._suspended_state["streaming"] = True

    _call_timeout = _get_call_timeout()
    _long_wait = asyncio.create_task(asyncio.sleep(86400))

    try:
        # SIMPLIFY-fix_20260521: 移除了等待循环。
        # drain_loop 已不再在引擎挂起时 break，因此无需在此等待引擎恢复。
        logger.info(
            "[wake_response] drain_loop 开始: pipeline=%s msg=%s is_suspended=%s queue_size=%d",
            pipeline_id[:12], message_id[:12],
            getattr(engine, "is_suspended", "?"),
            bridge._queue.qsize(),
        )
        drain_result = await asyncio.wait_for(
            bridge.drain_loop(
                _long_wait,
                heartbeat_interval=5.0,
                suspend_check=lambda: getattr(engine, "is_suspended", False),
                call_timeout=_call_timeout,
            ),
            # BUG-FIX-fix_20260523_timeout_too_large:
            # 问题根因: _call_timeout * 50 默认120s时为6000s（100分钟），过长。
            # 修复方案: 改为 _call_timeout * 10（默认20分钟），仍留有足够余量。
            timeout=_call_timeout * 10,
        )

        full_content = drain_result.get("accumulated_content", "")
        logger.info(
            "[wake_response] drain_loop 完成: pipeline=%s content=%d chars timed_out=%s",
            pipeline_id[:12], len(full_content), drain_result.get("timed_out"),
        )

        # BUG-FIX-fix_20260523_double_stream_end:
        # 问题根因: drain_loop 内部已发送 stream_end（正常退出第500行、超时退出第444行），
        #   _stream_wake_response 不需要再额外发送，否则前端收到两个 stream_end。
        # 修复方案: 删除此处重复的 stream_end 发送，仅保留 new_message。
        # 注意: _close_thinking_if_active 也已由 drain_loop 内部第498行调用。

        # BUG-FIX-20260515: 发送 new_message 更新前端
        # 始终通过 bridge.send_new_message 发送，
        # 该方法内部有空内容保底逻辑（使用 _accumulated_content）。
        await bridge.send_new_message(full_content, sequence=1)

        # BUG-FIX-20260511: 唤醒路径需要从引擎内部状态同步 conversation_history
        # 问题根因: 引擎内部维护 state["messages"]（含完整对话历史），
        # 但 _stream_wake_response 完成后 conversation_history（外部变量）
        # 没有同步，导致下一轮消息传给引擎时历史不完整。
        # 修复方案: 从引擎的 _suspended_state 或当前运行状态获取完整 messages，
        # 过滤掉内部 system 消息后同步到 conversation_history。
        if conversation_history is not None:
            engine_messages = None
            if getattr(engine, "_suspended_state", None) is not None:
                engine_messages = engine._suspended_state.get("messages")
            if engine_messages is None and hasattr(engine, "_state"):
                engine_messages = getattr(engine, "_state", {}).get("messages")
            if engine_messages:
                _sync_conversation_history(conversation_history, engine_messages)
                logger.info(
                    "唤醒路径同步 conversation_history: 从引擎同步 %d 条消息 "
                    "(原始 %d 条), history_len=%d",
                    len([
                        m for m in engine_messages
                        if isinstance(m, dict) and m.get("role") in _VALID_ROLES
                    ]),
                    len(engine_messages), len(conversation_history),
                )
            elif full_content:
                _sync_conversation_history(
                    conversation_history, [],
                    fallback_content=full_content, fallback_id=message_id,
                )
                logger.info(
                    "唤醒路径追加 assistant 消息: history_len=%d, content_len=%d",
                    len(conversation_history), len(full_content),
                )

    except Exception as exc:
        logger.error("唤醒流式响应失败: %s", exc)
        try:
            await bridge._send_event({
                "type": "stream_end",
                "data": {
                    "message_id": message_id,
                    "full_content": "",
                    "pipeline_id": bridge.pipeline_id,
                    "error": str(exc),
                },
            })
        except Exception:
            pass
    finally:
        _long_wait.cancel()



async def _stream_engine_response(
    websocket: WebSocket,
    user_content: str,
    message_id: str,
    stop_event: asyncio.Event,
    thread_id: str,
    conversation_history: list[dict[str, Any]],
    ctx: PipelineContext,
    ws_notifier: Any = None,
    pre_created_bridge: Any = None,
) -> None:
    """通过管道引擎获取 AI 回复并以流式方式发送到 WebSocket。

    使用 PipelineStreamBridge 桥接引擎回调与前端 WebSocket 协议，
    将同步 on_chunk 事件转换为异步流式消息发送。

    流程：
    1. 创建 PipelineStreamBridge，桥接引擎回调与 WebSocket 输出
    2. 调用 engine.run() 执行管道，通过 bridge.on_chunk 接收事件
    3. 通过 _run_drain_and_finalize 消费事件队列、提取结果并同步历史
    4. 处理取消和异常情况

    Args:
        websocket: WebSocket 连接实例
        user_content: 用户发送的原始文本
        message_id: 本轮回复的消息 UUID
        stop_event: 用于取消流式生成的事件对象
        thread_id: 当前线程/会话 ID
        conversation_history: 对话历史列表（会被就地更新）
        ctx: 管道上下文
        ws_notifier: WebSocketInteractionNotifier 实例
        pre_created_bridge: 预创建的 StreamBridge 实例
    """
    engine_task: asyncio.Task | None = None

    conversation_history.append({"role": "user", "content": user_content})

    # 确定 pipeline_id：
    # - 如果 pre_created_bridge 已指定 pipeline_id（调用方明确路由），优先使用
    # - 否则沿用 session 已有的（创建会话时分配），或用 Engine 自身的
    session = api_store.get_session(thread_id)
    if pre_created_bridge is not None and pre_created_bridge.pipeline_id:
        pipeline_id = pre_created_bridge.pipeline_id
    else:
        pipeline_id = session.active_pipeline_id if session and session.active_pipeline_id else ctx.engine.pipeline_id

    # BUG-FIX-fix_20260513_pipeline_cross_talk:
    # 每个 pipeline_id 对应独立的引擎实例，确保管道之间状态完全隔离。
    engine = ctx.get_or_create_engine(pipeline_id)

    # 用统一的 pipeline_id 创建或更新 Bridge
    if pre_created_bridge is not None:
        bridge = pre_created_bridge
        bridge.pipeline_id = pipeline_id
    else:
        bridge = PipelineStreamBridge(
            pipeline_id=pipeline_id,
            output_sink=TargetedSink(ws_notifier, thread_id),
            message_id=message_id,
        )

    if session:
        session.register_pipeline(pipeline_id)
        api_store.set_session(thread_id, session)

    # 注册 pipeline_id → thread_id 映射到 EngineRegistry
    _register_pipeline_thread(pipeline_id, engine, thread_id)

    if ctx.services:
        _exec_storage = ctx.services.get("execution_record_storage")
        if _exec_storage is not None:
            try:
                _exec_storage.update_summary(pipeline_id, {"thread_id": thread_id})
            except Exception as _exc:
                logger.warning("写入管道 thread_id 失败: %s", _exc)

    # BUG-FIX-fix_20260516_direct_pipeline_agent_routing:
    # 问题根因: 子管道（sub pipeline）发送消息时，engine.run() 始终使用
    #   ctx.agent_config（主管道的主agent），完全忽略 session 的 agent_id
    #   （子管道关联的agent），导致消息被路由到错误的agent。
    # 修复方案: 优先从 session.agent_id 解析 agent_config，
    #   找不到时回退到 ctx.agent_config。
    _resolved_agent_config = ctx.agent_config
    if session and getattr(session, "agent_id", None):
        try:
            _agent_registry = ctx.services.get("agent_registry") if ctx.services else None
            if _agent_registry:
                _direct_agent_config = _agent_registry.get(session.agent_id)
                if _direct_agent_config:
                    _resolved_agent_config = _direct_agent_config
                    logger.info(
                        "子管道使用 session 指定的 agent: agent_id=%s, pipeline=%s",
                        session.agent_id, pipeline_id[:12],
                    )
        except Exception:
            pass

    try:
        # 启动管道引擎（异步），通过 bridge.on_chunk 接收流式事件
        engine_task = asyncio.create_task(
            engine.run(
                user_input=user_content,
                agent_config=_resolved_agent_config,
                conversation_history=conversation_history[:-1],
                streaming=True,
                on_chunk=bridge.on_chunk,
                auto_approve=True,
                interaction_mode="auto",
            )
        )

        # 心跳回调：通过 WebSocket 发送心跳保活消息
        async def _heartbeat():
            """发送心跳确认消息，防止前端连接超时。"""
            try:
                await asyncio.wait_for(
                    ws_notifier.send_to_thread(
                        thread_id,
                        {"type": "heartbeat_ack", "data": {"server_time": datetime.now(timezone.utc).isoformat()}},
                    ),
                    timeout=3.0,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        # 通过 _run_drain_and_finalize 消费事件队列、提取结果并同步历史
        _call_timeout = _get_call_timeout()
        await _run_drain_and_finalize(
            bridge=bridge,
            engine_task=engine_task,
            conversation_history=conversation_history,
            message_id=message_id,
            pipeline_id=pipeline_id,
            call_timeout=_call_timeout,
            suspend_check=lambda: getattr(engine, "is_suspended", False),
            heartbeat_callback=_heartbeat,
        )

        # 检查是否被中断
        if stop_event.is_set():
            logger.info("流式生成被用户中断: message_id=%s", message_id)

    except asyncio.CancelledError:
        """流式任务被取消（用户中断或连接断开）。"""
        logger.info("流式任务被取消: message_id=%s", message_id)
        bridge.stop()
        # BUG-FIX-fix_engine_task_orphan:
        # 问题根因: 取消 _stream_engine_response 时，内部的 engine_task 不会被自动取消。
        #   导致旧引擎任务仍在共享的 ctx.engine 上运行，新消息启动的新 engine.run() 与之并发，
        #   引擎内部状态（_pipeline_id、_suspended_state 等）被污染，表现为"消息延迟一轮"。
        # 修复方案: 在 CancelledError 中显式取消 engine_task 并等待其退出。
        # 影响范围: 用户快速连续发送消息、发送新消息取消旧回复的场景。
        # 修复日期: 2026-05-08
        if engine_task is not None and not engine_task.done():
            await _cancel_engine_task(engine_task)
        # drain_loop 已发送 stream_end，此处不再重复发送
        raise

    except TimeoutError:
        """引擎执行超时，由 _run_drain_and_finalize 抛出。"""
        raise

    except Exception as exc:
        """管道引擎执行出错。"""
        # BUG-FIX-stream_error-on-exception:
        # 问题: drain_loop 完成后，后续代码（如 send_new_message）抛异常时，
        #   前端不会收到 stream_error 事件，导致流式状态可能无法正确结束。
        # 修复: 在 send_new_message 之前先发送 stream_error 事件，确保前端
        #   知道管道已出错，能正确清理 streamingTabs 中的残留状态。
        # 修复日期: 2026-05-13
        logger.error("管道引擎执行失败: %s", exc, exc_info=True)
        if engine_task is not None and not engine_task.done():
            await _cancel_engine_task(engine_task)
        # 先发送 stream_error 事件，通知前端管道出错
        try:
            await bridge._send_event({
                "type": "stream_error",
                "data": {
                    "message_id": message_id,
                    "pipeline_id": bridge.pipeline_id,
                    "error": str(exc),
                },
            })
        except Exception:
            pass
        # 再发送错误消息作为最终回复
        try:
            error_content = f"抱歉，处理你的消息时出现错误：{exc}"
            await bridge.send_new_message(error_content, sequence=1)
        except Exception:
            pass
