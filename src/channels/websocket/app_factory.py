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
import sys  # noqa: F401
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# PYTHONPATH 已在 Dockerfile/环境变量中设置为 /app/src，无需 sys.path.insert

# 加载 .env 文件中的环境变量（API Key 等敏感配置）
# 必须在所有其他导入之前完成，确保后续模块能正确读取环境变量
try:
    from dotenv import load_dotenv
    _env_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")  # noqa: PTH120
    if os.path.exists(_env_path):  # noqa: PTH110
        load_dotenv(_env_path, override=False)
        logging.getLogger(__name__).info("已加载 .env 文件: %s", _env_path)
except ImportError:
    pass  # python-dotenv 未安装时跳过

from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from channels.api.app import create_app
from channels.api.auth import verify_token
from channels.api.memory_store import store as api_store  # noqa: F401
from channels.websocket import stream_handler
from channels.websocket.static_files import mount_media_static_files
from channels.websocket.stream_handler import (
    PipelineContext,
    _init_pipeline_context,
)
from channels.websocket.ws_handler import ws_interaction_notifier
from pipeline.message_bus import send_pipeline_message
from pipeline.message_handler import MessageParseError, parse_frontend_message
from pipeline.stream_bridge import TargetedSink, create_targeted_sink  # noqa: F401

# 配置日志 — 接入统一日志系统（支持结构化输出 + JSON 格式 + 链路追踪）
from src.core.logging import LogContext, LoggingConfig, setup_logging as _setup_unified_logging

_setup_unified_logging(LoggingConfig.from_env(), reset=True)

logger = logging.getLogger(__name__)

_pipeline_ctx: PipelineContext | None = None
_task_worker_started: bool = False



def _get_call_timeout() -> float:
    """获取管道调用超时时间（秒）。"""
    try:
        from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
        _sp = get_service_provider()
        _timeout = _sp.get("call_timeout") if _sp else None
        if _timeout:
            return float(_timeout)
    except Exception:
        pass
    return 120.0


def create_combined_app() -> FastAPI:  # noqa: PLR0915
    """创建合并了 WebSocket 功能的 FastAPI 应用。

    将 WebSocket 路由注册到 FastAPI 中，实现单端口统一服务。
    在应用创建时初始化管道引擎上下文。

    Returns:
        配置好的 FastAPI 应用实例
    """
    global _pipeline_ctx  # noqa: PLW0603

    # 初始化管道引擎上下文（需要在创建 lifespan 之前完成，因为 lifespan 中要用到 _task_worker）
    _pipeline_ctx = _init_pipeline_context()
    if _pipeline_ctx.available:
        logger.info("管道引擎已就绪，WebSocket 将使用真实 AI 回复")
    else:
        logger.error("管道引擎未就绪！消息发送功能将不可用。请检查上方日志中的错误信息。")

    @asynccontextmanager
    async def _combined_lifespan(app: FastAPI):  # noqa: PLR0912,PLR0915
        """应用生命周期管理，替代已弃用的 on_event('startup')。

        在应用启动时立即启动 TaskWorker，确保纯 API 场景下任务可正常执行。
        """
        global _task_worker_started  # noqa: PLW0603
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
            from pipeline.message_bus import restore_pipelines_on_startup  # noqa: PLC0415
            count = await restore_pipelines_on_startup()
            if count:
                logger.info("启动恢复: 已恢复 %d 个管道", count)
        except Exception as exc:
            logger.debug("restore_pipelines_on_startup skipped: %s", exc)

        # 会话系统启动恢复：从 api_store 注册所有会话管道（含 agent_id）
        try:
            from channels.api.routes_threads import restore_session_pipelines  # noqa: PLC0415
            _session_count = restore_session_pipelines()
        except Exception as exc:
            logger.debug("restore_session_pipelines skipped: %s", exc)

        try:
            from triggers.manager import get_trigger_manager  # noqa: PLC0415
            main_loop = asyncio.get_running_loop()
            get_trigger_manager().set_main_loop(main_loop)
            asyncio._main_loop_ref = main_loop
            logger.info("[Lifespan] 主事件循环已保存: loop_id=%s", id(main_loop))
        except Exception as exc:
            logger.debug("TriggerManager set_main_loop skipped: %s", exc)

        # 启动 ConfigCenter 文件监听（异步）
        _config_center_started = False
        try:
            from config.config_center import get_config_center  # noqa: PLC0415
            center = get_config_center()
            # create_task 调度 start()，但非阻塞——需等待 start() 内部
            # _running 置位完成，否则下游 PluginHotReloader 检测 is_running
            # 会因时序竞态拿到 False 而走 fallback 分支（双监听）。
            # 注意：禁止预置 center._running=True，那会让 start() 早退、
            # watchfiles 监听循环永不启动（BUG-FIX-fix_20260614_config_center_running）。
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
            from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415
            from channels.api.routes_plugins import set_hot_reloader  # noqa: PLC0415
            from plugins.hot_reload import PluginHotReloader  # noqa: PLC0415
            from tools.global_registry import get_global_tool_registry_sync  # noqa: PLC0415

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
                from config.config_center import get_config_center  # noqa: PLC0415
                get_config_center().stop()
                logger.info("[Lifespan] ConfigCenter 已停止")
            except Exception:
                pass
            logger.info(
                "[Lifespan] 应用关闭 | pid=%d | loop_id=%s",
                os.getpid(), id(asyncio.get_running_loop()),
            )

    # BUG-FIX-fix_20260524_lifespan_not_called:
    # 问题根因: 之前使用 app.router.lifespan_context = _combined_lifespan 设置 lifespan，
    #          但这种方式在当前 FastAPI 版本中不会生效，uvicorn 启动时不会调用 lifespan。
    #          导致 TaskWorker.start() 从未被调用，task.submitted 事件无人监听，
    #          任务永远停留在 pending 状态。
    # 修复方案: 将 lifespan 作为参数传递给 FastAPI() 构造函数（通过 create_app(lifespan=...)），
    #          确保 uvicorn 在应用启动时正确调用 lifespan 上下文管理器。
    # 影响范围: 所有通过前端提交的子任务（task_submit）
    # 修复日期: 2026-05-24
    app = create_app(lifespan=_combined_lifespan)

    # WebSocket 连接管理
    # BUG-FIX-fix_20260624_ws_double_ledger:
    # 历史上这里维护了一个本地 active_connections，与
    # ws_interaction_notifier._active_connections 是两本独立账本，
    # 清理逻辑分裂导致：本地清掉了 notifier 还残留 / 反之亦然，
    # 体感上是「连接看起来活着但收不到推送」或「死连接还被推消息」。
    # 现在直接复用 notifier 内部字典，单源真理。
    active_connections: dict[str, list[WebSocket]] = ws_interaction_notifier._active_connections

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
    async def websocket_chat_global(websocket: WebSocket) -> None:  # noqa: PLR0912,PLR0915
        """全局单连接 WebSocket 端点（v3 协议）。"""
        token = websocket.query_params.get("token", "")
        # BUG-FIX-fix_20260625_ws_handshake_close_code_lost:
        # 问题根因: 原实现在 accept() 之前 close(code=4001)。Starlette 在握手阶段
        #   拒绝时，浏览器拿不到 WebSocket close frame，只收到 HTTP 403，
        #   ws.onclose 的 event.code 是 1006（Abnormal Closure）而非 4001。
        #   前端 GlobalWebSocket._scheduleReconnect 据此判断是否需要刷新 token，
        #   但 1006 !== 4001 → 认证拒绝被误判为普通断连 → 用过期 token 死循环重连。
        # 修复方案: token 校验失败时先 accept()，再 close(4001)。这样 close frame
        #   能正确送达浏览器，前端 event.code===4001 判断成立，触发 token 刷新。
        if not token:
            await websocket.accept()
            await websocket.close(code=4001, reason="全局连接需要 token 认证")
            return
        payload = verify_token(token)
        if payload is None:
            await websocket.accept()
            await websocket.close(code=4001, reason="Token 无效或已过期")
            return
        user_id = payload.get("sub", "")
        if not user_id:
            await websocket.accept()
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
        except WebSocketDisconnect:
            # BUG-FIX-fix_20260624_confirmation_disconnect:
            # 前端刚 accept 就刷新页面的常见竞态：accept 成功 → send 时对端已关。
            # 这是正常断开（不是异常），用 info 记录不污染 ERROR 日志。
            logger.info("[GlobalWS] 客户端在握手确认前已断开: user=%s", user_id[:12])
            ws_interaction_notifier.unregister_global(user_id, websocket)
            return
        except Exception as exc:
            logger.error(
                "[GlobalWS] 发送 connection_confirmation 失败: user=%s err=%s",
                user_id[:12], exc,
            )
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
                # heartbeat 高频轮询，不记日志
                if msg_type != "heartbeat":
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

                # 绑定链路追踪上下文，使后续日志可关联到当前会话
                LogContext.bind(
                    request_id=msg_data.get("request_id", uuid.uuid4().hex[:12]),
                    session_id=thread_id,
                    thread_id=thread_id,
                    trace_id=msg_data.get("trace_id", ""),
                )

                if msg_type == "user_input":
                    # BUG-FIX-fix_20260511_task_worker_global_ws:
                    # 问题根因: /ws/chat 全局端点缺少 TaskWorker 懒启动逻辑，
                    #           导致通过全局 WS 提交的任务没有订阅者，
                    #           pipeline_run_id 永远不会被绑定。
                    # 修复方案: 添加与 /ws/chat/{thread_id} 相同的 TaskWorker 懒启动。
                    global _task_worker_started  # noqa: PLW0603
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

                    # WS 入口只做转发：pipeline_id → 注册表找引擎 → 转发消息。
                    # agent 决策完全由数据源决定（不在此解析）：
                    # - 主管道：会话映射 thread["agent_id"]（注册时写入 tags，重建时从会话读）
                    # - 子任务管道：任务数据 task.metadata["target_id"]
                    # 引擎 idle 重启用引擎自带 _agent_config；revive 从持久化数据重建。
                    _sink = create_targeted_sink(ws_interaction_notifier, thread_id) if _pipeline_ctx and _pipeline_ctx.available else None

                    _history = conversation_histories.get(thread_id, [])

                    from pipeline.registry import get_engine_registry  # noqa: PLC0415
                    _registry = get_engine_registry()

                    # BUG-FIX-fix_20260531_sink_dead_thread_id_lost:
                    # 重启后 registry 中 entry.thread_id 为空，需在此处补上。
                    if _target_pid and thread_id:
                        _existing_entry = _registry.get(_target_pid)
                        if _existing_entry and not _existing_entry.thread_id:
                            _existing_entry.thread_id = thread_id

                    # 注册表没条目时不拦阻——转发给 send_pipeline_message，
                    # 由其内部 revive 逻辑重建（任务系统从 task 数据拿 agent，
                    # 会话系统从 api_store 拿 agent）。路口只转发不注册。

                    # BUG-FIX-fix_20260531_sink_dead_thread_id_lost:
                    # 管道已存在时（跳过了 register_pipeline），确保 thread_id 被更新。
                    if _target_pid:
                        _existing_entry = _registry.get(_target_pid)
                        if _existing_entry and not _existing_entry.thread_id:
                            _registry.update_thread_id(_target_pid, thread_id)

                    # 确保消息携带 thread_id（前端不一定在 payload 里带，但
                    # 连接层已确定）：引擎层 _resolve_persistent_agent 依赖它查 api_store
                    if not _pipeline_msg.thread_id:
                        _pipeline_msg.thread_id = thread_id

                    # BUG-FIX-fix_20260619_ws_lost_task_id:
                    # 前端「停止→再发送」走 WS 对话路径，WS 入口只转发消息，
                    # 不知道当前会话属于哪个任务，原先未传 task_id，导致引擎
                    # state[TASK_ID]=''，L2 task_submit 因拿不到 parent_task_id
                    # 报 L2_REQUIRES_PARENT_TASK。
                    # 修复：从注册表 tags 恢复 task_id（任务系统注册管道时写入，
                    # 见 task_executor.py register_pipeline 的 tags["task_id"]）。
                    # 会话类管道（非任务）tags 无 task_id，_ws_task_id 为空，行为不变。
                    _ws_task_id = ""
                    if _target_pid:
                        _entry_for_task = _registry.get(_target_pid)
                        if _entry_for_task and getattr(_entry_for_task, "tags", None):
                            _ws_task_id = _entry_for_task.tags.get("task_id", "") or ""

                    # ── 统一入口转发（只转发，不传 agent_config）──
                    _result = await send_pipeline_message(
                        _pipeline_msg,
                        output_sink=_sink,
                        conversation_history=_history if _history else None,
                        task_id=_ws_task_id,
                    )

                    if _result.success:
                        continue

                    await websocket.send_text(json.dumps({
                        "type": "stream_error",
                        "data": {"message_id": _msg_id, "error": _result.error or "管道不可用", "pipeline_id": _target_pid},
                    }, ensure_ascii=False))
                    continue
                if msg_type == "interaction_response":
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
                            from human_interaction import get_human_interaction_service  # noqa: PLC0415
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
                                    # human_interaction 工具为阻塞执行：工具内部 wait_for_choice()
                                    # 由 respond() → submit_response() → _set_event_threadsafe() 直接唤醒，
                                    # 工具返回后引擎自然进入下一轮，无需此处额外 wake()。
                                    # conversation 模式下，工具返回 conversation_mode=True 后引擎才挂起，
                                    # 此时应等待用户在对话标签页发新消息（经 send_pipeline_message 注入）唤醒。
                                    # 旧逻辑在此处用"进入标签页"的 approved 响应直接 wake()，会提前唤醒且不
                                    # 携带用户消息，既触发一次空转 LLM 调用，又导致用户随后发送的消息无法注入。
                                    # BUG-FIX-fix_20260625_conversation_wake_loses_user_input
                                    if pipeline_id and pipeline_id.startswith("__eval__"):
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
                if msg_type == "stop_generation":
                    # BUG-FIX-fix_20260512_stop_generation_global_ws:
                    # 问题根因: 旧代码只发送假的 stopped 状态，没有实际取消流式任务
                    #           和管道引擎运行，导致停止按钮无效。
                    # 修复方案:
                    #   1. 设置对应 thread_id 的 stop_event
                    #   2. 取消对应 thread_id 的流式任务
                    #   3. 尝试查找并取消关联的管道引擎
                    #   4. 尝试取消 TaskWorker 中的关联后台任务
                    #   5. 发送 state_change 回前端
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
                        from pipeline.message_bus import _find_engine  # noqa: PLC0415
                        from pipeline.registry import get_engine_registry as _get_reg  # noqa: PLC0415
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
                                # 重置引擎运行状态：停止输出不注销引擎，保留注册表条目。
                                # _find_engine 检查 _run_started，重置后引擎回到 idle 态，
                                # 重发消息时走 _start_idle_engine（用自带 _agent_config）。
                                # BUG-FIX-fix_20260625_zombie_suspended_engine:
                                #   若停止时引擎正挂在 suspend（wait 路由/等子任务），_run_loop
                                #   被 cancel 后不会清 _suspended_state。仅复位 _run_started 不够——
                                #   _find_engine 先判 is_suspended，会把它当"挂起中"返回，重发消息
                                #   走 wake 路径注入一个 engine_task 已死的僵尸，消息永久堆积。
                                #   故必须同时清空 _suspended_state / _wake_event / _engine_loop。
                                if hasattr(_eng, "_suspended_state"):
                                    _eng._suspended_state = None
                                if hasattr(_eng, "_wake_event"):
                                    _eng._wake_event = None
                                if hasattr(_eng, "_engine_loop"):
                                    _eng._engine_loop = None
                                if hasattr(_eng, "_run_started"):
                                    _eng._run_started = False
                                logger.info("[GlobalWS] 已停止引擎: pipeline=%s", _pid[:12])
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
                                                # BUG-FIX-fix_20260522_stop_generation_pipeline:
                                                #   在取消管道前先调用 fail_task 标记任务失败，
                                                #   确保任务状态被正确更新，而非仅中断执行。
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
            # BUG-FIX-fix_20260624_ws_double_ledger:
            # active_connections 现已直接复用 notifier._active_connections，
            # 这里只剩两件事：注销全局连接 + 清理 notifier 内的所有 thread 残留，
            # 顺带清理跟随退出的会话历史（conversation_histories 仍是本地字典）。
            ws_interaction_notifier.unregister_global(user_id, websocket)
            removed_tids: list[str] = []
            for tid in list(active_connections.keys()):
                if websocket in active_connections.get(tid, []):
                    active_connections[tid] = [c for c in active_connections[tid] if c != websocket]
                    if not active_connections[tid]:
                        removed_tids.append(tid)
            ws_interaction_notifier.unregister_all_for_ws(websocket)
            for tid in removed_tids:
                # unregister_all_for_ws 已经把空列表从 _active_connections 删了，
                # 这里仅同步清理本地 conversation_histories。
                conversation_histories.pop(tid, None)

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


def main() -> None:
    """主函数，启动 uvicorn 服务器。

    端口优先级：
    1. 命令行参数 --port
    2. 环境变量 BACKEND_PORT
    3. 默认值 8988

    如果指定端口被占用，自动查找下一个可用端口。
    """
    parser = argparse.ArgumentParser(description="Agent OS 服务器")
    parser.add_argument("--port", type=int, default=None, help="后端服务端口")
    args = parser.parse_args()

    default_port = 8988
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
