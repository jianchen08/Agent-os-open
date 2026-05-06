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
from channels.api.models import store as api_store

from application import Application

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# WebSocket 人类交互通知器
# ---------------------------------------------------------------------------


class WebSocketInteractionNotifier:
    """通过 WebSocket 将人类交互请求转发到前端。

    注册到 HumanInteractionService，当管道调用 human_interaction 工具时，
    将请求通过 WebSocket 发送到前端，前端展示交互面板。
    用户响应后，通过 interaction_response 消息提交回服务。

    如果前端在 auto_confirm_delay 秒内未响应，自动批准（回退策略）。
    """

    def __init__(self, auto_confirm_delay: float = 15.0) -> None:
        self._active_connections: dict[str, list[WebSocket]] = {}
        self._auto_confirm_delay = auto_confirm_delay
        self._service = None
        self._fallback_tasks: set[asyncio.Task] = set()

    def set_service(self, service) -> None:
        self._service = service

    def register(self, thread_id: str, websocket: WebSocket) -> None:
        if thread_id not in self._active_connections:
            self._active_connections[thread_id] = []
        if websocket not in self._active_connections[thread_id]:
            self._active_connections[thread_id].append(websocket)

    def unregister(self, thread_id: str, websocket: WebSocket) -> None:
        if thread_id in self._active_connections:
            conns = self._active_connections[thread_id]
            self._active_connections[thread_id] = [
                c for c in conns if c != websocket
            ]
            if not self._active_connections[thread_id]:
                del self._active_connections[thread_id]

    async def notify_request(self, request) -> bool:
        record = request if isinstance(request, dict) else {}
        thread_id = record.get("message_data", {}).get("thread_id", "")
        request_id = record.get("id", "")
        msg_data = record.get("message_data", {})

        # 优先按 thread_id 查找连接；找不到则广播到所有前端连接
        # （TaskWorker 创建的子 pipeline 使用独立的 pipeline_id，
        #   但 WebSocket 仅注册在 session_id 下，导致 thread_id 不匹配）
        conns = self._active_connections.get(thread_id, [])
        if not conns:
            all_conns: list[WebSocket] = []
            for _ws_list in self._active_connections.values():
                all_conns.extend(_ws_list)
            conns = all_conns

        sent = False
        if conns:
            payload = json.dumps({
                "type": "interaction_request",
                "data": {
                    "request_id": request_id,
                    "interaction_mode": msg_data.get(
                        "interaction_mode", "choice"
                    ),
                    "title": msg_data.get("title", ""),
                    "description": msg_data.get("description", ""),
                    "options": msg_data.get("options"),
                    "questions": msg_data.get("questions"),
                    "initial_message": msg_data.get("initial_message"),
                    "suggestions": msg_data.get("suggestions"),
                    "timeout_seconds": msg_data.get("timeout_seconds"),
                    "priority": msg_data.get("priority", "normal"),
                    "thread_id": thread_id,
                    "tab_id": msg_data.get("tab_id", ""),
                    "agent_id": msg_data.get("agent_id", ""),
                },
            }, ensure_ascii=False)

            for ws in conns:
                try:
                    await ws.send_text(payload)
                    sent = True
                except Exception:
                    logger.debug(
                        "[WSNotifier] 发送交互请求失败，连接可能已断开"
                    )

        if sent:
            logger.info(
                "[WSNotifier] 交互请求已发送 | request_id=%s", request_id,
            )
        else:
            logger.info(
                "[WSNotifier] 无前端连接，将在 %.0fs 后自动确认 | request_id=%s",
                self._auto_confirm_delay, request_id,
            )

        # 启动自动确认回退任务：如果前端未响应，自动批准
        if self._service:
            task = asyncio.create_task(
                self._auto_confirm_fallback(request_id, msg_data)
            )
            self._fallback_tasks.add(task)
            task.add_done_callback(self._fallback_tasks.discard)

        return sent

    async def _auto_confirm_fallback(
        self, request_id: str, msg_data: dict
    ) -> None:
        """延迟后检查请求是否仍待处理，若是则自动确认。"""
        await asyncio.sleep(self._auto_confirm_delay)
        if not self._service:
            return
        try:
            record = await self._service.get_request(request_id)
            if record and record.get("status") == "pending":
                logger.info(
                    "[WSNotifier] 前端未响应，自动确认 | request_id=%s",
                    request_id,
                )
                await self._service.submit_response(
                    request_id=request_id,
                    response_type="approved",
                    selected_option="approved",
                    feedback="自动确认（前端未响应）",
                )
        except Exception as exc:
            logger.debug("[WSNotifier] 自动确认回退失败: %s", exc)

    async def notify_cancel(
        self, request_id: str, reason: str | None = None, thread_id: str = ""
    ) -> bool:
        conns = self._active_connections.get(thread_id, [])
        if not conns:
            return True

        payload = json.dumps({
            "type": "interaction_cancelled",
            "data": {"request_id": request_id, "reason": reason},
        }, ensure_ascii=False)

        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                pass
        return True

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        conns = self._active_connections.get(thread_id, [])
        if not conns:
            return True

        payload = json.dumps({
            "type": "interaction_timeout",
            "data": {"request_id": request_id},
        }, ensure_ascii=False)

        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                pass
        return True

    async def notify_timeout_reminder(
        self, request_id, remaining_seconds, thread_id="", **kw
    ) -> bool:
        conns = self._active_connections.get(thread_id, [])
        if not conns:
            return True

        payload = json.dumps({
            "type": "interaction_timeout_reminder",
            "data": {
                "request_id": request_id,
                "remaining_seconds": remaining_seconds,
            },
        }, ensure_ascii=False)

        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:
                pass
        return True

    async def notify_conversation_start(
        self, thread_id, tab_id, title, **kw
    ) -> bool:
        return True

    async def broadcast_event(self, event_data: dict) -> bool:
        """向所有活跃的 WebSocket 连接广播自定义事件。

        用于 TaskWorker 等后台组件向前端推送事件（如 sub_agent_created），
        无需知道具体的 session_id 或 thread_id。

        Args:
            event_data: 完整的事件字典，包含 type 和 data 字段。
                示例: {"type": "sub_agent_created", "data": {...}}

        Returns:
            是否至少成功发送到一个连接
        """
        all_conns: list[WebSocket] = []
        for _ws_list in self._active_connections.values():
            all_conns.extend(_ws_list)

        if not all_conns:
            return False

        payload = json.dumps(event_data, ensure_ascii=False)
        sent = False
        for ws in all_conns:
            try:
                await ws.send_text(payload)
                sent = True
            except Exception:
                pass
        return sent


# 全局 WebSocket 通知器实例
_ws_interaction_notifier = WebSocketInteractionNotifier()


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
                logger.warning("缺少 event_bus 或 task_service，TaskWorker 未初始化")
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
            _ws_interaction_notifier.set_service(human_svc)
            human_svc.set_notifier(_ws_interaction_notifier)
            services["ws_interaction_notifier"] = _ws_interaction_notifier
            logger.info("WebSocketInteractionNotifier 已注册到 HumanInteractionService 和 services")
        except Exception as exc:
            logger.warning("注册 WebSocket 交互通知器失败: %s", exc)

        return PipelineContext(
            engine=engine,
            agent_config=agent_config,
            services=services,
            available=True,
        )

    except Exception as exc:
        logger.warning("管道引擎初始化失败，将回退到模拟回复模式: %s", exc, exc_info=True)
        return PipelineContext(available=False)


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

# 缓存的 call_timeout 值（首次调用时从 llm.yaml 加载，之后复用）
_cached_call_timeout: int | None = None


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
    # 用于将同步 _on_chunk 事件传递到异步发送协程的队列
    chunk_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    thinking_started = False
    thinking_content_parts: list[str] = []

    def _on_chunk(chunk: dict[str, Any]) -> None:
        """管道流式回调：将管道事件放入队列由主协程发送。

        处理以下 chunk 类型：
        - text: 正常回复 token
        - thinking: 思考过程
        - tool_start/tool_result: 工具调用事件
        - iteration: 管道迭代进度

        Args:
            chunk: 管道事件字典
        """
        nonlocal stream_started
        chunk_type = chunk.get("type", "text")
        content = chunk.get("content", "")

        # 文本 token — 累积内容
        if chunk_type == "text" and content:
            accumulated_content.append(content)

        # 将所有事件放入队列（thinking, tool_start, tool_result 等）
        chunk_queue.put_nowait(chunk)

    # 将用户消息添加到对话历史
    conversation_history.append({"role": "user", "content": user_content})

    # 将 engine._pipeline_id 同步为 session 的 active_pipeline_id，
    # 确保 ExecutionRecordStorage 中的记录能通过 session.pipeline_ids 找到。
    session = api_store.get_session(thread_id)
    if session:
        if session.active_pipeline_id:
            if ctx.engine._pipeline_id != session.active_pipeline_id:
                ctx.engine._pipeline_id = session.active_pipeline_id
        else:
            new_pid = session.generate_pipeline_id()
            ctx.engine._pipeline_id = new_pid

        # 确保 pipeline_id 在 pipeline_ids 列表中并持久化，
        # 以便 list_messages API 能通过 session.pipeline_ids 找到 YAML 执行记录。
        current_pid = ctx.engine._pipeline_id
        if current_pid and current_pid not in session.pipeline_ids:
            session.pipeline_ids.append(current_pid)
            session.active_pipeline_id = current_pid
        api_store.set_session(thread_id, session)

    pipeline_id = getattr(ctx.engine, "_pipeline_id", None)
    if pipeline_id:
        _ws_interaction_notifier.register(pipeline_id, websocket)

    # BUG-FIX-fix_pipeline_thread_association:
    # 问题根因: 管道运行后 YAML 文件没有存储 thread_id，导致无法关联到 thread。
    # 修复方案: 在管道运行开始前，将 thread_id 写入 ExecutionRecordStorage 的 summary。
    # 影响范围: list_messages、get_thread_detail 等接口的管道关联逻辑。
    # 修复日期: 2026-05-05
    if pipeline_id and ctx.services:
        _exec_storage = ctx.services.get("execution_record_storage")
        if _exec_storage is not None:
            try:
                _exec_storage.update_summary(pipeline_id, {"thread_id": thread_id})
            except Exception as _exc:
                logger.warning("写入管道 thread_id 失败: %s", _exc)

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

        # 异步消费队列：将 thinking / tool 事件实时发送到 WebSocket
        last_keepalive = asyncio.get_event_loop().time()

        async def _drain_chunk_queue(engine_task: asyncio.Task) -> None:
            """消费 chunk_queue，实时发送 thinking 和 tool 事件到前端。"""
            nonlocal thinking_started, last_keepalive
            while not engine_task.done() or not chunk_queue.empty():
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    # 每 5 秒发一次心跳保活，防止代理/浏览器 idle 超时
                    now = asyncio.get_event_loop().time()
                    if now - last_keepalive > 5.0:
                        last_keepalive = now
                        try:
                            await websocket.send_text(json.dumps({"type": "heartbeat_ack"}, ensure_ascii=False))
                        except Exception:
                            pass
                    continue
                if chunk is None:
                    break
                chunk_type = chunk.get("type", "text")
                content = chunk.get("content", "")

                # text 事件 → 实时流式发送到前端
                if chunk_type == "text" and content:
                    last_keepalive = asyncio.get_event_loop().time()
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "stream_chunk",
                            "data": {
                                "message_id": message_id,
                                "content": content,
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                # thinking 事件 → 发送到前端
                elif chunk_type == "thinking" and content:
                    thinking_content_parts.append(content)
                    try:
                        if not thinking_started:
                            thinking_started = True
                            await websocket.send_text(json.dumps({
                                "type": "thinking_start",
                                "data": {
                                    "message_id": message_id,
                                },
                            }, ensure_ascii=False))
                        await websocket.send_text(json.dumps({
                            "type": "thinking_chunk",
                            "data": {
                                "message_id": message_id,
                                "content": content,
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                # thinking_end 事件（迭代边界）
                elif chunk_type == "thinking_end":
                    if thinking_started:
                        thinking_started = False
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "thinking_end",
                                "data": {
                                    "message_id": message_id,
                                    "duration_ms": chunk.get("duration_ms"),
                                },
                            }, ensure_ascii=False))
                        except Exception:
                            pass

                # tool_start 事件
                elif chunk_type == "tool_start":
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_start",
                            "data": {
                                "message_id": message_id,
                                "tool_name": chunk.get("tool_name", "unknown"),
                                "args": chunk.get("args"),
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                # tool_result 事件
                elif chunk_type == "tool_result":
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_result",
                            "data": {
                                "message_id": message_id,
                                "tool_name": chunk.get("tool_name", "unknown"),
                                "success": chunk.get("success", True),
                                "result": chunk.get("result"),
                                "duration_ms": chunk.get("duration_ms"),
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                # iteration 事件（迭代开始时关闭旧的 thinking）
                elif chunk_type == "iteration":
                    if thinking_started:
                        thinking_started = False
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "thinking_end",
                                "data": {
                                    "message_id": message_id,
                                    "duration_ms": None,
                                },
                            }, ensure_ascii=False))
                        except Exception:
                            pass
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "iteration",
                            "data": {
                                "message_id": message_id,
                                "iteration": chunk.get("iteration", 0),
                                "max_iterations": chunk.get("max_iterations", 0),
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

        async def _drain_chunk_queue_with_suspend(
            engine_task: asyncio.Task,
            pipeline_engine: Any,
            call_timeout: int,
        ) -> None:
            """消费 chunk_queue，管道挂起时暂停超时计时。

            与 _drain_chunk_queue 相同的事件处理逻辑，
            但增加了 LLM 活动超时检测：管道挂起等待子任务时
            不计入 call_timeout（因为挂起期间没有 LLM 活动）。
            """
            nonlocal thinking_started, last_keepalive
            _last_active = asyncio.get_event_loop().time()

            while not engine_task.done() or not chunk_queue.empty():
                try:
                    chunk = await asyncio.wait_for(chunk_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    now = asyncio.get_event_loop().time()
                    if now - last_keepalive > 5.0:
                        last_keepalive = now
                        try:
                            await websocket.send_text(json.dumps({"type": "heartbeat_ack"}, ensure_ascii=False))
                        except Exception:
                            pass

                    if pipeline_engine is not None and getattr(pipeline_engine, "is_suspended", False):
                        _last_active = now
                    else:
                        elapsed = now - _last_active
                        if elapsed > call_timeout:
                            logger.warning(
                                "LLM 活动超时 (%.1fs/%ds): pipeline=%s",
                                elapsed, call_timeout,
                                getattr(pipeline_engine, "_pipeline_id", "unknown"),
                            )
                            return
                    continue

                _last_active = asyncio.get_event_loop().time()
                if chunk is None:
                    break
                chunk_type = chunk.get("type", "text")
                content = chunk.get("content", "")

                if chunk_type == "text" and content:
                    last_keepalive = asyncio.get_event_loop().time()
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "stream_chunk",
                            "data": {
                                "message_id": message_id,
                                "content": content,
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                elif chunk_type == "thinking" and content:
                    thinking_content_parts.append(content)
                    try:
                        if not thinking_started:
                            thinking_started = True
                            await websocket.send_text(json.dumps({
                                "type": "thinking_start",
                                "data": {
                                    "message_id": message_id,
                                },
                            }, ensure_ascii=False))
                        await websocket.send_text(json.dumps({
                            "type": "thinking_chunk",
                            "data": {
                                "message_id": message_id,
                                "content": content,
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                elif chunk_type == "thinking_end":
                    if thinking_started:
                        thinking_started = False
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "thinking_end",
                                "data": {
                                    "message_id": message_id,
                                    "duration_ms": chunk.get("duration_ms"),
                                },
                            }, ensure_ascii=False))
                        except Exception:
                            pass

                # tool_start 事件
                elif chunk_type == "tool_start":
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_start",
                            "data": {
                                "message_id": message_id,
                                "tool_name": chunk.get("tool_name", "unknown"),
                                "args": chunk.get("args"),
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                # tool_result 事件
                elif chunk_type == "tool_result":
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "tool_result",
                            "data": {
                                "message_id": message_id,
                                "tool_name": chunk.get("tool_name", "unknown"),
                                "success": chunk.get("success", True),
                                "result": chunk.get("result"),
                                "duration_ms": chunk.get("duration_ms"),
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

                elif chunk_type == "iteration":
                    if thinking_started:
                        thinking_started = False
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "thinking_end",
                                "data": {
                                    "message_id": message_id,
                                    "duration_ms": None,
                                },
                            }, ensure_ascii=False))
                        except Exception:
                            pass
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "iteration",
                            "data": {
                                "message_id": message_id,
                                "iteration": chunk.get("iteration", 0),
                                "max_iterations": chunk.get("max_iterations", 0),
                            },
                        }, ensure_ascii=False))
                    except Exception:
                        pass

        # 启动管道引擎（异步）
        engine_task = asyncio.create_task(
            ctx.engine.run(
                user_input=user_content,
                agent_config=ctx.agent_config,
                conversation_history=conversation_history[:-1],
                streaming=True,
                on_chunk=_on_chunk,
                auto_approve=True,
                interaction_mode="auto",
            )
        )

        # 同时消费队列中的实时事件（带超时保护，防止 LLM API 挂起导致前端永远卡住）
        # 管道挂起等待子任务时不计入超时（挂起期间没有 LLM 活动）
        _call_timeout = _get_call_timeout()

        engine_timed_out = False
        try:
            await asyncio.wait_for(
                _drain_chunk_queue_with_suspend(
                    engine_task, ctx.engine, _call_timeout,
                ),
                timeout=_call_timeout * 50,
            )
        except asyncio.TimeoutError:
            engine_timed_out = True
            logger.error(
                "管道引擎执行超时 (%ds)，取消任务: message_id=%s",
                _call_timeout, message_id,
            )
            engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):
                pass

        # 等待引擎完成并获取结果
        if engine_timed_out:
            raise TimeoutError(f"LLM 调用超时（{_call_timeout}s），请稍后重试")
        result = engine_task.result()

        # 发送 thinking_end（如果有 thinking 内容）
        if thinking_started:
            try:
                await websocket.send_text(json.dumps({
                    "type": "thinking_end",
                    "data": {
                        "message_id": message_id,
                        "duration_ms": None,
                    },
                }, ensure_ascii=False))
            except Exception:
                pass

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

        # ---- stream_chunk: 仅在实时流未发送时补发完整内容 ----
        if actual_content and not accumulated_content:
            logger.info(
                "补发 stream_chunk: message_id=%s, content_len=%d",
                message_id, len(actual_content),
            )
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

        # ---- 持久化消息到 store ----
        try:
            # 获取当前用户 ID（用于 user 消息）
            user_msg_id = conversation_history[-2]["id"] if len(conversation_history) >= 2 and "id" in conversation_history[-2] else None
            # 保存 user 消息
            api_store.add_message(
                thread_id=thread_id,
                message_id=user_msg_id or f"user_{message_id}",
                role="user",
                content=user_content,
            )
            # 保存 assistant 消息
            api_store.add_message(
                thread_id=thread_id,
                message_id=message_id,
                role="assistant",
                content=actual_content,
            )
        except Exception as persist_err:
            logger.warning("持久化消息失败: %s", persist_err)

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

        # 注册到 WebSocket 交互通知器
        _ws_interaction_notifier.register(thread_id, websocket)

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
                    message_id = uuid.uuid4().hex[:12]

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

                # ---- 交互响应：前端用户对 interaction_request 的回复 ----
                if msg_type == "interaction_response":
                    resp_data = message.get("data", {}) if isinstance(message.get("data"), dict) else {}
                    request_id = resp_data.get("request_id", "")
                    if not request_id:
                        logger.warning("interaction_response 缺少 request_id")
                        continue

                    try:
                        from human_interaction import get_human_interaction_service
                        human_svc = get_human_interaction_service()
                        await human_svc.submit_response(
                            request_id=request_id,
                            response_type=resp_data.get("response_type", "approved"),
                            selected_option=resp_data.get("selected_option"),
                            answers=resp_data.get("answers"),
                            feedback=resp_data.get("feedback"),
                        )
                        logger.info(
                            "交互响应已提交: request_id=%s, type=%s",
                            request_id, resp_data.get("response_type"),
                        )
                    except Exception as exc:
                        logger.error("提交交互响应失败: %s", exc, exc_info=True)
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
            # 从 WebSocket 交互通知器注销（session_id + pipeline_id）
            _ws_interaction_notifier.unregister(thread_id, websocket)
            if _pipeline_ctx and _pipeline_ctx.available:
                pid = getattr(_pipeline_ctx.engine, "_pipeline_id", None)
                if pid:
                    _ws_interaction_notifier.unregister(pid, websocket)
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

    # 挂载媒体文件静态服务（必须放在所有路由注册之后）
    try:
        from fastapi.staticfiles import StaticFiles

        output_dir = Path(os.environ.get("MEDIA_OUTPUT_DIR", "./output"))
        if output_dir.exists():
            media_dirs = {
                "images": output_dir / "images",
                "tts": output_dir / "tts",
                "video": output_dir / "video",
                "music": output_dir / "music",
                "test_images": output_dir / "test_images",
                "test_tts": output_dir / "test_tts",
                "test_video": output_dir / "test_video",
                "test_music": output_dir / "test_music",
            }
            for name, path in media_dirs.items():
                if path.exists():
                    path.mkdir(parents=True, exist_ok=True)
                    app.mount(
                        f"/media/{name}",
                        StaticFiles(directory=str(path)),
                        name=f"media_{name}",
                    )
            logger.info(
                "[STARTUP] Media static files mounted at /media/* (dirs: %s)",
                [n for n, p in media_dirs.items() if p.exists()],
            )
    except Exception as exc:
        logger.warning("[STARTUP] Media static files mount failed: %s", exc)

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
