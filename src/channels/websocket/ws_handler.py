"""WebSocket 人类交互通知处理器。

通过 WebSocket 将人类交互请求转发到前端，管理连接注册/注销、
全局单连接模式、自动确认回退等功能。

从 start_server.py 拆分而来，保持向后兼容。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketInteractionNotifier:
    """通过 WebSocket 将人类交互请求转发到前端。

    注册到 HumanInteractionService，当管道调用 human_interaction 工具时，
    将请求通过 WebSocket 发送到前端，前端展示交互面板。
    用户响应后，通过 interaction_response 消息提交回服务。

    如果前端在 auto_confirm_delay 秒内未响应，自动批准（回退策略）。
    """

    def __init__(self, auto_confirm_delay: float = 600.0) -> None:
        self._active_connections: dict[str, list[WebSocket]] = {}
        self._auto_confirm_delay = auto_confirm_delay
        self._service = None
        self._fallback_tasks: set[asyncio.Task] = set()
        self._fallback_request_map: dict[str, asyncio.Task] = {}
        self._global_connections: dict[str, WebSocket] = {}

    def set_service(self, service) -> None:
        self._service = service

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
        try:
            self._resume_pipeline_for_thread(thread_id)
        except Exception as _exc:
            logger.debug("[WS-Reconnect] 恢复 pipeline 失败: %s", _exc)

    def unregister(self, thread_id: str, websocket: WebSocket) -> None:
        if thread_id in self._active_connections:
            conns = self._active_connections[thread_id]
            self._active_connections[thread_id] = [
                c for c in conns if c != websocket
            ]
            if not self._active_connections[thread_id]:
                del self._active_connections[thread_id]

    def unregister_all_for_ws(self, websocket: WebSocket) -> None:
        """清理指定 WebSocket 连接在所有 thread_id 下的注册，同时清理 _global_connections。"""
        for tid in list(self._active_connections.keys()):
            conns = self._active_connections.get(tid, [])
            if websocket in conns:
                self._active_connections[tid] = [c for c in conns if c != websocket]
                if not self._active_connections[tid]:
                    del self._active_connections[tid]
        # 清理 _global_connections 中对应的条目
        stale_users = [uid for uid, ws in self._global_connections.items() if ws is websocket]
        for uid in stale_users:
            self._global_connections.pop(uid, None)

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

        payload_obj = {
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
                "file_paths": msg_data.get("file_paths"),
                "progress": msg_data.get("progress"),
                "agent_level": msg_data.get("agent_level"),
                "session_id": record.get("session_id", ""),
            },
        }

        payload = json.dumps(payload_obj, ensure_ascii=False)
        sent = False
        if conns:
            for ws in conns:
                try:
                    await ws.send_text(payload)
                    sent = True
                except Exception:
                    logger.debug(
                        "[WSNotifier] 发送交互请求失败，连接可能已断开"
                    )

        if not sent:
            for user_id, ws in list(self._global_connections.items()):
                try:
                    await ws.send_text(json.dumps(payload_obj, ensure_ascii=False))
                    sent = True
                    break
                except Exception:
                    self._global_connections.pop(user_id, None)

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
            self._fallback_request_map[request_id] = task
            task.add_done_callback(
                lambda t, _rid=request_id: self._fallback_request_map.pop(_rid, None)  # noqa: ARG005
            )
            task.add_done_callback(self._fallback_tasks.discard)

        return sent

    # ── 全局单连接模式方法 ──

    def register_global(self, user_id: str, websocket: WebSocket) -> None:
        """注册全局单连接（新架构：每用户一个 WS 连接）。"""
        old = self._global_connections.get(user_id)
        if old is not None:
            logger.info("[GlobalWS] 踢掉旧连接: user=%s", user_id[:12])
            with contextlib.suppress(Exception):
                asyncio.get_event_loop().create_task(old.close(code=4000, reason="被新连接替换"))
            # 清理旧连接在 _active_connections 中的残留条目，
            # 避免后续 send_to_thread 等方法在第一步找到空连接列表而非直接走 global 回退。
            for tid in list(self._active_connections.keys()):
                conns = self._active_connections.get(tid, [])
                if old in conns:
                    self._active_connections[tid] = [c for c in conns if c is not old]
                    if not self._active_connections[tid]:
                        del self._active_connections[tid]
        self._global_connections[user_id] = websocket
        logger.info("[GlobalWS] 全局连接已注册: user=%s, 总连接数=%d", user_id[:12], len(self._global_connections))
        try:
            for tid in list(self._active_connections.keys()):
                self._resume_pipeline_for_thread(tid)
        except Exception as _exc:
            logger.debug("[WS-Reconnect] 全局连接恢复 pipeline 失败: %s", _exc)

    def _resume_pipeline_for_thread(self, thread_id: str) -> None:
        """恢复指定 thread_id 关联的活跃 pipeline 的 WebSocket 输出。

        BUG-FIX-fix_20260601_ws_reconnect_resume_pipeline:
        前端刷新后重新连接时，查找该 thread_id 关联的正在运行的 pipeline，
        更新其 bridge 的 output_sink 为新的活跃连接，确保后续输出能续接到前端。

        Args:
            thread_id: 目标会话的 thread_id
        """
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            from pipeline.stream_bridge import TargetedSink, create_targeted_sink  # noqa: F401,PLC0415
            registry = get_engine_registry()
        except Exception:
            return

        # 遍历所有注册表条目，查找与该 thread_id 关联的活跃 pipeline
        for pipeline_id, entry in list(registry._engines.items()):
            if entry.thread_id != thread_id:
                continue
            if entry.engine is None:
                continue
            # 检查引擎是否仍在运行或挂起中
            _engine_running = getattr(entry.engine, 'is_running', False)
            _engine_suspended = getattr(entry.engine, 'is_suspended', False)
            if not _engine_running and not _engine_suspended:
                continue

            # 找到活跃的 pipeline，更新其 sink
            if entry.bridge is not None:
                _old_sink = getattr(entry.bridge, 'output_sink', None)
                _old_dead = getattr(_old_sink, 'is_dead', False) if _old_sink is not None else False
                if _old_dead:
                    # 创建新的 TargetedSink，使用当前 notifier 和 thread_id
                    _new_sink = create_targeted_sink(self, thread_id)
                    entry.bridge.output_sink = _new_sink
                    logger.info(
                        "[WS-Reconnect] 已恢复 pipeline 输出: pipeline=%s thread=%s "
                        "(替换 dead sink)",
                        pipeline_id[:12], thread_id[:12],
                    )
                    # Phase 1: drain_loop 已删除，engine 主动 emit 推送。
                    # sink 已替换，无需重启 drain。
            break  # 只恢复第一个匹配的 pipeline

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
            await asyncio.wait_for(
                ws.send_text(json.dumps(event, ensure_ascii=False, default=str)),
                timeout=5.0,
            )
            return True
        except (asyncio.TimeoutError, Exception) as exc:
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

    async def _send_event_to_thread(self, thread_id: str, event_data: dict) -> bool:
        """统一发送事件到 thread_id 关联的连接，先活跃连接再全局连接。

        封装完整的发送逻辑：先查 _active_connections 发给所有活跃连接，
        失败则回退到 _global_connections。发送失败的连接会被清理。

        Args:
            thread_id: 目标会话的 thread_id
            event_data: 完整的事件字典（将被 json.dumps 序列化）

        Returns:
            是否成功发送到至少一个连接
        """
        conns = self._active_connections.get(thread_id, [])
        if conns:
            payload = json.dumps(event_data, ensure_ascii=False)
            stale: list = []
            sent_any = False
            for ws in conns:
                try:
                    await asyncio.wait_for(ws.send_text(payload), timeout=5.0)
                    sent_any = True
                except (asyncio.TimeoutError, Exception):
                    stale.append(ws)
            if stale:
                self._active_connections[thread_id] = [
                    c for c in conns if c not in stale
                ]
                if not self._active_connections[thread_id]:
                    self._active_connections.pop(thread_id, None)
            if sent_any:
                return True

        # 回退全局连接
        for user_id, ws in list(self._global_connections.items()):
            try:
                await asyncio.wait_for(
                    ws.send_text(json.dumps(event_data, ensure_ascii=False)),
                    timeout=5.0,
                )
                return True
            except (asyncio.TimeoutError, Exception):
                self._global_connections.pop(user_id, None)

        return False

    async def send_to_thread(self, thread_id: str, event_data: dict) -> bool:
        """向指定 thread_id 的最新活跃 WebSocket 连接发送事件。

        优先查找 _active_connections（per-session 连接），
        若无则回退到 _global_connections（全局单连接）。

        Args:
            thread_id: 目标会话的 thread_id
            event_data: 完整的事件字典

        Returns:
            是否成功发送
        """
        ok = await self._send_event_to_thread(thread_id, event_data)
        if not ok:
            # thread_id 为空说明是后端任务（CLI/定时触发），没有前端连接是正常的，不打 warning
            if not thread_id:
                logger.debug(
                    "send_to_thread: 无活跃连接（后端任务）: type=%s",
                    event_data.get("type", "?"),
                )
            else:
                logger.warning(
                    "send_to_thread: 无活跃连接: thread_id=%s type=%s active=%s global=%s",
                    thread_id[:12] if thread_id else "(empty)",
                    event_data.get("type", "?"),
                    {k[:12]: len(v) for k, v in self._active_connections.items()},
                    list(self._global_connections.keys()),
                )
        return ok

    async def notify_cancel(
        self, request_id: str, reason: str | None = None, thread_id: str = ""
    ) -> bool:
        """通知前端交互请求已取消。"""
        return await self._send_event_to_thread(thread_id, {
            "type": "interaction_cancelled",
            "data": {"request_id": request_id, "reason": reason},
        })

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """通知前端交互请求已超时。"""
        return await self._send_event_to_thread(thread_id, {
            "type": "interaction_timeout",
            "data": {"request_id": request_id},
        })

    async def notify_timeout_reminder(
        self, request_id, remaining_seconds, thread_id="", **kw
    ) -> bool:
        """通知前端交互请求即将超时。"""
        return await self._send_event_to_thread(thread_id, {
            "type": "interaction_timeout_reminder",
            "data": {
                "request_id": request_id,
                "remaining_seconds": remaining_seconds,
            },
        })

    async def notify_conversation_start(
        self, thread_id, tab_id, title, **kw
    ) -> bool:
        return True


ws_interaction_notifier = WebSocketInteractionNotifier()
