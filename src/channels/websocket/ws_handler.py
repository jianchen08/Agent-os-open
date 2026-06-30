"""WebSocket 人类交互通知处理器。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from fastapi import WebSocket

logger = logging.getLogger(__name__)


def _resolve_send_timeout() -> float:
    """读取 WebSocket 发送超时（秒）。"""
    raw = os.environ.get("WS_SEND_TIMEOUT_SECONDS")
    if not raw:
        return 30.0
    try:
        val = float(raw)
        return val if val > 0 else 30.0
    except ValueError:
        return 30.0


# 模块级常量：进程启动时读一次即可，运行时不允许动态调整以保证行为可预期。
_SEND_TIMEOUT_SECONDS = _resolve_send_timeout()


class WebSocketInteractionNotifier:
    """通过 WebSocket 将人类交互请求转发到前端。"""

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
        if old is not None and old is not websocket:
            logger.info("[GlobalWS] 踢掉旧连接: user=%s", user_id[:12])
            self._schedule_close(old, code=4000, reason="被新连接替换")
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
            resumed_tids: set[str] = set()
            for tid in list(self._active_connections.keys()):
                self._resume_pipeline_for_thread(tid)
                resumed_tids.add(tid)
            # 补充: 从 registry 恢复 _active_connections 里没有的活跃 pipeline
            try:
                from pipeline.registry import get_engine_registry  # noqa: PLC0415
                _reg = get_engine_registry()
                for _pid, _entry in list(_reg.all_entries().items()):
                    if _entry.thread_id and _entry.thread_id not in resumed_tids:
                        self._resume_pipeline_for_thread(_entry.thread_id)
                        resumed_tids.add(_entry.thread_id)
            except Exception:
                logger.debug("[WS-Reconnect] registry 补充恢复失败（非致命）", exc_info=True)
        except Exception as _exc:
            logger.debug("[WS-Reconnect] 全局连接恢复 pipeline 失败: %s", _exc)

    @staticmethod
    def _schedule_close(websocket: WebSocket, *, code: int, reason: str) -> None:
        """安全地调度一个 WebSocket close 任务。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            with contextlib.suppress(Exception):
                loop.create_task(websocket.close(code=code, reason=reason))
            return

        # 没有 running loop：尝试用兜底的 _main_loop_ref（由 lifespan 注册），
        # 这样从同步上下文调用时也能把 close 投递到主 loop。
        main_loop = getattr(asyncio, "_main_loop_ref", None)
        if main_loop is not None and not main_loop.is_closed():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    websocket.close(code=code, reason=reason), main_loop,
                )
            return

        # 完全没有 loop 可用（极端情况）：尽量同步关闭底层 socket
        with contextlib.suppress(Exception):
            client_state = getattr(websocket, "client_state", None)
            logger.debug(
                "[GlobalWS] 旧连接 close 无可用 loop，跳过异步关闭 client_state=%s",
                client_state,
            )

    def _resume_pipeline_for_thread(self, thread_id: str) -> None:
        """恢复指定 thread_id 关联的活跃 pipeline 的 WebSocket 输出。"""
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            from pipeline.stream_bridge import TargetedSink, create_targeted_sink  # noqa: F401,PLC0415
            registry = get_engine_registry()
        except Exception:
            return

        # 遍历所有注册表条目，查找与该 thread_id 关联的活跃 pipeline。
        # 匹配来源：entry.thread_id 优先，为空时用 tags["session_id"] 兜底
        for pipeline_id, entry in list(registry._engines.items()):
            _matched_tid = entry.thread_id if entry.thread_id else (
                (entry.tags or {}).get("session_id", "") if entry.tags else ""
            )
            if _matched_tid != thread_id:
                continue
            if entry.engine is None:
                continue
            # 检查引擎是否仍在运行或挂起中
            _engine_running = getattr(entry.engine, 'is_running', False)
            _engine_suspended = getattr(entry.engine, 'is_suspended', False)
            if not _engine_running and not _engine_suspended:
                continue

            # 找到活跃的 pipeline，无条件把 sink 切到新连接。
            # 重连即"新连接接管"，旧 sink 必然指向已断开连接，直接重建即可，
            # 不必等连续失败判 dead。
            if entry.bridge is not None:
                # 补全 entry.thread_id（历史为空时），避免后续 send_to_thread 仍走 no-thread 回退
                if not entry.thread_id and thread_id:
                    entry.thread_id = thread_id
                _new_sink = create_targeted_sink(self, thread_id)
                if _new_sink is not None:
                    entry.bridge.output_sink = _new_sink
                    logger.info(
                        "[WS-Reconnect] 已恢复 pipeline 输出: pipeline=%s thread=%s "
                        "(重建 sink)",
                        pipeline_id[:12], thread_id[:12],
                    )
                # Phase 1: drain_loop 已删除，engine 主动 emit 推送。
                # sink 已替换，无需重启 drain。

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
                timeout=_SEND_TIMEOUT_SECONDS,
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
        """取消指定请求的自动确认回退任务。"""
        task = self._fallback_request_map.pop(request_id, None)
        if task and not task.done():
            task.cancel()
            logger.debug(
                "[WSNotifier] 已取消自动确认回退 | request_id=%s", request_id,
            )

    async def _send_event_to_thread(self, thread_id: str, event_data: dict) -> bool:
        """统一发送事件到 thread_id 关联的连接，先活跃连接再全局连接。"""
        conns = self._active_connections.get(thread_id, [])
        if conns:
            payload = json.dumps(event_data, ensure_ascii=False)
            stale: list = []
            sent_any = False
            for ws in conns:
                try:
                    await asyncio.wait_for(ws.send_text(payload), timeout=_SEND_TIMEOUT_SECONDS)
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
                    timeout=_SEND_TIMEOUT_SECONDS,
                )
                return True
            except (asyncio.TimeoutError, Exception):
                self._global_connections.pop(user_id, None)

        return False

    async def send_to_thread(self, thread_id: str, event_data: dict) -> bool:
        """向指定 thread_id 的最新活跃 WebSocket 连接发送事件。"""
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
