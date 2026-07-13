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

    def __init__(self) -> None:
        self._service = None
        # 单连接真相之源：user_id → WebSocket（每用户一条，新连接踢旧连接）。
        self._global_connections: dict[str, WebSocket] = {}
        # 流式路径兜底映射：thread_id → user_id。
        # 仅当发送侧只持有 thread_id（如 TargetedSink）时用于反查 user_id。
        # 在连接正常时（user 发消息，两 id 同时可见）建立，断连时清理。
        self._thread_user_map: dict[str, str] = {}

    def set_service(self, service) -> None:
        self._service = service

    def register_thread_user(self, thread_id: str, user_id: str) -> None:
        """建立 thread_id → user_id 映射，并尝试恢复该 thread 的活跃 pipeline 输出。

        单连接架构下，真正的连接按 user_id 注册在 _global_connections；
        本方法只维护 thread→user 的逻辑映射，供仅持有 thread_id 的流式发送路径反查。
        """
        if thread_id and user_id:
            self._thread_user_map[thread_id] = user_id
        try:
            self._resume_pipeline_for_thread(thread_id)
        except Exception as _exc:
            logger.debug("[WS-Reconnect] 恢复 pipeline 失败: %s", _exc)

    def get_user_for_thread(self, thread_id: str) -> str:
        """反查 thread_id 对应的 user_id（流式发送路径用）。"""
        return self._thread_user_map.get(thread_id, "")

    def unregister_all_for_ws(self, websocket: WebSocket) -> None:
        """清理指定 WebSocket 关联的全部状态：_global_connections + _thread_user_map。"""
        # 先反查该 ws 归属的 user_id
        stale_users = [uid for uid, ws in self._global_connections.items() if ws is websocket]
        for uid in stale_users:
            self._global_connections.pop(uid, None)
        # 清理指向这些 user 的 thread→user 映射
        if stale_users:
            stale_uid_set = set(stale_users)
            for tid in [t for t, u in self._thread_user_map.items() if u in stale_uid_set]:
                self._thread_user_map.pop(tid, None)

    async def notify_request(self, request) -> bool:
        record = request if isinstance(request, dict) else {}
        thread_id = record.get("message_data", {}).get("thread_id", "")
        request_id = record.get("id", "")
        msg_data = record.get("message_data", {})
        user_id = msg_data.get("user_id", "")

        payload_obj = {
            "type": "interaction_request",
            "data": {
                "request_id": request_id,
                "interaction_mode": msg_data.get("interaction_mode", "choice"),
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

        # 单连接架构下按 user_id 精确路由（与 task_notifier 一致）。
        # user_id 由工具层注入；缺失时回退到 thread→user 映射。
        target_uid = user_id or self._thread_user_map.get(thread_id, "")
        sent = False
        if target_uid:
            sent = await self.send_to_user(target_uid, payload_obj)
        elif self._global_connections:
            # 极端兜底：无 user_id 也无映射，但存在全局连接时广播（兼容历史）。
            for uid, ws in list(self._global_connections.items()):
                try:
                    await ws.send_text(json.dumps(payload_obj, ensure_ascii=False))
                    sent = True
                except Exception:
                    self._global_connections.pop(uid, None)

        if sent:
            logger.info(
                "[WSNotifier] 交互请求已发送 | request_id=%s user=%s",
                request_id,
                (target_uid or "broadcast")[:12],
            )
        else:
            # 推送失败（无连接/路由解析失败）：不自动确认，等待用户响应或工具自身超时。
            # 工具默认 timeout_seconds=86400（1天），超时后抛 InteractionTimeoutError 终止等待。
            logger.warning(
                "[WSNotifier] 交互请求未送达前端（无连接/路由失败），"
                "等待用户响应或工具超时 | request_id=%s target_uid=%s thread_id=%s",
                request_id,
                (target_uid or "empty")[:12],
                thread_id[:12],
            )

        return sent

    # ── 全局单连接模式方法 ──

    def register_global(self, user_id: str, websocket: WebSocket) -> None:
        """注册全局单连接（新架构：每用户一个 WS 连接）。"""
        old = self._global_connections.get(user_id)
        if old is not None and old is not websocket:
            logger.info("[GlobalWS] 踢掉旧连接: user=%s", user_id[:12])
            self._schedule_close(old, code=4000, reason="被新连接替换")
        self._global_connections[user_id] = websocket
        logger.info("[GlobalWS] 全局连接已注册: user=%s, 总连接数=%d", user_id[:12], len(self._global_connections))
        try:
            # 重连即新连接接管：恢复该 user 名下所有活跃 pipeline 的输出 sink。
            # thread 来源优先用 _thread_user_map（断连后仍在内存），再从 registry 补充。
            resumed_tids: set[str] = set()
            for tid in [t for t, u in self._thread_user_map.items() if u == user_id]:
                self._resume_pipeline_for_thread(tid)
                resumed_tids.add(tid)
            # 补充: 从 registry 恢复 _thread_user_map 里没有的活跃 pipeline
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
                    websocket.close(code=code, reason=reason),
                    main_loop,
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
            _matched_tid = (
                entry.thread_id if entry.thread_id else ((entry.tags or {}).get("session_id", "") if entry.tags else "")
            )
            if _matched_tid != thread_id:
                continue
            if entry.engine is None:
                continue
            # 检查引擎是否仍在运行或挂起中
            _engine_running = getattr(entry.engine, "is_running", False)
            _engine_suspended = getattr(entry.engine, "is_suspended", False)
            if not _engine_running and not _engine_suspended:
                continue

            # 找到活跃的 pipeline，无条件把 sink 切到新连接。
            # 重连即"新连接接管"，旧 sink 必然指向已断开连接，直接重建即可，
            # 不必等连续失败判 dead。
            if entry.bridge is not None:
                # 补全 entry.thread_id（历史为空时），便于按 thread 恢复
                if not entry.thread_id and thread_id:
                    entry.thread_id = thread_id
                _sink_user_id = (entry.tags or {}).get("user_id", "")
                _new_sink = create_targeted_sink(
                    self,
                    thread_id,
                    pipeline_id=pipeline_id,
                    user_id=_sink_user_id,
                )
                if _new_sink is not None:
                    entry.bridge.output_sink = _new_sink
                    logger.info(
                        "[WS-Reconnect] 已恢复 pipeline 输出: pipeline=%s thread=%s (重建 sink)",
                        pipeline_id[:12],
                        thread_id[:12],
                    )

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

    async def send_to_thread(self, thread_id: str, event_data: dict) -> bool:
        """向指定 thread_id 关联用户的 WebSocket 连接发送事件。

        单连接架构下按 thread_id → user_id 映射反查，再走 send_to_user 精确路由。
        """
        user_id = self._thread_user_map.get(thread_id, "")
        if user_id:
            return await self.send_to_user(user_id, event_data)

        # thread_id 为空说明是后端任务（CLI/定时触发），没有前端连接是正常的，不打 warning
        if not thread_id:
            logger.debug(
                "send_to_thread: 无活跃连接（后端任务）: type=%s",
                event_data.get("type", "?"),
            )
            return False

        # 无映射兜底：存在全局连接时广播（兼容历史，避免完全断流）。
        if self._global_connections:
            payload = json.dumps(event_data, ensure_ascii=False, default=str)
            sent_any = False
            for uid, ws in list(self._global_connections.items()):
                try:
                    await asyncio.wait_for(ws.send_text(payload), timeout=_SEND_TIMEOUT_SECONDS)
                    sent_any = True
                except (asyncio.TimeoutError, Exception):
                    self._global_connections.pop(uid, None)
            if sent_any:
                return True

        logger.warning(
            "send_to_thread: 无活跃连接: thread_id=%s type=%s global=%s",
            thread_id[:12],
            event_data.get("type", "?"),
            list(self._global_connections.keys()),
        )
        return False

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        """通知前端交互请求已取消。"""
        return await self.send_to_thread(
            thread_id,
            {
                "type": "interaction_cancelled",
                "data": {"request_id": request_id, "reason": reason},
            },
        )

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        """通知前端交互请求已超时。"""
        return await self.send_to_thread(
            thread_id,
            {
                "type": "interaction_timeout",
                "data": {"request_id": request_id},
            },
        )

    async def notify_timeout_reminder(self, request_id, remaining_seconds, thread_id="", **kw) -> bool:
        """通知前端交互请求即将超时。"""
        return await self.send_to_thread(
            thread_id,
            {
                "type": "interaction_timeout_reminder",
                "data": {
                    "request_id": request_id,
                    "remaining_seconds": remaining_seconds,
                },
            },
        )

    async def notify_conversation_start(self, thread_id, tab_id, title, **kw) -> bool:
        return True


ws_interaction_notifier = WebSocketInteractionNotifier()
