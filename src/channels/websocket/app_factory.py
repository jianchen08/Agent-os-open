"""FastAPI 应用工厂和服务器入口。

创建合并了 WebSocket 功能的 FastAPI 应用，提供服务器启动入口。

从 start_server.py 拆分而来，保持向后兼容。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# PYTHONPATH 已在 Dockerfile/环境变量中设置为 /app/src，无需 sys.path.insert

# 加载 .env 文件中的环境变量（API Key 等敏感配置）
# 必须在所有其他导入之前完成，确保后续模块能正确读取环境变量
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path, override=False)
        logging.getLogger(__name__).info("已加载 .env 文件: %s", _env_path)
except ImportError:
    pass  # python-dotenv 未安装时跳过

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from channels.api.app import create_app
from channels.api.auth import verify_token
from channels.api.memory_store import store as api_store

from pipeline.stream_bridge import TargetedSink, create_targeted_sink

from channels.websocket.ws_handler import ws_interaction_notifier
from channels.websocket import stream_handler

from channels.websocket.stream_handler import (
    PipelineContext,
    _init_pipeline_context,
)
from channels.websocket.static_files import mount_media_static_files
from pipeline.message_handler import parse_frontend_message, MessageParseError
from pipeline.message_bus import handle_incoming_message

# 配置日志（统一到 src.core.logging）
from src.core.logging import setup_logging as _setup_unified_logging, LoggingConfig, StructuredFormatter

_setup_unified_logging(LoggingConfig(output="console"), reset=True)

# EventBus 调试文件（使用统一格式化器）
_fh = logging.FileHandler("debug_eventbus.log", encoding="utf-8", mode="w")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(StructuredFormatter())
logging.getLogger().addHandler(_fh)

logger = logging.getLogger(__name__)

_pipeline_ctx: PipelineContext | None = None
_task_worker_started: bool = False


async def _resolve_sub_pipeline_agent_config(pipeline_id: str) -> Any | None:
    """通过 pipeline_run_id 查找关联任务的 agent_config。

    子管道引擎完成 run() 后注册被清理，send_pipeline_message 走 _try_revive_pipeline
    复活路径时，需要正确的 agent_config 才能使用子Agent而非主Agent。

    查找链路（按优先级）：
    1. 引擎注册表 tags.task_id → task_service.get_task → task.target_id → agent_registry
    2. task_service.get_all_tasks() 全量扫描（引擎已注销时的回退）

    Args:
        pipeline_id: 子管道的 pipeline_run_id

    Returns:
        对应的 AgentConfig 实例，未找到返回 None
    """
    if not pipeline_id:
        return None

    try:
        from infrastructure.service_provider import get_service_provider
        _sp = get_service_provider()
    except Exception:
        return None

    task_service = _sp.get("task_service") if _sp else None
    agent_registry = _sp.get("agent_registry") if _sp else None
    if not task_service or not agent_registry:
        return None

    import contextlib

    # ── 路径1：引擎注册表 tags 直接查 task_id ──
    from pipeline.registry import get_engine_registry
    _entry = get_engine_registry().get(pipeline_id)
    if _entry is not None and _entry.tags.get("task_id"):
        task_id = _entry.tags["task_id"]
        with contextlib.suppress(Exception):
            task = task_service.get_task(task_id)
            if task:
                target_id = getattr(task, "target_id", None)
                if target_id:
                    agent_config = agent_registry.get(target_id)
                    if agent_config:
                        logger.info(
                            "[GlobalWS] 子管道 agent_config 解析成功 (via registry tags): "
                            "pipeline=%s task=%s target_id=%s",
                            pipeline_id[:12], task_id[:12], target_id,
                        )
                        return agent_config

    # ── 路径2：全量扫描（引擎已注销，但 task.pipeline_run_id 仍存在）──
    try:
        all_tasks = task_service.get_all_tasks()
        for task in all_tasks:
            if getattr(task, "pipeline_run_id", None) == pipeline_id:
                target_id = getattr(task, "target_id", None)
                if target_id:
                    agent_config = agent_registry.get(target_id)
                    if agent_config:
                        logger.info(
                            "[GlobalWS] 子管道 agent_config 解析成功 (via full scan): "
                            "pipeline=%s task=%s target_id=%s",
                            pipeline_id[:12], getattr(task, "id", "")[:12], target_id,
                        )
                        return agent_config
    except Exception as exc:
        logger.warning("[GlobalWS] 子管道 agent_config 全量扫描失败: %s", exc)

    return None


def _resolve_agent_from_thread(thread_id: str) -> Any | None:
    """从线程/会话的 agent_id 字段解析 Agent 配置。

    这是"切换 Agent"功能的核心：前端通过 PATCH /api/v1/threads/{id}/agent
    将选中的 agent_id 写入线程存储后，此处根据该 ID 查找对应的 Agent 配置。

    查找优先级：
    1. api_store.get_thread(thread_id).agent_id → agent_registry.get(config_id)
    2. api_store.get_thread(thread_id).agent_id → agent_registry.get(id)
    3. 都找不到返回 None

    Args:
        thread_id: 线程 ID

    Returns:
        对应的 AgentConfig 实例，未找到返回 None
    """
    if not thread_id:
        return None

    try:
        thread = api_store.get_thread(thread_id)
    except Exception:
        return None

    if not thread:
        return None

    agent_id = thread.get("agent_id")
    if not agent_id:
        return None

    try:
        from infrastructure.service_provider import get_service_provider
        _sp = get_service_provider()
        agent_registry = _sp.get("agent_registry") if _sp else None
        if not agent_registry:
            from agents.global_registry import get_global_agent_registry_sync
            agent_registry = get_global_agent_registry_sync()
    except Exception:
        return None

    if not agent_registry:
        return None

    # 按 config_id 查找（如 lingxi）
    agent_config = agent_registry.get(agent_id)
    if agent_config:
        logger.info(
            "[GlobalWS] 从线程 agent_id 解析 Agent: thread=%s agent_id=%s config_id=%s",
            thread_id[:12], agent_id, getattr(agent_config, 'config_id', '?'),
        )
        return agent_config

    # fallback: 按 id 查找
    for cfg in agent_registry.list_all():
        if getattr(cfg, 'id', None) == agent_id:
            logger.info(
                "[GlobalWS] 从线程 agent_id 解析 Agent (by id): thread=%s agent_id=%s",
                thread_id[:12], agent_id,
            )
            return cfg

    logger.warning(
        "[GlobalWS] 线程 agent_id=%s 在 registry 中未找到，返回 None（调用方需 fail-closed）: thread=%s",
        agent_id, thread_id[:12],
    )
    return None


def _get_call_timeout() -> float:
    """获取管道调用超时时间（秒）。"""
    try:
        from infrastructure.service_provider import get_service_provider
        _sp = get_service_provider()
        _timeout = _sp.get("call_timeout") if _sp else None
        if _timeout:
            return float(_timeout)
    except Exception:
        pass
    return 120.0


def create_combined_app() -> FastAPI:
    """创建合并了 WebSocket 功能的 FastAPI 应用。

    将 WebSocket 路由注册到 FastAPI 中，实现单端口统一服务。
    在应用创建时初始化管道引擎上下文。

    Returns:
        配置好的 FastAPI 应用实例
    """
    global _pipeline_ctx

    # 初始化管道引擎上下文（需要在创建 lifespan 之前完成，因为 lifespan 中要用到 _task_worker）
    _pipeline_ctx = _init_pipeline_context()
    if _pipeline_ctx.available:
        logger.info("管道引擎已就绪，WebSocket 将使用真实 AI 回复")
    else:
        logger.error("管道引擎未就绪！消息发送功能将不可用。请检查上方日志中的错误信息。")

    @asynccontextmanager
    async def _combined_lifespan(app: FastAPI):
        """应用生命周期管理，替代已弃用的 on_event('startup')。

        在应用启动时立即启动 TaskWorker，确保纯 API 场景下任务可正常执行。
        """
        global _task_worker_started
        logger.info(
            "[Lifespan] 应用启动开始 | pid=%d | loop_id=%s",
            os.getpid(), id(asyncio.get_running_loop()),
        )
        if not _task_worker_started:
            tw = getattr(stream_handler, "_task_worker", None)
            if tw and hasattr(tw, "start"):
                try:
                    await tw.start()
                    _task_worker_started = True
                    logger.info("TaskWorker started (app startup)")
                except Exception as exc:
                    logger.warning("TaskWorker start failed (app startup): %s", exc, exc_info=True)

        try:
            from pipeline.message_bus import restore_pipelines_on_startup
            count = await restore_pipelines_on_startup()
            if count:
                logger.info("启动恢复: 已恢复 %d 个管道", count)
        except Exception as exc:
            logger.debug("restore_pipelines_on_startup skipped: %s", exc)

        try:
            from triggers.manager import get_trigger_manager
            main_loop = asyncio.get_running_loop()
            get_trigger_manager().set_main_loop(main_loop)
            asyncio._main_loop_ref = main_loop
            logger.info("[Lifespan] 主事件循环已保存: loop_id=%s", id(main_loop))
        except Exception as exc:
            logger.debug("TriggerManager set_main_loop skipped: %s", exc)

        # 启动 ConfigCenter 文件监听（异步）
        _config_center_started = False
        try:
            from config.config_center import get_config_center
            center = get_config_center()
            # create_task 调度 start()，但非阻塞——需等待 start() 内部
            # _running 置位完成，否则下游 PluginHotReloader 检测 is_running
            # 会因时序竞态拿到 False 而走 fallback 分支（双监听）。
            # 注意：禁止预置 center._running=True，那会让 start() 早退、
            asyncio.create_task(center.start())
            _config_center_started = await center.wait_ready(timeout=5.0)
            if _config_center_started:
                logger.info("[Lifespan] ConfigCenter 文件监听已启动")
            else:
                logger.warning(
                    "[Lifespan] ConfigCenter 启动超时或失败（热重载将不可用）"
                )
        except Exception as exc:
            logger.warning("[Lifespan] ConfigCenter 启动失败（热重载将不可用）: %s", exc, exc_info=True)

        # 启动 PluginHotReloader（用全局单例 registry，订阅 ConfigCenter 回调）
        _hot_reloader = None
        try:
            from plugins.hot_reload import PluginHotReloader
            from agents.global_registry import get_global_agent_registry_sync
            from tools.global_registry import get_global_tool_registry_sync
            from channels.api.routes_plugins import set_hot_reloader

            _hot_reloader = PluginHotReloader(
                config_dir="config",
                agent_registry=get_global_agent_registry_sync(),
                tool_registry=get_global_tool_registry_sync(),
            )
            _hot_reloader.start()  # ConfigCenter 运行时自动走订阅模式
            set_hot_reloader(_hot_reloader)  # 供 API 路由查询状态/历史
            logger.info("[Lifespan] PluginHotReloader 已启动")
        except Exception as exc:
            logger.warning("[Lifespan] PluginHotReloader 启动失败: %s", exc, exc_info=True)

        try:
            yield
        finally:
            # 停止 PluginHotReloader
            if _hot_reloader is not None:
                try:
                    _hot_reloader.stop()
                    logger.info("[Lifespan] PluginHotReloader 已停止")
                except Exception:
                    pass
            # 停止 ConfigCenter 文件监听
            try:
                from config.config_center import get_config_center
                get_config_center().stop()
                logger.info("[Lifespan] ConfigCenter 已停止")
            except Exception:
                pass
            logger.info(
                "[Lifespan] 应用关闭 | pid=%d | loop_id=%s",
                os.getpid(), id(asyncio.get_running_loop()),
            )

    app = create_app(lifespan=_combined_lifespan)

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
        ws_interaction_notifier.register_global(user_id, websocket)

        try:
            await websocket.send_text(json.dumps({
                "type": "connection_confirmation",
                "data": {"status": "connected", "mode": "global", "user_id": user_id},
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception:
            ws_interaction_notifier.unregister_global(user_id, websocket)
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
                    global _task_worker_started
                    if not _task_worker_started:
                        _task_worker_started = True
                        try:
                            tw = getattr(stream_handler, "_task_worker", None)
                            if tw and hasattr(tw, "start"):
                                await tw.start()
                                logger.info("TaskWorker started (global ws /ws/chat)")
                        except Exception as exc:
                            logger.warning("TaskWorker start failed (global ws): %s", exc)

                    if thread_id not in active_connections:
                        active_connections[thread_id] = []
                    if websocket not in active_connections[thread_id]:
                        active_connections[thread_id].append(websocket)
                    if thread_id not in conversation_histories:
                        conversation_histories[thread_id] = []

                    ws_interaction_notifier.register(thread_id, websocket)

                    try:
                        _pipeline_msg = parse_frontend_message(msg_data)
                    except MessageParseError as _parse_err:
                        logger.warning("[GlobalWS] 消息解析失败: %s", _parse_err.reason)
                        continue

                    if _pipeline_msg.is_empty:
                        continue

                    _msg_id = uuid.uuid4().hex[:12]

                    # pipeline_id 是唯一路由标识，前端必须传 pipeline_id
                    _target_pid = _pipeline_msg.pipeline_id
                    if not _target_pid:
                        logger.error(
                            "user_input 缺少 pipeline_id，拒绝路由: thread_id=%s content=%.30s",
                            thread_id, _pipeline_msg.content[:30],
                        )
                        continue

                    # P0-安全: 执行者必须显式确定，禁止静默降级到默认 Agent（灵汐）。
                    # 子管道任务关联的 Agent → 线程选中的 Agent，两层都解析不到即 fail-closed。
                    _agent_config = await _resolve_sub_pipeline_agent_config(_target_pid)
                    if _agent_config is None:
                        _agent_config = _resolve_agent_from_thread(thread_id)
                    if _agent_config is None:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "data": {
                                "message": (
                                    "未配置执行 Agent（子管道与线程均无可用 Agent），"
                                    "禁止静默降级到默认 Agent。thread_id=" + thread_id
                                ),
                                "code": "NO_AGENT_CONFIGURED",
                            },
                        }, ensure_ascii=False))
                        continue
                    # 创建 output_sink
                    _sink = create_targeted_sink(ws_interaction_notifier, thread_id) if _pipeline_ctx and _pipeline_ctx.available else None

                    _history = conversation_histories.get(thread_id, [])

                    from pipeline.registry import get_engine_registry
                    _registry = get_engine_registry()

                    if _target_pid and thread_id:
                        _existing_entry = _registry.get(_target_pid)
                        if _existing_entry and not _existing_entry.thread_id:
                            _existing_entry.thread_id = thread_id

                    if not _target_pid or not _registry.get(_target_pid):
                        _sess = api_store.get_session(thread_id)
                        _new_pid = _target_pid or ""
                        if not _new_pid and _pipeline_ctx and _pipeline_ctx.available and _pipeline_ctx.engine:
                            _new_pid = _pipeline_ctx.engine.pipeline_id
                        if not _registry.get(_new_pid):
                            _provider = None
                            try:
                                from infrastructure.service_provider import get_service_provider
                                _provider = get_service_provider()
                            except Exception:
                                pass
                            _reg_result = _registry.register_pipeline(
                                pipeline_id=_new_pid,
                                thread_id=thread_id,
                                tags={"mode": "interactive", "channel": "ws", "session_id": thread_id},
                                input_route_table=_provider.get("input_route_table") if _provider else None,
                                output_route_table=_provider.get("output_route_table") if _provider else None,
                                plugin_registry=_provider.get("plugin_registry") if _provider else None,
                                services=_provider.get_all_services() if _provider else {},
                            )
                            if _reg_result:
                                _target_pid = _reg_result.engine.pipeline_id
                                _pipeline_msg.pipeline_id = _target_pid
                                if _sess and not _sess.active_pipeline_id:
                                    _sess.active_pipeline_id = _target_pid

                    if _target_pid:
                        _existing_entry = _registry.get(_target_pid)
                        if _existing_entry and not _existing_entry.thread_id:
                            _registry.update_thread_id(_target_pid, thread_id)

                    # ── 使用新入口注入标准消息对象 ──
                    _result = await handle_incoming_message(
                        _pipeline_msg,
                        agent_config=_agent_config,
                        output_sink=_sink,
                        conversation_history=_history if _history else None,
                    )

                    if _result.success:
                        continue

                    await websocket.send_text(json.dumps({
                        "type": "stream_error",
                        "data": {"message_id": _msg_id, "error": _result.error or "管道不可用", "pipeline_id": _target_pid},
                    }, ensure_ascii=False))
                    continue
                elif msg_type == "interaction_response":
                    try:
                        _pipeline_msg = parse_frontend_message(msg_data)
                    except MessageParseError as _parse_err:
                        logger.warning("[GlobalWS] interaction_response 消息解析失败: %s", _parse_err.reason)
                        continue
                    resp_data = _pipeline_msg.metadata
                    request_id = resp_data.get("request_id", "")
                    logger.info(
                        "[GlobalWS] 收到 interaction_response | request_id=%s | data_keys=%s",
                        request_id, list(resp_data.keys()) if isinstance(resp_data, dict) else "non-dict",
                    )
                    if request_id:
                        try:
                            from human_interaction import get_human_interaction_service
                            human_svc = get_human_interaction_service()
                            if human_svc:
                                respond_result = await human_svc.respond(request_id, resp_data)
                                logger.info(
                                    "[GlobalWS] human_svc.respond 返回 | request_id=%s | result=%s",
                                    request_id, respond_result,
                                )
                                request_record = await human_svc.get_request(request_id)
                                if request_record:
                                    pipeline_id = request_record.get("session_id", "")
                                    logger.info(
                                        "[GlobalWS] 交互请求记录 | request_id=%s | session_id=%s | status=%s",
                                        request_id, pipeline_id, request_record.get("status"),
                                    )
                                    if pipeline_id and not pipeline_id.startswith("__eval__"):
                                        from pipeline.message_bus import _find_engine
                                        engine, _ = _find_engine(pipeline_id)
                                        if engine and hasattr(engine, "wake"):
                                            engine.wake()
                                            logger.info(
                                                "[GlobalWS] 用户交互响应已处理，唤醒 pipeline | "
                                                "request_id=%s | pipeline_id=%s",
                                                request_id, pipeline_id,
                                            )
                                    elif pipeline_id.startswith("__eval__"):
                                        logger.info(
                                            "[GlobalWS] 评估交互响应已处理（纯Event，无管道唤醒） | "
                                            "request_id=%s | session_id=%s",
                                            request_id, pipeline_id,
                                        )
                                else:
                                    logger.warning(
                                        "[GlobalWS] 交互请求记录未找到 | request_id=%s",
                                        request_id,
                                    )
                            else:
                                logger.warning("[GlobalWS] human_svc 为 None，无法处理交互响应")
                        except Exception as exc:
                            logger.warning("[GlobalWS] interaction_response 处理失败: %s", exc, exc_info=True)
                    else:
                        logger.warning("[GlobalWS] interaction_response 缺少 request_id")
                    continue
                elif msg_type == "stop_generation":
                    logger.info("[GlobalWS] 用户请求停止生成: thread_id=%s user=%s", thread_id[:12], user_id[:12])

                    # 1. 通过 Registry 取消关联管道的 drain_task

                    # 必须携带 pipeline_id 精确停止，管道ID是唯一路由标识
                    _stop_msg = parse_frontend_message(msg_data)
                    _pipeline_id = _stop_msg.pipeline_id
                    if not _pipeline_id:
                        logger.error(
                            "stop_generation 缺少 pipeline_id，拒绝停止: thread_id=%s",
                            thread_id,
                        )
                        continue
                    _all_pipeline_ids: set[str] = {_pipeline_id}
                    try:
                        from pipeline.message_bus import _find_engine
                        from pipeline.registry import get_engine_registry as _get_reg
                        for _pid in _all_pipeline_ids:
                            # 即时停止后台 drain_loop（bridge.stop 哨兵 + task.cancel）
                            _get_reg().cancel_drain_task(_pid)
                            _eng, _st = _find_engine(_pid)
                            if _eng:
                                # 取消引擎的挂起状态
                                if hasattr(_eng, "_suspended_state") and _eng._suspended_state is not None:
                                    _eng._suspended_state["ended"] = True
                                # 唤醒引擎使其退出挂起等待
                                if hasattr(_eng, "_wake_event") and _eng._wake_event is not None:
                                    _eng._wake_event.set()
                                if _pipeline_ctx and hasattr(_pipeline_ctx, "_engines"):
                                    _pipeline_ctx._engines.pop(_pid, None)
                                logger.info("[GlobalWS] 已取消管道引擎并清理缓存: pipeline=%s state=%s", _pid[:12], _st)
                    except Exception as _eng_err:
                        logger.warning("[GlobalWS] 取消管道引擎时出错: %s", _eng_err)

                    # 3. 尝试取消 TaskWorker 中与该 thread_id 关联的后台任务
                    try:
                        tw = getattr(stream_handler, "_task_worker", None)
                        if tw and hasattr(tw, "_task_id_to_bg_task") and hasattr(tw, "_task_service"):
                            _task_svc = getattr(tw, "_task_service", None)
                            if _task_svc:
                                # 遍历活跃任务，查找与 thread_id 关联的 task
                                for _active_tid in list(getattr(tw, "_active_tasks", set())):
                                    try:
                                        _t = _task_svc.get_task(_active_tid)
                                        if _t:
                                            # 通过 task 的 pipeline_run_id 或 parent_pipeline_id
                                            # 关联到 thread_id 对应的管道
                                            _t_pipeline = getattr(_t, "pipeline_run_id", "") or ""
                                            _t_parent = getattr(_t, "parent_pipeline_id", "") or ""
                                            if _t_pipeline in _all_pipeline_ids or _t_parent in _all_pipeline_ids:
                                                try:
                                                    await _task_svc.fail_task(_active_tid, reason=f"用户取消: {_stop_msg.metadata.get('reason', 'stop_generation')}")
                                                except Exception as _ft_err:
                                                    logger.warning("[GlobalWS] fail_task 失败(仍将继续取消): task=%s, err=%s", _active_tid[:12], _ft_err)
                                                tw.cancel_pipeline(_active_tid)
                                                logger.info("[GlobalWS] 已取消 TaskWorker 后台任务: task=%s", _active_tid[:12])
                                    except Exception:
                                        pass
                    except Exception as _tw_err:
                        logger.warning("[GlobalWS] 取消 TaskWorker 任务时出错: %s", _tw_err)

                    await websocket.send_text(json.dumps({
                        "type": "state_change",
                        "data": {"status": "stopped", "thread_id": thread_id},
                    }))

        except WebSocketDisconnect:
            logger.info("[GlobalWS] 用户断开连接: user=%s", user_id[:12])
        except Exception as exc:
            logger.error("[GlobalWS] 消息循环异常: user=%s err=%s", user_id[:12], exc)
        finally:
            ws_interaction_notifier.unregister_global(user_id, websocket)
            for tid in list(active_connections.keys()):
                if websocket in active_connections.get(tid, []):
                    active_connections[tid] = [c for c in active_connections[tid] if c != websocket]
                    if not active_connections[tid]:
                        del active_connections[tid]
                        conversation_histories.pop(tid, None)
            ws_interaction_notifier.unregister_all_for_ws(websocket)

    # 挂载媒体文件静态服务（必须放在所有路由注册之后）
    mount_media_static_files(app)

    return app


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def find_available_port(start_port: int, host: str = "0.0.0.0") -> int:
    """从 start_port 开始查找可用端口。

    通过尝试绑定端口来判断是否可用，如果指定端口被占用则依次递增，
    最多搜索到 start_port + 100。

    Args:
        start_port: 起始端口号
        host: 绑定地址，默认 0.0.0.0

    Returns:
        第一个可用的端口号

    Raises:
        RuntimeError: 在搜索范围内没有可用端口
    """
    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            logger.debug("端口 %d 已被占用，尝试下一个", port)
            continue
    raise RuntimeError(f"在端口 {start_port}-{start_port + 99} 范围内没有可用端口")


def _cleanup_ghost_running_tasks() -> int:
    """清理服务重启后残留的幽灵任务。

    委托给 TaskService.cleanup_ghost_tasks() 静态方法，
    app_factory.py 只负责传入数据目录，不持有任务生命周期逻辑。
    """
    import asyncio
    from tasks.service import TaskService

    _data_dir = str(Path(__file__).resolve().parents[3] / "data" / "tasks")
    cleaned, cascaded = asyncio.run(TaskService.cleanup_ghost_tasks(_data_dir))
    return cleaned


def main() -> None:
    """主函数，启动 uvicorn 服务器。

    端口优先级：
    1. 命令行参数 --port
    2. 环境变量 BACKEND_PORT
    3. 默认值 8888

    如果指定端口被占用，自动查找下一个可用端口。
    """
    parser = argparse.ArgumentParser(description="Agent OS 服务器")
    parser.add_argument("--port", type=int, default=None, help="后端服务端口")
    args = parser.parse_args()

    default_port = 8888
    preferred_port = args.port or int(os.environ.get("BACKEND_PORT", default_port))

    actual_port = find_available_port(preferred_port)

    if actual_port != preferred_port:
        logger.warning(
            "端口 %d 已被占用，自动切换到 %d", preferred_port, actual_port,
        )

    logger.info("正在启动 Agent OS 服务器...")
    logger.info("API 地址: http://localhost:%d", actual_port)
    logger.info("API 文档: http://localhost:%d/docs", actual_port)
    logger.info("健康检查: http://localhost:%d/health", actual_port)

    os.environ["BACKEND_PORT"] = str(actual_port)

    # 启动前清理幽灵 running 任务（引擎已死但 YAML 仍为 running）
    _cleanup_ghost_running_tasks()

    app = create_combined_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=actual_port,
        log_level="info",
        timeout_keep_alive=120,
        ws_ping_interval=30.0,
        ws_ping_timeout=60.0,
    )


if __name__ == "__main__":
    main()
