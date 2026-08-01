"""FastAPI 应用工厂和服务器入口。"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import socket
import sys  # noqa: F401
import uuid
from datetime import datetime, timezone
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

# web 模式强制同时输出到文件：[WSNotifier]/[GlobalWS] 等交互推送链路日志需可事后审计，
# 仅输出到控制台时（LoggingConfig 默认 output=console），进程关闭后无从查证。
# CLI 不受影响（cli_main.py 有独立的 setup_logging）。
_web_log_config = dataclasses.replace(LoggingConfig.from_env(), output="both", file_path="logs/agent_os.log")
_setup_unified_logging(_web_log_config, reset=True)

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
    """创建合并了 WebSocket 功能的 FastAPI 应用。"""
    global _pipeline_ctx  # noqa: PLW0603

    # 初始化管道引擎上下文（需要在创建 lifespan 之前完成，因为 lifespan 中要用到 _task_worker）
    _pipeline_ctx = _init_pipeline_context()
    if _pipeline_ctx.available:
        logger.info("管道引擎已就绪，WebSocket 将使用真实 AI 回复")
    else:
        logger.error("管道引擎未就绪！消息发送功能将不可用。请检查上方日志中的错误信息。")

    @asynccontextmanager
    async def _combined_lifespan(app: FastAPI):  # noqa: PLR0912,PLR0915
        """应用生命周期管理，替代已弃用的 on_event('startup')。"""
        global _task_worker_started  # noqa: PLW0603
        logger.info(
            "[Lifespan] 应用启动开始 | pid=%d | loop_id=%s",
            os.getpid(),
            id(asyncio.get_running_loop()),
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

        # 启动时不注册任何管道：注册是明确的调用方行为（任务管理工具启动任务时）。
        # 重启后的 running/pending 任务由 TaskWorker._recover_running_tasks 标记为
        # suspended（执行被中断），等用户明确恢复时再由调用方 register。

        # 会话系统启动恢复：从 api_store 注册所有会话管道（含 agent_id）
        try:
            from channels.api.routes_threads import restore_session_pipelines  # noqa: PLC0415

            _session_count = restore_session_pipelines()
            if _session_count:
                logger.info("[Lifespan] 会话管道恢复: 已注册 %d 个主管道", _session_count)
            else:
                logger.warning("[Lifespan] 会话管道恢复: 0 个（检查 api_store 数据或 restore_session_pipelines）")
        except Exception as exc:
            logger.warning("[Lifespan] restore_session_pipelines 失败: %s", exc, exc_info=True)

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
            # watchfiles 监听循环永不启动。
            asyncio.create_task(center.start())
            _config_center_started = await center.wait_ready(timeout=5.0)
            if _config_center_started:
                logger.info("[Lifespan] ConfigCenter 文件监听已启动")
            else:
                logger.warning("[Lifespan] ConfigCenter 启动超时或失败（热重载将不可用）")
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
                os.getpid(),
                id(asyncio.get_running_loop()),
            )

    app = create_app(lifespan=_combined_lifespan)

    # 每个 thread_id 的对话历史
    conversation_histories: dict[str, list[dict[str, Any]]] = {}

    @app.websocket("/ws")
    async def websocket_root(websocket: WebSocket) -> None:
        """处理根路径 WebSocket 连接（需 token 认证）。"""
        # 鉴权：与 /ws/chat 一致，token 从 query 参数取，accept 前校验
        token = websocket.query_params.get("token", "")
        if not token:
            await websocket.accept()
            await websocket.close(code=4001, reason="连接需要 token 认证")
            return
        payload = verify_token(token)
        if payload is None:
            await websocket.accept()
            await websocket.close(code=4001, reason="Token 无效或已过期")
            return

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

        async def _safe_reject_ws(code: int, reason: str) -> None:
            """拒绝连接：accept + close。吞 websockets 15.x close 的库内部异常。

            websockets>=13 移除了 transfer_data_task，starlette 的 close 路径在
            uvicorn websockets_impl 中会 await self.transfer_data_task 触发
            AttributeError（库 bug，非业务异常），导致 ASGI 崩溃。这里兜底。
            """
            await websocket.accept()
            try:
                await websocket.close(code=code, reason=reason)
            except Exception:
                logger.debug("[GlobalWS] 拒绝连接 close 异常（已忽略）: code=%s", code, exc_info=True)

        token = websocket.query_params.get("token", "")
        if not token:
            await _safe_reject_ws(4001, "全局连接需要 token 认证")
            return
        payload = verify_token(token)
        if payload is None:
            await _safe_reject_ws(4001, "Token 无效或已过期")
            return
        user_id = payload.get("sub", "")
        if not user_id:
            await _safe_reject_ws(4001, "Token 中缺少用户标识")
            return

        await websocket.accept()
        ws_interaction_notifier.register_global(user_id, websocket)

        try:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "connection_confirmation",
                        "data": {"status": "connected", "mode": "global", "user_id": user_id},
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )
        except WebSocketDisconnect:
            logger.info("[GlobalWS] 客户端在握手确认前已断开: user=%s", user_id[:12])
            ws_interaction_notifier.unregister_global(user_id, websocket)
            return
        except Exception as exc:
            logger.error(
                "[GlobalWS] 发送 connection_confirmation 失败: user=%s err=%s",
                user_id[:12],
                exc,
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
                    logger.info(
                        "[GlobalWS] 收到消息: type=%s thread_id=%s user=%s",
                        msg_type,
                        msg_data.get("thread_id", "")[:12],
                        user_id[:12],
                    )

                if msg_type == "heartbeat":
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "heartbeat_ack",
                                "data": {"server_time": datetime.now(timezone.utc).isoformat()},
                            }
                        )
                    )
                    continue

                thread_id = msg_data.get("thread_id", "")
                if not thread_id:
                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "error",
                                "data": {"message": f"消息缺少 thread_id: type={msg_type}"},
                            }
                        )
                    )
                    continue

                # 绑定链路追踪上下文，使后续日志可关联到当前会话
                LogContext.bind(
                    request_id=msg_data.get("request_id", uuid.uuid4().hex[:12]),
                    session_id=thread_id,
                    thread_id=thread_id,
                    trace_id=msg_data.get("trace_id", ""),
                )

                if msg_type == "user_input":
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

                    if thread_id not in conversation_histories:
                        conversation_histories[thread_id] = []

                    # 单连接架构：真正的 ws 注册在 register_global(user_id)；
                    # 这里只建立 thread_id → user_id 逻辑映射，供流式发送路径反查。
                    ws_interaction_notifier.register_thread_user(thread_id, user_id)

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
                            thread_id,
                            _pipeline_msg.content[:30],
                        )
                        continue

                    # WS 入口只做转发：pipeline_id → 注册表找引擎 → 转发消息。
                    # agent 决策完全由数据源决定（不在此解析）：
                    # - 主管道：会话映射 thread["agent_id"]（注册时写入 tags，重建时从会话读）
                    # - 子任务管道：任务数据 task.metadata["target_id"]
                    # 引擎 idle 重启用引擎自带 _agent_config；revive 从持久化数据重建。
                    _sink = (
                        create_targeted_sink(
                            ws_interaction_notifier,
                            thread_id,
                            user_id=user_id,
                        )
                        if _pipeline_ctx and _pipeline_ctx.available
                        else None
                    )

                    _history = conversation_histories.get(thread_id, [])

                    from pipeline.registry import get_engine_registry  # noqa: PLC0415

                    _registry = get_engine_registry()

                    if _target_pid and thread_id:
                        _existing_entry = _registry.get(_target_pid)
                        if _existing_entry and not _existing_entry.thread_id:
                            _existing_entry.thread_id = thread_id

                    # 注册表没条目时不拦阻——转发给 send_pipeline_message，
                    # 由其内部 revive 逻辑重建（任务系统从 task 数据拿 agent，
                    # 会话系统从 api_store 拿 agent）。路口只转发不注册。

                    if _target_pid:
                        _existing_entry = _registry.get(_target_pid)
                        if _existing_entry and not _existing_entry.thread_id:
                            _registry.update_thread_id(_target_pid, thread_id)

                    # 确保消息携带 thread_id（前端不一定在 payload 里带，但
                    # 连接层已确定）：引擎层 _resolve_persistent_agent 依赖它查 api_store
                    if not _pipeline_msg.thread_id:
                        _pipeline_msg.thread_id = thread_id

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

                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "stream_error",
                                "data": {
                                    "message_id": _msg_id,
                                    "error": _result.error or "管道不可用",
                                    "pipeline_id": _target_pid,
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
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
                        request_id,
                        list(resp_data.keys()) if isinstance(resp_data, dict) else "non-dict",
                    )
                    if request_id:
                        try:
                            from human_interaction import get_human_interaction_service  # noqa: PLC0415

                            human_svc = get_human_interaction_service()
                            if human_svc:
                                respond_result = await human_svc.respond(request_id, resp_data)
                                logger.info(
                                    "[GlobalWS] human_svc.respond 返回 | request_id=%s | result=%s",
                                    request_id,
                                    respond_result,
                                )
                                request_record = await human_svc.get_request(request_id)
                                if request_record:
                                    pipeline_id = request_record.get("session_id", "")
                                    logger.info(
                                        "[GlobalWS] 交互请求记录 | request_id=%s | session_id=%s | status=%s",
                                        request_id,
                                        pipeline_id,
                                        request_record.get("status"),
                                    )
                                    # human_interaction 为阻塞执行，工具返回后引擎自然进入下一轮，无需此处 wake()。
                                    # conversation 模式下引擎挂起，等待用户在对话标签页发新消息唤醒。
                                    if pipeline_id and pipeline_id.startswith("__eval__"):
                                        logger.info(
                                            "[GlobalWS] 评估交互响应已处理（纯Event，无管道唤醒） | "
                                            "request_id=%s | session_id=%s",
                                            request_id,
                                            pipeline_id,
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
                        # 用户停止生成 = 控制信号（非终结引擎生命）。
                        # 走 message_bus 信号路径：写 state["pending_signals"]，由插件读取处理。
                        # 不再穿透引擎私有成员（_suspended_state/_wake_event/_run_started）——
                        # 那是反模式，已由 I3 内部自治 + 信号机制取代。
                        # send_pipeline_message 用模块级 import（行 50），勿在此函数内重复
                        # import，否则会制造作用域 bug（426 行 user_input 分支引用被误判局部）。
                        _stop_msg.metadata.setdefault("signal_type", "stop_generation")
                        for _pid in _all_pipeline_ids:
                            await send_pipeline_message(_stop_msg)
                            logger.info("[GlobalWS] 已投递停止信号: pipeline=%s", _pid[:12])
                    except Exception as _eng_err:
                        logger.warning("[GlobalWS] 投递停止信号出错: %s", _eng_err)

                    # 停止生成 = 纯输出打断语义：只投递控制信号（上面已做），不触碰
                    # 任务状态机与引擎注册表。曾在此处 fail_task + cancel_pipeline，会把
                    # "用户暂时不想看输出"误升级为"任务失败 + 管道销毁 + 触发重试"，且
                    # engine_task.cancel() 会经 engine.py 的 CancelledError 分支再次 fail_task，
                    # 二者叠加导致终态通知打到已 unregister 的 parent_pipeline 被硬拒。
                    # 任务/管道的生命周期由显式停止工具(_stop_task)、超时硬墙、容器销毁
                    # 等真正终态路径负责，与 stop_generation 无关。用户重发消息即继续。

                    await websocket.send_text(
                        json.dumps(
                            {
                                "type": "state_change",
                                "data": {"status": "stopped", "thread_id": thread_id},
                            }
                        )
                    )

        except WebSocketDisconnect:
            logger.info("[GlobalWS] 用户断开连接: user=%s", user_id[:12])
        except Exception as exc:
            logger.error("[GlobalWS] 消息循环异常: user=%s err=%s", user_id[:12], exc)
        finally:
            # 在清理前先反查该 user 名下的 thread（供 conversation_histories 清理）
            removed_tids = [tid for tid, uid in ws_interaction_notifier._thread_user_map.items() if uid == user_id]
            ws_interaction_notifier.unregister_global(user_id, websocket)
            ws_interaction_notifier.unregister_all_for_ws(websocket)
            for tid in removed_tids:
                conversation_histories.pop(tid, None)

    # 挂载媒体文件静态服务（必须放在所有路由注册之后）
    mount_media_static_files(app)

    return app


# 入口


def _resolve_bind_host(host: str) -> str:
    """规范化绑定地址，并就「localhost 慢」陷阱给出诊断提示。

    背景：Windows/部分系统下 ``localhost`` 同时解析到 IPv6(::1) 与 IPv4(127.0.0.1)，
    若服务端只监听 IPv4(0.0.0.0)，客户端优先连 ::1 会等待超时(~2s)再回退 IPv4，
    表现为每个请求无故慢 2 秒。此处不强改用户配置，仅在检测到风险时打 WARN，
    指引用户用 127.0.0.1（客户端，推荐）解决。

    注意：BACKEND_HOST=:: 在 Linux 上可达成 IPv4+IPv6 双栈，但实测在 Windows 上
    asyncio/uvicorn 的 :: 监听只接受原生 IPv6 连接，127.0.0.1(IPv4) 反而被拒。
    故 Windows 下首选「客户端用 127.0.0.1 + 服务端 0.0.0.0」而非服务端 :: 。
    """
    if host in ("0.0.0.0", "") and sys.platform == "win32":
        logger.warning(
            "[bind] host=%r 仅监听 IPv4。Windows 下客户端若用 localhost 访问，"
            "会因 ::1(IPv6) 优先且无监听而每个请求超时约 2s 再回退 IPv4。"
            "推荐解决：前端/客户端改用 127.0.0.1（见 frontend/.env）。",
            host or "0.0.0.0",
        )
    return host or "0.0.0.0"


def find_available_port(start_port: int, host: str = "0.0.0.0") -> int:
    """从 start_port 开始查找可用端口。

    根据 host 选择匹配的 socket family 探测：IPv6 地址(如 ``::``)用 AF_INET6，
    其余用 AF_INET，避免与实际监听 family 不一致导致探测失真。
    """
    is_v6 = ":" in host and not host.startswith("0.")
    family = socket.AF_INET6 if is_v6 else socket.AF_INET
    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                # IPv6 socket 默认可能 V6ONLY，关掉以匹配 dualstack 监听语义
                if is_v6:
                    try:
                        s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                    except (OSError, AttributeError):
                        pass
                s.bind((host, port, 0, 0) if is_v6 else (host, port))
                return port
        except OSError:
            logger.debug("端口 %d 已被占用，尝试下一个", port)
            continue
    raise RuntimeError(f"在端口 {start_port}-{start_port + 99} 范围内没有可用端口")


def main() -> None:
    """主函数，启动 uvicorn 服务器。"""
    parser = argparse.ArgumentParser(description="Agent OS 服务器")
    parser.add_argument("--port", type=int, default=None, help="后端服务端口")
    parser.add_argument(
        "--host",
        default=None,
        help="后端绑定地址（默认 0.0.0.0 仅 IPv4；设 :: 在 Linux 可双栈，但 Windows 仅 IPv6 会拒 IPv4，故 Windows 推荐客户端用 127.0.0.1）",
    )
    args = parser.parse_args()

    default_port = 8988
    preferred_port = args.port or int(os.environ.get("BACKEND_PORT", default_port))

    # 绑定地址优先级：命令行 --host > 环境变量 BACKEND_HOST > 默认 0.0.0.0
    bind_host = _resolve_bind_host(args.host or os.environ.get("BACKEND_HOST", "0.0.0.0"))

    actual_port = find_available_port(preferred_port, bind_host)

    if actual_port != preferred_port:
        logger.warning(
            "端口 %d 已被占用，自动切换到 %d",
            preferred_port,
            actual_port,
        )

    logger.info("正在启动 Agent OS 服务器...")
    logger.info("绑定地址: %s:%d", bind_host, actual_port)
    logger.info("API 地址: http://127.0.0.1:%d", actual_port)
    logger.info("API 文档: http://127.0.0.1:%d/docs", actual_port)
    logger.info("健康检查: http://127.0.0.1:%d/health", actual_port)

    os.environ["BACKEND_PORT"] = str(actual_port)

    app = create_combined_app()
    uvicorn.run(
        app,
        host=bind_host,
        port=actual_port,
        log_level="info",
        timeout_keep_alive=120,
        ws_ping_interval=30.0,
        ws_ping_timeout=60.0,
    )


if __name__ == "__main__":
    main()
