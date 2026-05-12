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
from src.pipeline.stream_bridge import PipelineStreamBridge, TargetedSink
from src.infrastructure.execution_record_storage import ExecutionRecordData

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent


def _persist_messages_to_yaml(
    exec_storage: Any,
    pipeline_id: str,
    user_msg_id: str,
    user_content: str,
    assistant_msg_id: str,
    assistant_content: str,
    thinking_content: str | None = None,
) -> None:
    if exec_storage is None or not pipeline_id:
        return
    try:
        existing = exec_storage.list_by_pipeline(pipeline_id)
        next_seq = max((r.sequence for r in existing), default=0) + 1
        exec_storage.save(ExecutionRecordData(
            record_id=user_msg_id,
            pipeline_run_id=pipeline_id,
            type="user",
            sequence=next_seq,
            role="user",
            content=user_content,
        ))
        exec_storage.save(ExecutionRecordData(
            record_id=assistant_msg_id,
            pipeline_run_id=pipeline_id,
            type="ai",
            sequence=next_seq + 1,
            role="assistant",
            content=assistant_content,
            thinking_content=thinking_content or None,
        ))
        logger.info(
            "[persist_yaml] pipeline=%s user_seq=%d ai_seq=%d thinking=%d",
            pipeline_id[:12], next_seq, next_seq + 1, len(thinking_content or ""),
        )
    except Exception as exc:
        logger.warning("持久化消息到 ExecutionRecordStorage 失败: %s", exc)


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

    def __init__(self, auto_confirm_delay: float = 600.0) -> None:
        self._active_connections: dict[str, list[WebSocket]] = {}
        self._pipeline_thread_map: dict[str, str] = {}
        self._auto_confirm_delay = auto_confirm_delay
        self._service = None
        self._fallback_tasks: set[asyncio.Task] = set()
        # BUG-FIX-fix_20260511_auto_confirm_not_cancelled:
        # 问题根因: _auto_confirm_fallback 任务在用户响应后未被取消，浪费资源且可能竞争。
        # 修复方案: 增加 request_id → fallback task 的映射，支持按 request_id 取消。
        self._fallback_request_map: dict[str, asyncio.Task] = {}
        self._global_connections: dict[str, WebSocket] = {}

    def set_service(self, service) -> None:
        self._service = service

    def register_pipeline_thread(self, pipeline_id: str, thread_id: str) -> None:
        """注册 pipeline_id 到 ws_thread_id 的映射。

        当主管道通过 _stream_engine_response 创建时调用，
        记录 pipeline_id 与 WebSocket 连接 thread_id 的对应关系，
        以便子管道事件能直接路由到正确的 WebSocket 连接。

        Args:
            pipeline_id: 主管道的 pipeline_id（engine._pipeline_id）
            thread_id: WebSocket 连接的 thread_id
        """
        self._pipeline_thread_map[pipeline_id] = thread_id

    def get_thread_for_pipeline(self, pipeline_id: str) -> str:
        """根据 pipeline_id 查找对应的 ws_thread_id。

        用于子管道（TaskWorker）向前端路由事件时，
        通过主管道的 pipeline_id 找到正确的 WebSocket thread_id。

        Args:
            pipeline_id: 主管道的 pipeline_id

        Returns:
            对应的 ws_thread_id，未找到则返回空字符串
        """
        return self._pipeline_thread_map.get(pipeline_id, "")

    def register(self, thread_id: str, websocket: WebSocket) -> None:
        if thread_id not in self._active_connections:
            self._active_connections[thread_id] = []
        if websocket not in self._active_connections[thread_id]:
            self._active_connections[thread_id].append(websocket)
            logger.info(
                "WS注册: thread_id=%s, 当前连接数=%d, 所有线程=%s",
                thread_id, len(self._active_connections[thread_id]),
                {k: len(v) for k, v in self._active_connections.items()},
            )

    def unregister(self, thread_id: str, websocket: WebSocket) -> None:
        if thread_id in self._active_connections:
            conns = self._active_connections[thread_id]
            self._active_connections[thread_id] = [
                c for c in conns if c != websocket
            ]
            if not self._active_connections[thread_id]:
                del self._active_connections[thread_id]

    def unregister_all_for_ws(self, websocket: WebSocket) -> None:
        """清理指定 WebSocket 连接在所有 thread_id 下的注册。"""
        for tid in list(self._active_connections.keys()):
            conns = self._active_connections.get(tid, [])
            if websocket in conns:
                self._active_connections[tid] = [c for c in conns if c != websocket]
                if not self._active_connections[tid]:
                    del self._active_connections[tid]

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
                    "pipeline_id": record.get("message_data", {}).get("pipeline_id", ""),
                    "file_contents": msg_data.get("file_contents"),
                    "agent_level": msg_data.get("agent_level"),
                    "session_id": record.get("session_id", ""),
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
            # BUG-FIX-fix_20260511_auto_confirm_not_cancelled:
            # 记录 request_id → task 映射，以便用户响应后能取消该任务。
            self._fallback_request_map[request_id] = task
            task.add_done_callback(
                lambda t, _rid=request_id: self._fallback_request_map.pop(_rid, None)
            )
            task.add_done_callback(self._fallback_tasks.discard)

        return sent

    # ── 全局单连接模式方法 ──

    def register_global(self, user_id: str, websocket: WebSocket) -> None:
        """注册全局单连接（新架构：每用户一个 WS 连接）。"""
        old = self._global_connections.get(user_id)
        if old is not None:
            logger.info("[GlobalWS] 踢掉旧连接: user=%s", user_id[:12])
            try:
                asyncio.get_event_loop().create_task(old.close(code=4000, reason="被新连接替换"))
            except Exception:
                pass
        self._global_connections[user_id] = websocket
        logger.info("[GlobalWS] 全局连接已注册: user=%s, 总连接数=%d", user_id[:12], len(self._global_connections))

    def unregister_global(self, user_id: str, websocket: WebSocket = None) -> None:
        """注销全局连接。只有当传入的 websocket 是当前注册的连接时才删除，防止新连接被旧连接的 finally 块误删。"""
        current = self._global_connections.get(user_id)
        if websocket is not None and current is not websocket:
            logger.info("[GlobalWS] 跳过注销（已被新连接替换）: user=%s", user_id[:12])
            return
        self._global_connections.pop(user_id, None)
        logger.info("[GlobalWS] 全局连接已注销: user=%s, 剩余=%d", user_id[:12], len(self._global_connections))

    async def send_to_user(self, user_id: str, event: dict) -> bool:
        """通过全局单连接推送事件给指定用户。"""
        ws = self._global_connections.get(user_id)
        if ws is None:
            logger.error("[GlobalWS] 用户不在线: user=%s", user_id[:12])
            return False
        try:
            await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
            return True
        except Exception as exc:
            logger.error("[GlobalWS] 推送失败，注销连接: user=%s err=%s", user_id[:12], exc)
            self._global_connections.pop(user_id, None)
            return False

    def get_global_websocket(self, user_id: str) -> WebSocket | None:
        """获取指定用户的全局 WebSocket 连接。"""
        return self._global_connections.get(user_id)

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
                options = msg_data.get("options", [])
                first_option_id = options[0]["id"] if options else "approve"
                await self._service.submit_response(
                    request_id=request_id,
                    response_type="approved",
                    selected_option=first_option_id,
                    feedback="自动确认（前端未响应）",
                )
        except Exception as exc:
            logger.debug("[WSNotifier] 自动确认回退失败: %s", exc)

    def cancel_fallback(self, request_id: str) -> None:
        """取消指定请求的自动确认回退任务。

        当用户在前端响应交互请求后调用，取消对应的 _auto_confirm_fallback
        任务以避免不必要的资源浪费和潜在的竞争条件。

        Args:
            request_id: 要取消自动确认的请求 ID
        """
        task = self._fallback_request_map.pop(request_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(
                "[WSNotifier] 已取消自动确认回退 | request_id=%s", request_id,
            )

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

    async def send_to_thread(self, thread_id: str, event_data: dict) -> bool:
        """向指定 thread_id 的最新活跃 WebSocket 连接发送事件。

        优先查找 _active_connections（per-session 连接），
        若无则回退到 _global_connections（全局单连接）。

        只发送到第一个成功的连接，避免多个连接导致消息重复。
        发送失败的连接会被清理出活跃列表。

        Args:
            thread_id: 目标会话的 thread_id
            event_data: 完整的事件字典

        Returns:
            是否成功发送
        """
        conns = self._active_connections.get(thread_id, [])
        if conns:
            payload = json.dumps(event_data, ensure_ascii=False)
            stale: list = []
            for ws in conns:
                try:
                    await ws.send_text(payload)
                    return True
                except Exception:
                    stale.append(ws)
            if stale:
                self._active_connections[thread_id] = [
                    c for c in conns if c not in stale
                ]

        for user_id, ws in list(self._global_connections.items()):
            try:
                await ws.send_text(json.dumps(event_data, ensure_ascii=False))
                return True
            except Exception:
                self._global_connections.pop(user_id, None)

        logger.warning(
            "send_to_thread: 无活跃连接: thread_id=%s type=%s active=%s global=%s",
            thread_id[:12] if thread_id else "(empty)",
            event_data.get("type", "?"),
            {k[:12]: len(v) for k, v in self._active_connections.items()},
            list(self._global_connections.keys()),
        )
        return False

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
        for tid, _ws_list in self._active_connections.items():
            all_conns.extend(_ws_list)
            if len(_ws_list) > 1:
                logger.warning(
                    "broadcast_event: thread_id=%s 有 %d 个连接!",
                    tid, len(_ws_list),
                )

        if not all_conns:
            return False

        etype = event_data.get("type", "unknown")
        _quiet_types = {
            "heartbeat_ack", "sub_agent_created",
            "stream_start", "stream_chunk", "stream_end", "stream_keepalive",
            "thinking_start", "thinking_chunk", "thinking_end",
            "tool_start", "tool_result", "new_message", "iteration",
        }
        if etype not in _quiet_types:
            logger.warning(
                "broadcast_event: 流式事件走了广播路径! type=%s, conns=%d",
                etype, len(all_conns),
            )

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


# _route_to_sub_pipeline 已移除，子管道路由统一使用 pipeline.message_bus.send_pipeline_message


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

    if ws_notifier and thread_id and hasattr(ws_notifier, "register_pipeline_thread"):
        ws_notifier.register_pipeline_thread(pipeline_id, thread_id)

    if pre_created_bridge is not None:
        bridge = pre_created_bridge
        # BUG-FIX: 保留 TargetedSink（全局端点场景），
        # 仅在有 ws_notifier 且 bridge 不是 TargetedSink 时才替换
        if ws_notifier is not None:
            bridge.output_sink = TargetedSink(ws_notifier, thread_id)
    else:
        bridge = PipelineStreamBridge(
            pipeline_id=pipeline_id,
            output_sink=TargetedSink(ws_notifier, thread_id),
            message_id=message_id,
        )
        engine._saved_on_chunk = bridge.on_chunk
        engine._saved_streaming = True
        if engine._suspended_state is not None:
            engine._suspended_state["on_chunk"] = bridge.on_chunk
            engine._suspended_state["streaming"] = True

    _call_timeout = _get_call_timeout()

    _long_wait = asyncio.create_task(asyncio.sleep(86400))

    try:
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
            timeout=_call_timeout * 50,
        )

        full_content = drain_result.get("accumulated_content", "")
        logger.info(
            "[wake_response] drain_loop 完成: pipeline=%s content=%d chars timed_out=%s",
            pipeline_id[:12], len(full_content), drain_result.get("timed_out"),
        )

        # 发送 stream_end
        await bridge._close_thinking_if_active(None)
        await bridge._send_event({
            "type": "stream_end",
            "data": {
                "message_id": message_id,
                "full_content": full_content,
                "pipeline_id": bridge.pipeline_id,
            },
        })

        # 发送 new_message 更新前端
        if full_content:
            await bridge._send_event({
                "type": "new_message",
                "data": {
                    "message_id": message_id,
                    "content": full_content,
                    "pipeline_id": bridge.pipeline_id,
                    "role": "assistant",
                },
            })

        # 持久化消息到 store（唤醒路径）
        if full_content and thread_id:
            try:
                user_msg_id = (
                    conversation_history[-2]["id"]
                    if conversation_history and len(conversation_history) >= 2 and "id" in conversation_history[-2]
                    else None
                )
                resolved_user_msg_id = user_msg_id or f"user_{message_id}"
                api_store.add_message(
                    thread_id=thread_id,
                    message_id=resolved_user_msg_id,
                    role="user",
                    content=user_content,
                )
                api_store.add_message(
                    thread_id=thread_id,
                    message_id=message_id,
                    role="assistant",
                    content=full_content,
                )
                thinking_text = "".join(drain_result.get("thinking_content_parts", []))
                _exec_storage = ctx.services.get("execution_record_storage") if ctx.services else None
                _persist_messages_to_yaml(
                    _exec_storage, bridge.pipeline_id,
                    resolved_user_msg_id, user_content,
                    message_id, full_content,
                    thinking_content=thinking_text or None,
                )
            except Exception as persist_err:
                logger.warning("唤醒路径持久化消息失败: %s", persist_err)

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
                _valid_roles = {"user", "assistant", "tool"}
                filtered = [
                    msg for msg in engine_messages
                    if isinstance(msg, dict) and msg.get("role") in _valid_roles
                ]
                conversation_history.clear()
                conversation_history.extend(filtered)
                logger.info(
                    "唤醒路径同步 conversation_history: 从引擎同步 %d 条消息 "
                    "(原始 %d 条), history_len=%d",
                    len(filtered), len(engine_messages), len(conversation_history),
                )
            elif full_content:
                conversation_history.append({
                    "role": "assistant",
                    "content": full_content,
                    "id": message_id,
                })
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
    3. 通过 bridge.drain_loop() 消费事件队列并实时发送到前端
    4. 从结果中提取 raw_result 更新对话历史
    5. 通过 bridge.send_new_message 发送最终消息

    Args:
        websocket: WebSocket 连接实例
        user_content: 用户发送的原始文本
        message_id: 本轮回复的消息 UUID
        stop_event: 用于取消流式生成的事件对象
        thread_id: 当前线程/会话 ID
        conversation_history: 对话历史列表（会被就地更新）
        ctx: 管道上下文
    """
    if pre_created_bridge is not None:
        bridge = pre_created_bridge
    else:
        bridge = PipelineStreamBridge(
            pipeline_id=thread_id,
            output_sink=TargetedSink(ws_notifier, thread_id),
            message_id=message_id,
        )
    engine_task: asyncio.Task | None = None
    drain_result: dict = {}

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
        _ws_notifier = ctx.services.get("ws_interaction_notifier") if ctx.services else None
        if _ws_notifier:
            _ws_notifier.register_pipeline_thread(pipeline_id, thread_id)
        else:
            logger.warning(
                "register_pipeline_thread SKIPPED: pipeline=%s thread=%s has_services=%s has_notifier=%s",
                pipeline_id[:12], thread_id[:12],
                ctx.services is not None, _ws_notifier is not None,
            )
    else:
        logger.warning("register_pipeline_thread SKIPPED: pipeline_id is None")

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
        # 启动管道引擎（异步），通过 bridge.on_chunk 接收流式事件
        engine_task = asyncio.create_task(
            ctx.engine.run(
                user_input=user_content,
                agent_config=ctx.agent_config,
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
            await websocket.send_text(json.dumps({"type": "heartbeat_ack"}, ensure_ascii=False))

        # 通过 bridge.drain_loop 消费事件队列并实时发送到前端
        _call_timeout = _get_call_timeout()
        engine_timed_out = False

        try:
            drain_result = await asyncio.wait_for(
                bridge.drain_loop(
                    engine_task,
                    heartbeat_callback=_heartbeat,
                    heartbeat_interval=5.0,
                    suspend_check=lambda: getattr(ctx.engine, "is_suspended", False),
                    call_timeout=_call_timeout,
                ),
                timeout=_call_timeout * 50,
            )
            if drain_result.get("timed_out"):
                engine_timed_out = True
                logger.error("LLM 活动超时 (%ds): pipeline=%s", _call_timeout, message_id)
                engine_task.cancel()
                try:
                    await engine_task
                except (asyncio.CancelledError, Exception):
                    pass
        except asyncio.TimeoutError:
            engine_timed_out = True
            logger.error("管道引擎执行超时 (%ds)，取消任务: message_id=%s", _call_timeout, message_id)
            engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):
                pass

        # 等待引擎完成并获取结果
        if engine_timed_out:
            raise TimeoutError(f"LLM 调用超时（{_call_timeout}s），请稍后重试")

        # 管道挂起时 engine_task 未完成，使用 drain_result 的累积内容
        result = {}
        if engine_task.done():
            result = engine_task.result()
        else:
            logger.info(
                "管道未完成（可能已挂起），使用 drain_result: pipeline=%s",
                str(pipeline_id)[:12] if pipeline_id else "?",
            )

        # 检查是否被中断
        if stop_event.is_set():
            logger.info("流式生成被用户中断: message_id=%s", message_id)

        # 获取完整回复内容
        # 优先使用管道维护的 messages，其次使用 bridge 累积的内容，最后用 raw_result
        final_messages = result.get("messages", [])
        actual_content = ""

        if final_messages:
            # 从最终 messages 中提取最后一条 assistant 消息
            for msg in reversed(final_messages):
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    actual_content = msg.get("content", "")
                    break

            # BUG-FIX-20260511: 过滤管道内部消息，只保留有效对话消息
            # 问题根因: final_messages 包含了管道内部的 system 消息（如
            # [StreamRepetitionGuard]、[ThinkingTruncationGuard] 等），
            # 这些不是正常的对话历史，导致传给 AI 模型的消息数量不一致。
            # 修复方案: 只保留 role 为 user、assistant、tool 的消息。
            _valid_roles = {"user", "assistant", "tool"}
            filtered_messages = [
                msg for msg in final_messages
                if isinstance(msg, dict) and msg.get("role") in _valid_roles
            ]
            conversation_history.clear()
            conversation_history.extend(filtered_messages)
        elif drain_result.get("accumulated_content"):
            actual_content = drain_result["accumulated_content"]
            # BUG-FIX-20260511: 引擎挂起时 final_messages 为空，
            # 但 drain_result 有流式累积内容。需要将 assistant 回复
            # 追加到 conversation_history，确保下一轮历史完整。
            conversation_history.append({
                "role": "assistant",
                "content": actual_content,
                "id": message_id,
            })
        else:
            actual_content = result.get("raw_result", "")

        # ---- stream_chunk: 仅在实时流未发送时补发完整内容 ----
        bridge_accumulated = drain_result.get("accumulated_content", "")
        if actual_content and not bridge_accumulated:
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

        # ---- new_message 最终消息（通过 bridge 发送）----
        await bridge.send_new_message(actual_content, sequence=1)

        # ---- 持久化消息到 store ----
        try:
            user_msg_id = conversation_history[-2]["id"] if len(conversation_history) >= 2 and "id" in conversation_history[-2] else None
            resolved_user_msg_id = user_msg_id or f"user_{message_id}"
            api_store.add_message(
                thread_id=thread_id,
                message_id=resolved_user_msg_id,
                role="user",
                content=user_content,
            )
            api_store.add_message(
                thread_id=thread_id,
                message_id=message_id,
                role="assistant",
                content=actual_content,
            )
            thinking_text = "".join(drain_result.get("thinking_content_parts", []))
            _exec_storage = ctx.services.get("execution_record_storage") if ctx.services else None
            _persist_messages_to_yaml(
                _exec_storage, bridge.pipeline_id,
                resolved_user_msg_id, user_content,
                message_id, actual_content,
                thinking_content=thinking_text or None,
            )
        except Exception as persist_err:
            logger.warning("持久化消息失败: %s", persist_err)

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
            engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):
                pass
        # drain_loop 已发送 stream_end，此处不再重复发送
        raise

    except Exception as exc:
        """管道引擎执行出错。"""
        logger.error("管道引擎执行失败: %s", exc, exc_info=True)
        if engine_task is not None and not engine_task.done():
            engine_task.cancel()
            try:
                await engine_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            error_content = f"抱歉，处理你的消息时出现错误：{exc}"
            await bridge.send_new_message(error_content, sequence=1)
        except Exception:
            pass


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
        logger.error("管道引擎未就绪！消息发送功能将不可用。请检查上方日志中的错误信息。")

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

    @app.websocket("/ws/chat")
    async def websocket_chat_global(websocket: WebSocket) -> None:
        """全局单连接 WebSocket 端点（v3 协议）。"""
        token = websocket.query_params.get("token", "")
        if not token:
            await websocket.close(code=4001, reason="全局连接需要 token 认证")
            return
        payload = verify_token(token)
        if payload is None:
            await websocket.close(code=4001, reason="Token 无效或已过期")
            return
        user_id = payload.get("sub", "")
        if not user_id:
            await websocket.close(code=4001, reason="Token 中缺少用户标识")
            return

        await websocket.accept()
        _ws_interaction_notifier.register_global(user_id, websocket)

        try:
            await websocket.send_text(json.dumps({
                "type": "connection_confirmation",
                "data": {"status": "connected", "mode": "global", "user_id": user_id},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception:
            _ws_interaction_notifier.unregister_global(user_id, websocket)
            return

        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg_data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg_data.get("type", "")
                logger.info("[GlobalWS] 收到消息: type=%s thread_id=%s user=%s", msg_type, msg_data.get("thread_id", "")[:12], user_id[:12])

                if msg_type == "heartbeat":
                    await websocket.send_text(json.dumps({
                        "type": "heartbeat_ack",
                        "data": {"server_time": datetime.now(timezone.utc).isoformat()},
                    }))
                    continue

                thread_id = msg_data.get("thread_id", "")
                if not thread_id:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "data": {"message": f"消息缺少 thread_id: type={msg_type}"},
                    }))
                    continue

                if msg_type == "user_input":
                    # BUG-FIX-fix_20260511_task_worker_global_ws:
                    # 问题根因: /ws/chat 全局端点缺少 TaskWorker 懒启动逻辑，
                    #           导致通过全局 WS 提交的任务没有订阅者，
                    #           pipeline_run_id 永远不会被绑定。
                    # 修复方案: 添加与 /ws/chat/{thread_id} 相同的 TaskWorker 懒启动。
                    global _task_worker_started
                    if not _task_worker_started:
                        _task_worker_started = True
                        try:
                            tw = globals().get("_task_worker")
                            if tw and hasattr(tw, "start"):
                                await tw.start()
                                logger.info("TaskWorker started (global ws /ws/chat)")
                        except Exception as exc:
                            logger.warning("TaskWorker start failed (global ws): %s", exc)

                    msg_data.setdefault("pipeline_id", msg_data.get("pipeline_id", ""))
                    if thread_id not in active_connections:
                        active_connections[thread_id] = []
                    if websocket not in active_connections[thread_id]:
                        active_connections[thread_id].append(websocket)
                    if thread_id not in conversation_histories:
                        conversation_histories[thread_id] = []

                    _ws_interaction_notifier.register(thread_id, websocket)

                    _user_content = msg_data.get("content", "")
                    if not _user_content:
                        continue
                    _msg_id = uuid.uuid4().hex[:12]
                    _pipeline_id = msg_data.get("pipeline_id", "")
                    _stop_evt = asyncio.Event()
                    _history = conversation_histories.get(thread_id, [])

                    if _pipeline_id:
                        from pipeline.message_bus import send_pipeline_message, _find_engine as _find_engine_for_sub
                        _sub_eng, _sub_st = _find_engine_for_sub(_pipeline_id)
                        if _sub_eng and _sub_st == "suspended":
                            _sub_bridge = PipelineStreamBridge(
                                pipeline_id=_pipeline_id,
                                output_sink=TargetedSink(_ws_interaction_notifier, thread_id),
                                message_id=_msg_id,
                            )
                            _sub_eng._saved_on_chunk = _sub_bridge.on_chunk
                            _sub_eng._saved_streaming = True
                            if _sub_eng._suspended_state is not None:
                                _sub_eng._suspended_state["on_chunk"] = _sub_bridge.on_chunk
                                _sub_eng._suspended_state["streaming"] = True
                            sub_result = await send_pipeline_message(_pipeline_id, _user_content)
                            if sub_result.success:
                                asyncio.create_task(
                                    _stream_wake_response(
                                        websocket, _user_content, _msg_id,
                                        _stop_evt, _sub_eng, _pipeline_id,
                                        thread_id=thread_id,
                                        ws_notifier=_ws_interaction_notifier,
                                        conversation_history=_history,
                                        pre_created_bridge=_sub_bridge,
                                    )
                                )
                                continue
                        else:
                            sub_result = await send_pipeline_message(_pipeline_id, _user_content)
                            if sub_result.success:
                                continue
                        await websocket.send_text(json.dumps({
                            "type": "pipeline_error",
                            "data": {"message_id": _msg_id, "error": f"子管道未找到: pipeline={_pipeline_id}", "pipeline_id": _pipeline_id},
                        }, ensure_ascii=False))
                        continue

                    from pipeline.message_bus import _find_engine
                    _main_pid = ""
                    if _pipeline_ctx and _pipeline_ctx.available and _pipeline_ctx.engine:
                        _main_pid = _pipeline_ctx.engine.pipeline_id
                    _ex_eng, _eng_st = _find_engine(_main_pid) if _main_pid else (None, "")

                    if _ex_eng and _eng_st == "suspended":
                        _wake_bridge = PipelineStreamBridge(
                            pipeline_id=thread_id or _main_pid,
                            output_sink=TargetedSink(_ws_interaction_notifier, thread_id),
                            message_id=_msg_id,
                        )
                        _ex_eng._saved_on_chunk = _wake_bridge.on_chunk
                        _ex_eng._saved_streaming = True
                        if _ex_eng._suspended_state is not None:
                            _ex_eng._suspended_state["on_chunk"] = _wake_bridge.on_chunk
                            _ex_eng._suspended_state["streaming"] = True
                        from pipeline.message_bus import send_pipeline_message
                        inject_result = await send_pipeline_message(_main_pid, _user_content)
                        if inject_result.success:
                            asyncio.create_task(
                                _stream_wake_response(
                                    websocket, _user_content, _msg_id,
                                    _stop_evt, _ex_eng, _main_pid,
                                    thread_id=thread_id,
                                    ws_notifier=_ws_interaction_notifier,
                                    conversation_history=_history,
                                    pre_created_bridge=_wake_bridge,
                                )
                            )
                        continue

                    if _pipeline_ctx and _pipeline_ctx.available:
                        _main_sink = TargetedSink(_ws_interaction_notifier, thread_id)
                        _main_bridge = PipelineStreamBridge(
                            pipeline_id=thread_id,
                            output_sink=_main_sink,
                            message_id=_msg_id,
                        )
                        asyncio.create_task(
                            _stream_engine_response(
                                websocket, _user_content, _msg_id,
                                _stop_evt, thread_id, _history, _pipeline_ctx,
                                pre_created_bridge=_main_bridge,
                            )
                        )
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "stream_error",
                            "data": {"message_id": _msg_id, "error": "管道引擎未初始化", "pipeline_id": ""},
                        }, ensure_ascii=False))
                    continue
                elif msg_type == "interaction_response":
                    resp_data = msg_data.get("data", {}) if isinstance(msg_data.get("data"), dict) else msg_data
                    request_id = resp_data.get("request_id", "")
                    if request_id:
                        try:
                            from human_interaction import get_human_interaction_service
                            human_svc = get_human_interaction_service()
                            if human_svc:
                                await human_svc.respond(request_id, resp_data)
                        except Exception as exc:
                            logger.warning("[GlobalWS] interaction_response 处理失败: %s", exc)
                    continue
                elif msg_type == "stop_generation":
                    await websocket.send_text(json.dumps({
                        "type": "state_change",
                        "data": {"status": "stopped"},
                    }))

        except WebSocketDisconnect:
            logger.info("[GlobalWS] 用户断开连接: user=%s", user_id[:12])
        except Exception as exc:
            logger.error("[GlobalWS] 消息循环异常: user=%s err=%s", user_id[:12], exc)
        finally:
            _ws_interaction_notifier.unregister_global(user_id, websocket)
            for tid in list(active_connections.keys()):
                if websocket in active_connections.get(tid, []):
                    active_connections[tid] = [c for c in active_connections[tid] if c != websocket]
                    if not active_connections[tid]:
                        del active_connections[tid]
                        conversation_histories.pop(tid, None)
            _ws_interaction_notifier.unregister_all_for_ws(websocket)

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
                    msg_data = message.get("data") if isinstance(message.get("data"), dict) else message
                    user_content = (
                        msg_data.get("content", "") if isinstance(msg_data, dict)
                        else message.get("content", "")
                    )
                    if not user_content:
                        continue

                    # 子管道路由：前端子Tab发送消息时携带 pipeline_id，
                    # 后端直接用它路由到对应管道。
                    # BUG-FIX-fix_20260511_conversation_mode_no_response:
                    # 问题根因: 旧逻辑通过 task_service 按 task.id 查找 pipeline_run_id，
                    # 但 conversation 模式下前端传的是 pipeline_id 而非 task.id，
                    # 查找失败导致消息被丢弃。
                    # 修复方案: 前端直接传 tab.pipelineRunId 作为 pipeline_id，
                    # 后端直接用它调用 send_pipeline_message。
                    pipeline_id = (
                        msg_data.get("pipeline_id", "") if isinstance(msg_data, dict)
                        else ""
                    )
                    if pipeline_id:
                        from pipeline.message_bus import send_pipeline_message, _find_engine as _find_engine_for_sub

                        _sub_engine, _sub_state = _find_engine_for_sub(pipeline_id)

                        if _sub_engine and _sub_state == "suspended":
                            # 挂起引擎：先创建新 bridge 再唤醒
                            stop_event.set()
                            if current_stream_task and not current_stream_task.done():
                                current_stream_task.cancel()
                                try:
                                    await current_stream_task
                                except asyncio.CancelledError:
                                    pass

                            stop_event = asyncio.Event()
                            _sub_msg_id = uuid.uuid4().hex[:12]
                            _sub_bridge = PipelineStreamBridge(
                                pipeline_id=pipeline_id,
                                output_sink=TargetedSink(_ws_interaction_notifier, thread_id),
                                message_id=_sub_msg_id,
                            )
                            _sub_engine._saved_on_chunk = _sub_bridge.on_chunk
                            _sub_engine._saved_streaming = True
                            if _sub_engine._suspended_state is not None:
                                _sub_engine._suspended_state["on_chunk"] = _sub_bridge.on_chunk
                                _sub_engine._suspended_state["streaming"] = True

                            sub_result = await send_pipeline_message(pipeline_id, user_content)
                            if sub_result.success:
                                logger.info(
                                    "子管道唤醒成功: pipeline_id=%s", pipeline_id[:12],
                                )
                                current_stream_task = asyncio.create_task(
                                    _stream_wake_response(
                                        websocket, user_content, _sub_msg_id,
                                        stop_event, _sub_engine, pipeline_id,
                                        thread_id=thread_id,
                                        ws_notifier=_ws_interaction_notifier,
                                        conversation_history=history,
                                        pre_created_bridge=_sub_bridge,
                                    )
                                )
                                continue
                        else:
                            # 运行中或无引擎：直接注入 / revive
                            sub_result = await send_pipeline_message(pipeline_id, user_content)
                            if sub_result.success:
                                logger.info(
                                    "子管道路由成功: pipeline_id=%s method=%s",
                                    pipeline_id[:12], sub_result.method,
                                )
                                continue

                        await websocket.send_text(json.dumps({
                            "type": "pipeline_error",
                            "data": {
                                "message_id": uuid.uuid4().hex[:12],
                                "error": f"子管道未找到或已退出: pipeline={pipeline_id}",
                                "pipeline_id": pipeline_id,
                            },
                        }, ensure_ascii=False))
                        continue

                    # 检查主管道引擎是否已挂起（多轮对话场景）
                    from pipeline.message_bus import _find_engine
                    _main_pid = ""
                    logger.info(
                        "开始管道状态检查: ctx=%s ctx_avail=%s has_engine=%s",
                        "yes" if _pipeline_ctx else "no",
                        _pipeline_ctx.available if _pipeline_ctx else "no_ctx",
                        "yes" if (_pipeline_ctx and _pipeline_ctx.engine) else "no",
                    )
                    if _pipeline_ctx and _pipeline_ctx.available and _pipeline_ctx.engine:
                        _main_pid = _pipeline_ctx.engine.pipeline_id
                    _existing_engine, _engine_state = _find_engine(_main_pid) if _main_pid else (None, "")
                    logger.info(
                        "管道状态检查: main_pid=%s engine=%s state=%s ctx_avail=%s",
                        str(_main_pid)[:12] if _main_pid else "None",
                        "yes" if _existing_engine else "no",
                        _engine_state or "none",
                        _pipeline_ctx.available if _pipeline_ctx else "no_ctx",
                    )

                    stop_event.set()
                    if current_stream_task and not current_stream_task.done():
                        current_stream_task.cancel()
                        try:
                            await current_stream_task
                        except asyncio.CancelledError:
                            pass

                    stop_event = asyncio.Event()
                    message_id = uuid.uuid4().hex[:12]

                    if _existing_engine and _engine_state == "suspended":
                        # BUG-FIX-20260511: 必须在唤醒引擎之前注入新的 on_chunk。
                        # 时序问题: send_pipeline_message 会唤醒引擎，引擎立刻在另一个
                        # task 中开始处理，如果此时 on_chunk 还是旧的（已关闭的 bridge），
                        # LLM 输出会丢失。必须先注入新的 on_chunk 再唤醒。
                        #
                        # BUG-FIX-fix_20260511_wake_pipeline_id_mismatch:
                        # 问题根因: 唤醒路径的 bridge 使用 _main_pid（引擎 pipeline ID）
                        # 作为 pipeline_id，但第一条消息的 bridge 使用 thread_id（会话 ID）。
                        # 前端按 pipeline_id 路由事件到 pipelineMessageStore，
                        # activePipelineId = sessionId（L1 主管道场景）。
                        # _main_pid ≠ thread_id 导致第二条消息的事件路由到错误的 store key，
                        # 前端看不到 AI 回复（"思考中转圈然后消失"）。
                        # 修复方案: 唤醒路径的 bridge 使用 thread_id，与第一条消息一致。
                        _wake_bridge = PipelineStreamBridge(
                            pipeline_id=thread_id or _main_pid,
                            output_sink=TargetedSink(_ws_interaction_notifier, thread_id),
                            message_id=message_id,
                        )
                        _existing_engine._saved_on_chunk = _wake_bridge.on_chunk
                        _existing_engine._saved_streaming = True
                        if _existing_engine._suspended_state is not None:
                            _existing_engine._suspended_state["on_chunk"] = _wake_bridge.on_chunk
                            _existing_engine._suspended_state["streaming"] = True

                        from pipeline.message_bus import send_pipeline_message
                        inject_result = await send_pipeline_message(_main_pid, user_content)
                        if inject_result.success:
                            current_stream_task = asyncio.create_task(
                                _stream_wake_response(
                                    websocket, user_content, message_id,
                                    stop_event, _existing_engine, _main_pid,
                                    thread_id=thread_id,
                                    ws_notifier=_ws_interaction_notifier,
                                    conversation_history=history,
                                    pre_created_bridge=_wake_bridge,
                                )
                            )
                        continue

                    # 引擎不存在 → 正常启动
                    if _pipeline_ctx and _pipeline_ctx.available:
                        current_stream_task = asyncio.create_task(
                            _stream_engine_response(
                                websocket, user_content, message_id,
                                stop_event, thread_id, history, _pipeline_ctx,
                                ws_notifier=_ws_interaction_notifier,
                            )
                        )
                    else:
                        await websocket.send_text(json.dumps({
                            "type": "stream_error",
                            "data": {
                                "message_id": message_id,
                                "error": "管道引擎未初始化，无法处理消息。请检查服务器日志并重启。",
                                "pipeline_id": "",
                            },
                        }, ensure_ascii=False))
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
            # 从 WebSocket 交互通知器注销
            _ws_interaction_notifier.unregister(thread_id, websocket)
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
