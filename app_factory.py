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

# 将 src 目录加入 sys.path，确保模块可被正确导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from channels.api.app import create_app
from channels.api.auth import verify_token
from channels.api.memory_store import store as api_store

from src.pipeline.stream_bridge import PipelineStreamBridge, TargetedSink

from ws_handler import ws_interaction_notifier
import stream_handler

from stream_handler import (
    PipelineContext,
    _init_pipeline_context,
    _stream_engine_response,
    _stream_wake_response,
)
from static_files import mount_media_static_files

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_pipeline_ctx: PipelineContext | None = None
_task_worker_started: bool = False


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

    @asynccontextmanager
    async def _combined_lifespan(app: FastAPI):
        """应用生命周期管理，替代已弃用的 on_event('startup')。

        在应用启动时立即启动 TaskWorker，确保纯 API 场景下任务可正常执行。
        """
        global _task_worker_started
        if not _task_worker_started:
            tw = getattr(stream_handler, "_task_worker", None)
            if tw and hasattr(tw, "start"):
                try:
                    await tw.start()
                    _task_worker_started = True
                    logger.info("TaskWorker started (app startup)")
                except Exception as exc:
                    logger.warning("TaskWorker start failed (app startup): %s", exc)
        yield

    app.router.lifespan_context = _combined_lifespan

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

        # BUG-FIX-fix_20260512_stop_generation_global_ws:
        # 问题根因: /ws/chat 全局端点的 stop_generation 处理只发送假的 stopped 状态，
        #           没有实际取消流式任务和管道引擎运行，导致前端点击停止按钮后
        #           后端继续生成并消耗资源。
        # 修复方案: 添加 thread_id → (stream_task, stop_event) 追踪字典，
        #           在创建流式任务时存储引用，在 stop_generation 时实际取消。
        _thread_stream_tasks: dict[str, asyncio.Task] = {}
        _thread_stop_events: dict[str, asyncio.Event] = {}

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
                    # BUG-FIX-fix_20260511_task_worker_global_ws:
                    # 问题根因: /ws/chat 全局端点缺少 TaskWorker 懒启动逻辑，
                    #           导致通过全局 WS 提交的任务没有订阅者，
                    #           pipeline_run_id 永远不会被绑定。
                    # 修复方案: 添加与 /ws/chat/{thread_id} 相同的 TaskWorker 懒启动。
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

                    msg_data.setdefault("pipeline_id", msg_data.get("pipeline_id", ""))
                    if thread_id not in active_connections:
                        active_connections[thread_id] = []
                    if websocket not in active_connections[thread_id]:
                        active_connections[thread_id].append(websocket)
                    if thread_id not in conversation_histories:
                        conversation_histories[thread_id] = []

                    ws_interaction_notifier.register(thread_id, websocket)

                    _user_content = msg_data.get("content", "")
                    if not _user_content:
                        continue
                    _msg_id = uuid.uuid4().hex[:12]
                    _client_msg_id = msg_data.get("client_message_id", "")
                    _pipeline_id = msg_data.get("pipeline_id", "")
                    _stop_evt = asyncio.Event()
                    _history = conversation_histories.get(thread_id, [])

                    # BUG-FIX-fix_20260512_stop_generation_global_ws:
                    # 在创建新的流式任务前，取消该 thread_id 之前的旧任务，
                    # 确保同一 thread_id 同时只有一个活跃的流式生成任务。
                    _old_task = _thread_stream_tasks.get(thread_id)
                    if _old_task and not _old_task.done():
                        _old_stop = _thread_stop_events.get(thread_id)
                        if _old_stop:
                            _old_stop.set()
                        _old_task.cancel()
                    _thread_stop_events[thread_id] = _stop_evt

                    if _pipeline_id:
                        from pipeline.message_bus import send_pipeline_message, _find_engine as _find_engine_for_sub
                        _sub_eng, _sub_st = _find_engine_for_sub(_pipeline_id)
                        if _sub_eng and _sub_st == "suspended":
                            _sub_bridge = PipelineStreamBridge(
                                pipeline_id=_pipeline_id,
                                output_sink=TargetedSink(ws_interaction_notifier, thread_id),
                                message_id=_msg_id,
                            )
                            _sub_eng._saved_on_chunk = _sub_bridge.on_chunk
                            _sub_eng._saved_streaming = True
                            if _sub_eng._suspended_state is not None:
                                _sub_eng._suspended_state["on_chunk"] = _sub_bridge.on_chunk
                                _sub_eng._suspended_state["streaming"] = True
                            sub_result = await send_pipeline_message(_pipeline_id, _user_content)
                            if sub_result.success:
                                _stream_task = asyncio.create_task(
                                    _stream_wake_response(
                                        websocket, _user_content, _msg_id,
                                        _stop_evt, _sub_eng, _pipeline_id,
                                        thread_id=thread_id,
                                        ws_notifier=ws_interaction_notifier,
                                        conversation_history=_history,
                                        pre_created_bridge=_sub_bridge,
                                    )
                                )
                                _thread_stream_tasks[thread_id] = _stream_task
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
                    # 优先用 session 已有的 pipeline_id（创建会话时分配）
                    _sess = api_store.get_session(thread_id)
                    _main_pid = _sess.active_pipeline_id if _sess and _sess.active_pipeline_id else ""
                    if not _main_pid and _pipeline_ctx and _pipeline_ctx.available and _pipeline_ctx.engine:
                        _main_pid = _pipeline_ctx.engine.pipeline_id
                    _ex_eng, _eng_st = _find_engine(_main_pid) if _main_pid else (None, "")

                    if _ex_eng and _eng_st == "suspended":
                        _wake_bridge = PipelineStreamBridge(
                            pipeline_id=_main_pid,
                            output_sink=TargetedSink(ws_interaction_notifier, thread_id),
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
                            _stream_task = asyncio.create_task(
                                _stream_wake_response(
                                    websocket, _user_content, _msg_id,
                                    _stop_evt, _ex_eng, _main_pid,
                                    thread_id=thread_id,
                                    ws_notifier=ws_interaction_notifier,
                                    conversation_history=_history,
                                    pre_created_bridge=_wake_bridge,
                                )
                            )
                            _thread_stream_tasks[thread_id] = _stream_task
                        continue

                    if _pipeline_ctx and _pipeline_ctx.available:
                        _main_sink = TargetedSink(ws_interaction_notifier, thread_id)
                        _main_bridge = PipelineStreamBridge(
                            pipeline_id=_main_pid,
                            output_sink=_main_sink,
                            message_id=_msg_id,
                        )
                        _stream_task = asyncio.create_task(
                            _stream_engine_response(
                                websocket, _user_content, _msg_id,
                                _stop_evt, thread_id, _history, _pipeline_ctx,
                                ws_notifier=ws_interaction_notifier,
                                pre_created_bridge=_main_bridge,
                            )
                        )
                        _thread_stream_tasks[thread_id] = _stream_task
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

                    # 1. 设置 stop_event 并取消流式任务
                    _stop_evt = _thread_stop_events.get(thread_id)
                    if _stop_evt:
                        _stop_evt.set()
                    _stream_task = _thread_stream_tasks.get(thread_id)
                    if _stream_task and not _stream_task.done():
                        _stream_task.cancel()
                        try:
                            await _stream_task
                        except asyncio.CancelledError:
                            pass
                    _thread_stream_tasks.pop(thread_id, None)
                    _thread_stop_events.pop(thread_id, None)

                    # 2. 尝试查找并取消关联的管道引擎
                    #    通过 _pipeline_thread_map 反查 pipeline_id，
                    #    再通过 _find_engine 找到运行中的引擎进行取消。
                    _all_pipeline_ids: set[str] = set()
                    try:
                        from pipeline.message_bus import _find_engine
                        if hasattr(ws_interaction_notifier, "_pipeline_thread_map"):
                            for _pid, _tid in ws_interaction_notifier._pipeline_thread_map.items():
                                if _tid == thread_id:
                                    _all_pipeline_ids.add(_pid)
                        for _pid in _all_pipeline_ids:
                            _eng, _st = _find_engine(_pid)
                            if _eng:
                                # 取消引擎的挂起状态
                                if hasattr(_eng, "_suspended_state") and _eng._suspended_state is not None:
                                    _eng._suspended_state["ended"] = True
                                # 唤醒引擎使其退出挂起等待
                                if hasattr(_eng, "_wake_event") and _eng._wake_event is not None:
                                    _eng._wake_event.set()
                                logger.info("[GlobalWS] 已取消管道引擎: pipeline=%s state=%s", _pid[:12], _st)
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
