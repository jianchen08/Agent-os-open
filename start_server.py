"""统一服务器启动入口。

同时启动 FastAPI（含 API 和 WebSocket）服务。
将 WebSocket 服务器挂载到 FastAPI 应用中，通过同一端口提供服务。

WebSocket 处理通过实际的 PipelineEngine 驱动 AI 回复，
支持流式输出、对话历史管理和中断控制。

用法：
    python start_server.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 将 src 目录加入 sys.path，确保模块可被正确导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from channels.api.app import create_app
from channels.api.auth import verify_token

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 管道上下文：封装 PipelineEngine 及其依赖
# ---------------------------------------------------------------------------


class PipelineContext:
    """管道引擎上下文，持有引擎实例、Agent 配置和服务字典。

    由 ``_init_pipeline_context()`` 创建，在 WebSocket 处理中通过
    ``_pipeline_ctx`` 全局变量访问。

    Attributes:
        engine: PipelineEngine 实例
        agent_config: 默认 Agent 配置（灵汐）
        services: 共享服务字典
        available: 是否成功初始化
    """

    def __init__(
        self,
        engine: Any | None = None,
        agent_config: Any | None = None,
        services: dict[str, Any] | None = None,
        available: bool = False,
    ) -> None:
        """初始化管道上下文。

        Args:
            engine: PipelineEngine 实例
            agent_config: Agent 配置
            services: 共享服务字典
            available: 是否成功初始化
        """
        self.engine = engine
        self.agent_config = agent_config
        self.services = services or {}
        self.available = available


# 全局管道上下文（延迟初始化）
_pipeline_ctx: PipelineContext | None = None
_task_worker_started: bool = False


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

        # 创建 ModelConfigLoader
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

        # 构建共享服务
        services = _build_services(agent_registry=agent_registry)

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

        # 创建管道引擎
        from pipeline.engine import PipelineEngine
        checkpoint_mgr = services.get("checkpoint_manager")
        engine = PipelineEngine(
            input_route_table=pipeline_config.input_route_table,
            output_route_table=pipeline_config.output_route_table,
            plugin_registry=plugin_registry,
            services=services,
            checkpoint_manager=checkpoint_mgr,
        )
        logger.info("PipelineEngine 创建完成")

        # 初始化 TaskWorker — 处理 task_submit 提交的任务
        try:
            from infrastructure.task_worker import TaskWorker
            event_bus = services.get("event_bus")
            task_service = services.get("task_service")
            if event_bus and task_service:
                _task_worker = TaskWorker(
                    task_service=task_service,
                    plugin_registry=plugin_registry,
                    input_route_table=pipeline_config.input_route_table,
                    output_route_table=pipeline_config.output_route_table,
                    services=services,
                    event_bus=event_bus,
                )
                # 存储为模块全局变量，供 WebSocket handler 启动
                globals()["_task_worker"] = _task_worker

                def _eval_pipeline_factory():
                    return PipelineEngine(
                        input_route_table=pipeline_config.input_route_table,
                        output_route_table=pipeline_config.output_route_table,
                        plugin_registry=plugin_registry,
                        services=services,
                    )

                sys._agent_os_pipeline_factory = _eval_pipeline_factory
                logger.info("TaskWorker 初始化完成（将在首次请求时启动）")
            else:
                logger.warning("缺少 event_bus 或 task_service，TaskWorker 未初始化")
        except Exception as exc:
            logger.warning("TaskWorker 初始化失败: %s", exc)

        return PipelineContext(
            engine=engine,
            agent_config=agent_config,
            services=services,
            available=True,
        )

    except Exception as exc:
        logger.warning("管道引擎初始化失败，将回退到模拟回复模式: %s", exc, exc_info=True)
        return PipelineContext(available=False)


def _build_services(agent_registry: Any = None) -> dict[str, Any]:
    """构建共享服务字典。

    创建工具注册表、记忆存储等共享服务，
    插件通过 ctx.get_service() 自主获取。

    Args:
        agent_registry: Agent 注册表实例（可选）

    Returns:
        服务名称到实例的映射字典
    """
    services: dict[str, Any] = {}

    # 1. ToolRegistry — 工具注册表
    try:
        from tools.registry import ToolRegistry
        tool_registry = ToolRegistry()
        _register_basic_tools(tool_registry)
        services["tool_registry"] = tool_registry
        sys._agent_os_tool_registry = tool_registry
        logger.info("服务已创建: tool_registry (%d 个基础工具)", tool_registry.count())

        from tools.auto_loader import init_tool_auto_loader
        init_tool_auto_loader(tool_registry)
        logger.info("ToolAutoLoader 已初始化")
    except Exception as exc:
        logger.warning("创建 tool_registry 服务失败: %s", exc)

    # 2. JsonMemoryStore — 记忆存储
    json_store = None
    try:
        from memory.storage.json_store import JsonMemoryStore
        json_store = JsonMemoryStore()
        logger.info("服务已创建: JsonMemoryStore")
    except Exception as exc:
        logger.warning("创建 JsonMemoryStore 失败: %s", exc)

    if json_store is not None:
        services["memory_store"] = json_store
        services["semantic_storage"] = json_store

    # 3. MessageQueue — 管道间消息传递
    try:
        from infrastructure.message_queue import MessageQueue
        services["message_queue"] = MessageQueue()
        logger.info("服务已创建: message_queue")
    except Exception as exc:
        logger.warning("创建 message_queue 服务失败: %s", exc)

    # 4. ExecutionRecordStorage — 执行记录持久化
    try:
        from infrastructure.execution_record_storage import ExecutionRecordStorage
        services["execution_record_storage"] = ExecutionRecordStorage(
            data_dir=str(_PROJECT_ROOT / "data" / "pipelines")
        )
        logger.info("服务已创建: execution_record_storage")
    except Exception as exc:
        logger.warning("创建 execution_record_storage 服务失败: %s", exc)

    # 5. EventBus — 事件总线
    try:
        from pipeline.event_bus import EventBus
        event_bus = EventBus()
        services["event_bus"] = event_bus
        sys._agent_os_event_bus = event_bus
    except Exception as exc:
        logger.warning("创建 event_bus 服务失败: %s", exc)

    # 6. TaskService — 任务服务
    try:
        from tasks.service import TaskService
        task_service = TaskService()
        services["task_service"] = task_service
        sys._agent_os_task_service = task_service
        logger.info("服务已创建: task_service")
    except Exception as exc:
        logger.warning("创建 task_service 服务失败: %s", exc)

    # 7. AgentRegistry — 供 TaskWorker 加载子 agent 配置
    if agent_registry is not None:
        services["agent_registry"] = agent_registry
        sys._agent_os_agent_registry = agent_registry
        logger.info("服务已注入: agent_registry")

    # 8. PipelineCheckpointManager — 管道检查点
    try:
        from infrastructure.checkpoint.pipeline_checkpoint import PipelineCheckpointManager
        services["checkpoint_manager"] = PipelineCheckpointManager()
        logger.info("服务已创建: checkpoint_manager")
    except Exception as exc:
        logger.warning("创建 checkpoint_manager 服务失败: %s", exc)

    sys._agent_os_services = services
    return services


def _register_basic_tools(registry: Any) -> None:
    """注册基础工具（无需依赖注入）。

    Args:
        registry: ToolRegistry 实例
    """
    import datetime
    import math as _math
    from tools.types import Tool, ToolSource

    # current_time
    def current_time(params: dict[str, Any]) -> str:
        """获取当前时间。"""
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        tool = Tool(
            name="current_time",
            description="获取当前日期和时间",
            source=ToolSource.BUILTIN,
            input_schema={
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "时区（默认本地）"},
                },
            },
        )
        registry.register_with_handler(tool=tool, handler=current_time)
    except Exception as exc:
        logger.warning("注册基础工具 current_time 失败: %s", exc)

    # calculator
    def calculator(params: dict[str, Any]) -> str:
        """执行简单数学计算。"""
        expression = params.get("expression", "")
        if not expression:
            return "错误：未提供计算表达式"
        try:
            allowed_names = {
                "abs": abs, "round": round, "min": min, "max": max,
                "pow": pow, "sum": sum,
                "pi": _math.pi, "e": _math.e,
                "sqrt": _math.sqrt, "ceil": _math.ceil, "floor": _math.floor,
            }
            result = eval(expression, {"__builtins__": {}}, allowed_names)  # noqa: S307
            return str(result)
        except Exception as exc:
            return f"计算错误：{exc}"

    try:
        tool = Tool(
            name="calculator",
            description="执行简单数学计算，支持加减乘除和常用数学函数",
            source=ToolSource.BUILTIN,
            input_schema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "数学表达式，如 '123+456' 或 'sqrt(144)'",
                    },
                },
                "required": ["expression"],
            },
        )
        registry.register_with_handler(tool=tool, handler=calculator)
    except Exception as exc:
        logger.warning("注册基础工具 calculator 失败: %s", exc)


# ---------------------------------------------------------------------------
# 模拟回复（回退模式）
# ---------------------------------------------------------------------------


def _generate_simulated_reply(user_content: str) -> str:
    """根据用户输入内容生成模拟 AI 回复。

    仅在管道引擎初始化失败时作为回退使用。

    Args:
        user_content: 用户发送的文本内容

    Returns:
        模拟的 AI 回复文本
    """
    text = user_content.strip().lower()
    if text in ("你好", "hello", "hi", "hey", "嗨"):
        return (
            "你好！我是 Agent OS 助手，很高兴为你服务。\n\n"
            "有什么我可以帮助你的吗？"
        )
    if text in ("你是谁", "who are you"):
        return (
            "我是 Agent OS 的 AI 助手。\n\n"
            "我可以回答问题、提供建议和协助完成各种任务。"
        )
    if any(kw in text for kw in ("帮助", "help", "能做什么")):
        return (
            "我可以帮助你完成以下任务：\n\n"
            "1. 回答各类问题\n"
            "2. 提供技术建议\n"
            "3. 协助代码开发\n"
            "4. 数据分析和处理\n\n"
            "请告诉我你需要什么帮助！"
        )
    return f"我收到了你的消息：{user_content}"


# ---------------------------------------------------------------------------
# 流式回复处理
# ---------------------------------------------------------------------------


async def _stream_engine_response(
    websocket: WebSocket,
    user_content: str,
    message_id: str,
    stop_event: asyncio.Event,
    thread_id: str,
    conversation_history: list[dict[str, Any]],
    ctx: PipelineContext,
) -> None:
    """通过管道引擎获取 AI 回复并以流式方式发送到 WebSocket。

    流程：
    1. 构建 on_chunk 回调，将管道事件转换为 WebSocket 协议消息
    2. 调用 engine.run() 执行管道
    3. 从结果中提取 raw_result 更新对话历史
    4. 发送 new_message 最终消息

    Args:
        websocket: WebSocket 连接实例
        user_content: 用户发送的原始文本
        message_id: 本轮回复的消息 UUID
        stop_event: 用于取消流式生成的事件对象
        thread_id: 当前线程/会话 ID
        conversation_history: 对话历史列表（会被就地更新）
        ctx: 管道上下文
    """
    # 流式状态追踪
    stream_started = False
    accumulated_content: list[str] = []

    def _on_chunk(chunk: dict[str, Any]) -> None:
        """管道流式回调：将管道事件转换为 WebSocket 协议消息。

        处理以下 chunk 类型：
        - text: 正常回复 token，发送 stream_chunk
        - thinking: 思考过程（暂不发送到前端）
        - tool_call/tool_start/tool_result: 工具调用事件
        - iteration: 管道迭代进度

        注意：此回调在同步上下文中被调用，使用 asyncio.run_coroutine_threadsafe
        或直接写入列表，由主协程负责发送。

        Args:
            chunk: 管道事件字典
        """
        nonlocal stream_started
        chunk_type = chunk.get("type", "text")
        content = chunk.get("content", "")

        # 文本 token — 累积内容
        if chunk_type == "text" and content:
            accumulated_content.append(content)

        # 其他事件类型（thinking, tool_call, tool_start, tool_result, iteration）
        # 仅记录日志，暂不发送到前端
        if chunk_type == "tool_start":
            logger.debug("工具开始执行: %s", chunk.get("tool_name", "unknown"))
        elif chunk_type == "tool_result":
            logger.debug(
                "工具执行完成: %s, success=%s",
                chunk.get("tool_name", "unknown"),
                chunk.get("success", True),
            )
        elif chunk_type == "iteration":
            logger.debug(
                "管道迭代: %d/%d",
                chunk.get("iteration", 0),
                chunk.get("max_iterations", 0),
            )

    # 将用户消息添加到对话历史
    conversation_history.append({"role": "user", "content": user_content})

    try:
        # ---- stream_start ----
        await websocket.send_text(json.dumps({
            "type": "stream_start",
            "data": {
                "message_id": message_id,
                "session_id": thread_id,
            },
        }, ensure_ascii=False))
        stream_started = True

        # 调用管道引擎
        result = await ctx.engine.run(
            user_input=user_content,
            agent_config=ctx.agent_config,
            conversation_history=conversation_history[:-1],  # 排除刚添加的 user 消息，engine 内部会追加
            streaming=True,
            on_chunk=_on_chunk,
            auto_approve=True,
            interaction_mode="auto",
        )

        # 检查是否被中断
        if stop_event.is_set():
            logger.info("流式生成被用户中断: message_id=%s", message_id)

        # 获取完整回复内容
        # 优先使用管道维护的 messages，其次使用 on_chunk 累积的内容，最后用 raw_result
        final_messages = result.get("messages", [])
        actual_content = ""

        if final_messages:
            # 从最终 messages 中提取最后一条 assistant 消息
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break
            # 更新对话历史为管道维护的完整消息列表
            conversation_history.clear()
            conversation_history.extend(final_messages)
        elif accumulated_content:
            actual_content = "".join(accumulated_content)
        else:
            actual_content = result.get("raw_result", "")

        # ---- stream_chunk: 发送完整内容作为一个 chunk ----
        if actual_content:
            await websocket.send_text(json.dumps({
                "type": "stream_chunk",
                "data": {
                    "content": actual_content,
                    "message_id": message_id,
                },
            }, ensure_ascii=False))

        # ---- stream_end ----
        await websocket.send_text(json.dumps({
            "type": "stream_end",
            "data": {
                "message_id": message_id,
                "full_content": actual_content,
            },
        }, ensure_ascii=False))

        # ---- new_message 最终消息 ----
        await websocket.send_text(json.dumps({
            "type": "new_message",
            "data": {
                "id": message_id,
                "role": "assistant",
                "content": actual_content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sequence": 1,
            },
        }, ensure_ascii=False))

    except asyncio.CancelledError:
        """流式任务被取消（用户中断或连接断开）。"""
        logger.info("流式任务被取消: message_id=%s", message_id)
        # 尝试发送 stream_end 以确保前端状态一致
        try:
            partial_content = "".join(accumulated_content)
            await websocket.send_text(json.dumps({
                "type": "stream_end",
                "data": {
                    "message_id": message_id,
                    "full_content": partial_content,
                },
            }, ensure_ascii=False))
        except Exception:
            pass
        raise

    except Exception as exc:
        """管道引擎执行出错。"""
        logger.error("管道引擎执行失败: %s", exc, exc_info=True)
        # 发送错误信息到前端
        try:
            error_content = f"抱歉，处理你的消息时出现错误：{exc}"
            if stream_started:
                await websocket.send_text(json.dumps({
                    "type": "stream_end",
                    "data": {
                        "message_id": message_id,
                        "full_content": error_content,
                    },
                }, ensure_ascii=False))
            await websocket.send_text(json.dumps({
                "type": "new_message",
                "data": {
                    "id": message_id,
                    "role": "assistant",
                    "content": error_content,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sequence": 1,
                },
            }, ensure_ascii=False))
        except Exception:
            pass


async def _stream_simulated_response(
    websocket: WebSocket,
    user_content: str,
    message_id: str,
    stop_event: asyncio.Event,
    session_id: str,
) -> None:
    """异步发送模拟 AI 流式回复（回退模式）。

    按照流式协议依次发送 stream_start -> stream_chunk(逐字) -> stream_end -> new_message。
    当 stop_event 被设置时，立即中断流式输出。

    Args:
        websocket: WebSocket 连接实例
        user_content: 用户发送的原始文本
        message_id: 本轮回复的消息 UUID
        stop_event: 用于取消流式生成的事件对象
        session_id: 当前线程/会话 ID
    """
    full_content = _generate_simulated_reply(user_content)

    # ---- stream_start ----
    await websocket.send_text(json.dumps({
        "type": "stream_start",
        "data": {
            "message_id": message_id,
            "session_id": session_id,
        },
    }, ensure_ascii=False))

    # ---- stream_chunk 逐字发送 ----
    sent_chars: list[str] = []
    for char in full_content:
        if stop_event.is_set():
            logger.info("流式生成被用户中断: message_id=%s", message_id)
            break
        sent_chars.append(char)
        await websocket.send_text(json.dumps({
            "type": "stream_chunk",
            "data": {
                "content": char,
                "message_id": message_id,
            },
        }, ensure_ascii=False))
        # 模拟逐字打字延迟
        await asyncio.sleep(0.05)

    actual_content = "".join(sent_chars)

    # ---- stream_end ----
    await websocket.send_text(json.dumps({
        "type": "stream_end",
        "data": {
            "message_id": message_id,
            "full_content": actual_content,
        },
    }, ensure_ascii=False))

    # ---- new_message 最终消息 ----
    await websocket.send_text(json.dumps({
        "type": "new_message",
        "data": {
            "id": message_id,
            "role": "assistant",
            "content": actual_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": 1,
        },
    }, ensure_ascii=False))


# ---------------------------------------------------------------------------
# FastAPI 应用创建
# ---------------------------------------------------------------------------


def create_combined_app() -> FastAPI:
    """创建合并了 WebSocket 功能的 FastAPI 应用。

    将 WebSocket 路由注册到 FastAPI 中，实现单端口统一服务。
    在应用创建时初始化管道引擎上下文。

    Returns:
        配置好的 FastAPI 应用实例
    """
    global _pipeline_ctx
    app = create_app()

    # 初始化管道引擎上下文
    _pipeline_ctx = _init_pipeline_context()
    if _pipeline_ctx.available:
        logger.info("管道引擎已就绪，WebSocket 将使用真实 AI 回复")
    else:
        logger.warning("管道引擎未就绪，WebSocket 将使用模拟回复")

    # WebSocket 连接管理
    active_connections: dict[str, list[WebSocket]] = {}

    # 每个 thread_id 的对话历史
    conversation_histories: dict[str, list[dict[str, Any]]] = {}

    @app.websocket("/ws")
    async def websocket_root(websocket: WebSocket) -> None:
        """处理根路径 WebSocket 连接。"""
        await websocket.accept()
        logger.info("WebSocket 连接已建立（根路径）")
        try:
            while True:
                data = await websocket.receive_text()
                await websocket.send_text(f"Echo: {data}")
        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开（根路径）")

    @app.websocket("/ws/{thread_id}")
    async def websocket_thread(websocket: WebSocket, thread_id: str) -> None:
        """处理线程 WebSocket 连接，支持 AI 流式回复。

        根据管道引擎是否可用，自动选择真实 AI 回复或模拟回复。
        每个线程维护独立的对话历史。

        支持可选的 token query 参数进行认证。
        处理前端发送的 user_input / heartbeat / stop_generation 消息类型，
        并通过 stream_start -> stream_chunk -> stream_end -> new_message 协议回复。

        Args:
            websocket: WebSocket 连接实例
            thread_id: 线程 ID
        """
        # 可选 token 验证
        token = websocket.query_params.get("token", "")
        if token:
            payload = verify_token(token)
            if payload is None:
                await websocket.close(code=4001, reason="Invalid or expired token")
                return

        await websocket.accept()

        # 管理连接
        if thread_id not in active_connections:
            active_connections[thread_id] = []
        active_connections[thread_id].append(websocket)

        # 初始化对话历史
        if thread_id not in conversation_histories:
            conversation_histories[thread_id] = []

        # 发送连接确认
        await websocket.send_text(json.dumps({
            "type": "connection_confirmation",
            "data": {
                "thread_id": thread_id,
                "status": "connected",
            },
        }, ensure_ascii=False))

        logger.info("WebSocket 连接已建立: thread_id=%s", thread_id)

        # 当前流式生成任务和取消事件
        current_stream_task: asyncio.Task | None = None
        stop_event = asyncio.Event()

        # 获取当前线程的对话历史引用
        history = conversation_histories[thread_id]

        try:
            while True:
                data = await websocket.receive_text()

                # 尝试解析 JSON，兼容纯文本消息
                try:
                    message = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    message = {"type": "user_input", "content": data}

                msg_type = message.get("type", "")

                # ---- 心跳响应 ----
                if msg_type == "heartbeat":
                    await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
                    continue

                # ---- 停止生成 ----
                if msg_type == "stop_generation":
                    stop_event.set()
                    if current_stream_task and not current_stream_task.done():
                        current_stream_task.cancel()
                        try:
                            await current_stream_task
                        except asyncio.CancelledError:
                            pass
                    logger.info("用户请求停止生成: thread_id=%s", thread_id)
                    continue

                # ---- 用户输入：启动流式回复 ----
                if msg_type == "user_input":
                    # 首次请求时启动 TaskWorker
                    global _task_worker_started
                    if not _task_worker_started:
                        _task_worker_started = True
                        try:
                            tw = globals().get("_task_worker")
                            if tw and hasattr(tw, "start"):
                                await tw.start()
                                logger.info("TaskWorker started (web server mode)")
                        except Exception as exc:
                            logger.warning("TaskWorker start failed: %s", exc)

                    # 提取用户文本内容
                    user_content = (
                        message.get("data", {}).get("content")
                        if isinstance(message.get("data"), dict)
                        else message.get("content", "")
                    )
                    if not user_content:
                        continue

                    # 若上一轮流式回复尚未结束，先取消
                    stop_event.set()
                    if current_stream_task and not current_stream_task.done():
                        current_stream_task.cancel()
                        try:
                            await current_stream_task
                        except asyncio.CancelledError:
                            pass

                    # 重置取消事件，启动新的流式任务
                    stop_event = asyncio.Event()
                    message_id = str(uuid.uuid4())

                    # 根据管道引擎是否可用选择处理方式
                    if _pipeline_ctx and _pipeline_ctx.available:
                        current_stream_task = asyncio.create_task(
                            _stream_engine_response(
                                websocket, user_content, message_id,
                                stop_event, thread_id, history, _pipeline_ctx,
                            )
                        )
                    else:
                        current_stream_task = asyncio.create_task(
                            _stream_simulated_response(
                                websocket, user_content, message_id,
                                stop_event, thread_id,
                            )
                        )
                    continue

                # 未知消息类型，忽略
                logger.debug("收到未处理的消息类型: %s", msg_type)

        except WebSocketDisconnect:
            logger.info("WebSocket 连接已断开: thread_id=%s", thread_id)
        finally:
            # 连接断开时取消进行中的流式任务
            stop_event.set()
            if current_stream_task and not current_stream_task.done():
                current_stream_task.cancel()
            if thread_id in active_connections:
                active_connections[thread_id] = [
                    c for c in active_connections[thread_id] if c != websocket
                ]
                if not active_connections[thread_id]:
                    del active_connections[thread_id]
                    # 清理对话历史（无活跃连接时）
                    conversation_histories.pop(thread_id, None)

    @app.websocket("/ws/chat/{thread_id}")
    async def websocket_chat(websocket: WebSocket, thread_id: str) -> None:
        """处理聊天 WebSocket 连接，复用 websocket_thread 的流式回复逻辑。

        Args:
            websocket: WebSocket 连接实例
            thread_id: 线程 ID
        """
        await websocket_thread(websocket, thread_id)

    return app


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> None:
    """主函数，启动 uvicorn 服务器。"""
    logger.info("正在启动 Agent OS 服务器...")
    logger.info("API 地址: http://localhost:8888")
    logger.info("API 文档: http://localhost:8888/docs")
    logger.info("健康检查: http://localhost:8888/health")

    app = create_combined_app()
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="info")


if __name__ == "__main__":
    main()
