"""流式响应处理模块。

包含管道引擎上下文管理、流式引擎响应和挂起唤醒响应逻辑。

从 start_server.py 拆分而来，保持向后兼容。
"""

from __future__ import annotations

import asyncio
import contextlib
import json  # noqa: F401
import logging
import os  # noqa: F401
import sys  # noqa: F401
import uuid  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path
from typing import Any

# PYTHONPATH 已在 Dockerfile/环境变量中设置为 /app/src，无需 sys.path.insert
from fastapi import WebSocket  # noqa: F401

from channels.api.memory_store import store as api_store  # noqa: F401
from channels.websocket.ws_handler import ws_interaction_notifier
from pipeline.stream_bridge import PipelineStreamBridge, TargetedSink  # noqa: F401

# 日志配置由统一入口 src.core.logging.setup_logging() 负责（在 app_factory.py 中调用）
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class PipelineContext:
    """管道引擎上下文（启动期共享配置容器）。

    由 ``_init_pipeline_context()`` 创建，在 WebSocket 处理中通过
    ``_pipeline_ctx`` 全局变量访问。持有启动期共享的 pipeline_config /
    plugin_registry / services，供初始化使用。

    引擎实例的生命周期由 EngineRegistry 统一管理（I1），本类不再缓存引擎。

    Attributes:
        engine: 默认 PipelineEngine 实例（启动期，向后兼容）
        agent_config: 默认 Agent 配置
        services: 共享服务字典
        available: 是否成功初始化
        pipeline_config: 管道配置
        plugin_registry: 插件注册表
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
        if engine is not None:
            engine._pipeline_id = ""
            self.engine = engine
        # _engines 第二套注册表缓存已删除（遗留死代码，无外部调用方）。
        # 引擎生命唯一由 EngineRegistry 管理（I1）。


# 全局管道上下文（延迟初始化）
# Module-level var (_task_worker set by _init_pipeline_context)
_task_worker = None
_cached_call_timeout: int | None = None


def _init_pipeline_context() -> PipelineContext:  # noqa: PLR0912,PLR0915
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
        from config.models import ModelConfigLoader  # noqa: PLC0415
        from pipeline.config import build_plugin_registry, load_pipeline_config  # noqa: PLC0415

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

        from application import Application  # noqa: PLC0415

        model_loader = ModelConfigLoader()

        # 加载管道配置
        pipeline_config = load_pipeline_config(config_path, model_loader=model_loader)

        # 构建插件注册表
        plugin_registry = build_plugin_registry(pipeline_config, model_loader=model_loader)

        # 加载 Agent 配置
        from agents.registry import AgentRegistry  # noqa: PLC0415

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
                    from tools.builtin import register_core_tools  # noqa: PLC0415

                    registered = register_core_tools(tool_registry, session=None)
                    logger.info("ToolCore 注册了 %d 个核心工具", len(registered))
                except Exception as exc:
                    logger.warning("register_core_tools 失败: %s", exc)
                tool_core.register_tools_from_registry(tool_registry)
            # Docker 容器隔离通过 IsolationManager 统一管理

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
            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415

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
                    pipeline_config,
                    plugin_registry,
                )
                logger.info("TaskWorker 初始化完成（将在首次请求时启动）")
            else:
                logger.warning("TaskWorker 创建返回 None，详见上方 application 日志")
        except Exception as exc:
            logger.warning("TaskWorker 初始化失败: %s", exc)

        # 注册 WebSocket 交互通知器到 HumanInteractionService
        try:
            from human_interaction import get_human_interaction_service  # noqa: PLC0415

            # 导入 desktop_notifier — 触发 install_hook()，接入 OS 桌面通知（含提示音）
            try:  # noqa: SIM105
                import human_interaction.desktop_notifier  # noqa: F401,PLC0415
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
    global _cached_call_timeout  # noqa: PLW0603
    if _cached_call_timeout is not None:
        return _cached_call_timeout
    try:
        from config.models import ModelConfigLoader  # noqa: PLC0415

        loader = ModelConfigLoader()
        defaults = loader._load_llm_data().get("defaults", {})
        _cached_call_timeout = int(defaults.get("call_timeout", 120))
    except Exception:
        _cached_call_timeout = 120
    return _cached_call_timeout


# ---------------------------------------------------------------------------
# 流式响应共享辅助函数
# ---------------------------------------------------------------------------

_VALID_ROLES = {"user", "assistant", "tool"}


def _register_pipeline_thread(pipeline_id: str, engine: Any, thread_id: str) -> None:
    """注册 pipeline_id 到 thread_id 的映射到 EngineRegistry。

    如果 pipeline_id 已注册，更新其 thread_id；
    否则，新建注册条目。
    """
    from pipeline.registry import get_engine_registry  # noqa: PLC0415

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
        filtered = [msg for msg in messages if isinstance(msg, dict) and msg.get("role") in _VALID_ROLES]
        conversation_history.clear()
        conversation_history.extend(filtered)
    elif fallback_content:
        conversation_history.append(
            {
                "role": "assistant",
                "content": fallback_content,
                "id": fallback_id,
            }
        )


async def _cancel_engine_task(engine_task: asyncio.Task) -> None:
    """取消引擎任务并等待其退出。

    安全地取消 asyncio.Task，捕获 CancelledError 和其他异常。
    """
    engine_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await engine_task


@dataclass
class StreamContext:
    """统一的流式请求上下文，合并了 engine_response / wake_response / drain_sub_bridge 三条路径的参数。"""

    pipeline_id: str
    message_id: str
    thread_id: str
    engine: Any = None
    bridge: Any = None
    conversation_history: list[dict[str, Any]] | None = None
    ws_notifier: Any = None
    websocket: Any = None
    stop_event: asyncio.Event | None = None
    agent_config: Any = None
    workspace: str = ""
    task_id: str = ""
    user_content: str = ""
    pipeline_ctx: Any = None


async def _create_engine_tracker(engine: Any) -> asyncio.Task:
    async def _poll():
        await asyncio.sleep(0.5)
        while True:
            is_running = getattr(engine, "is_running", False)
            is_suspended = getattr(engine, "is_suspended", False)
            if not is_running and not is_suspended:
                break
            await asyncio.sleep(0.3)

    return asyncio.create_task(_poll())


async def handle_stream_request(ctx: StreamContext) -> None:
    """统一的流式请求处理函数，合并了 engine_response / wake_response / drain_sub_bridge 三条路径。

    根据 ctx 中的参数自动判断路径：
    1. 有 engine 且无 user_content → drain 路径
    2. 有 pipeline_ctx → 新引擎路径

    所有路径共享：
    - drain_loop 消费
    - 结果提取 + conversation_history 同步
    - new_message 发送
    - 取消/超时/异常处理
    """

    logger.warning(
        "[handle_stream] 无可用路径: engine=%s bridge=%s user_content=%s pipeline_ctx=%s",
        ctx.engine is not None,
        ctx.bridge is not None,
        bool(ctx.user_content),
        ctx.pipeline_ctx is not None,
    )
