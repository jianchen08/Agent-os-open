"""
人类交互服务实现（纯内存版）。

使用内存 dict 存储请求和响应，无外部数据库依赖。

暴露接口：
- get_human_interaction_service：获取全局单例
- set_human_interaction_service：设置全局单例
- reset_human_interaction_service：重置全局单例
- HumanInteractionService：人类交互服务类
- InteractionTimeoutError：交互超时异常
- InteractionCancelledError：交互取消异常
- InteractionDeniedError：交互拒绝异常
"""

import asyncio
import contextlib
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from human.interfaces import (
    IHumanInteractionService,
    IInteractionNotifier,
)
from human.models import (
    InteractionMode,
    InteractionStatus,
    Priority,
    ResponseType,
)

logger = logging.getLogger(__name__)

# choice 请求拒绝记忆的硬上限：异常灌写下逐出最旧（长驻 sidecar 无界增长治理）。
_REJECT_MEMORY_MAX_ENTRIES = 1024

# 拒绝记忆命中时自动拒绝的终局反馈（经 InteractionDeniedError.reason 透传给调用方，
# 最终进入模型的 tool_result 反馈载荷）。
REJECT_MEMORY_FEEDBACK = (
    "该命令/操作已被用户拒绝，不要再次尝试相同或变体命令；"
    "如确有需要，请先向用户说明理由并获得新的明确指示。"
)


def _read_env_seconds(name: str, default: float) -> float:
    """读取秒数配置（环境变量），非法值回退默认；0/负值表示关闭对应机制。"""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _is_deny_option(option_id: str, label: str) -> bool:
    """判断选项是否表达"拒绝"语义（按 id 或 label 文案）。"""
    norm_label = label.strip().lower()
    if norm_label.startswith("拒绝") or "reject" in norm_label or "deny" in norm_label:
        return True
    return option_id.strip().lower() in ("denied", "reject", "rejected")


class InteractionTimeoutError(Exception):
    """交互超时异常。"""

    def __init__(self, request_id: str, timeout: float):
        self.request_id = request_id
        self.timeout = timeout
        super().__init__(f"交互超时: {request_id} (超时时间: {timeout}秒)")


class InteractionCancelledError(Exception):
    """交互取消异常。"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互取消: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class InteractionDeniedError(Exception):
    """交互拒绝异常。"""

    def __init__(self, request_id: str, reason: str | None = None):
        self.request_id = request_id
        self.reason = reason
        message = f"交互拒绝: {request_id}"
        if reason:
            message += f" (原因: {reason})"
        super().__init__(message)


class HumanInteractionService(IHumanInteractionService):
    """
    人类交互服务（纯内存版）。

    使用内存 dict 存储 InteractionRecord，通过 asyncio.Event
    实现请求-响应的异步等待。

    支持：
    - 选择模式：审批确认、澄清问题、方案选择
    - 对话模式：跳转到对话标签页
    """

    def __init__(
        self,
        notifier: IInteractionNotifier | None = None,
        default_timeout: float = 86400.0,
        remind_before_seconds: int = 300,
        choice_max_wait_seconds: float | None = None,
        reject_memory_ttl_seconds: float | None = None,
    ):
        self._notifier = notifier
        self._default_timeout = default_timeout
        self._remind_before_seconds = remind_before_seconds
        # BUG-14 有界等待：choice 请求（审批卡）等待上限，默认 86400s=24h
        # （BUG-40 用户裁定由 600s 放宽），可经
        # HUMAN_INTERACTION_CHOICE_MAX_WAIT_SECONDS 覆盖；非正值 = 关闭上限。
        # 上限同时作用于创建（记录 timeout_seconds + 后台超时任务 + 前端倒计时）
        # 与等待（调用方传更大 timeout 也被收敛），两处同源。
        self._choice_max_wait_seconds = (
            _read_env_seconds("HUMAN_INTERACTION_CHOICE_MAX_WAIT_SECONDS", 86400.0)
            if choice_max_wait_seconds is None
            else choice_max_wait_seconds
        )
        # BUG-14 拒绝记忆：同 (session_id, title) 的 choice 请求被用户拒绝（或
        # 超时按拒绝裁决）后，TTL 内重试不再弹卡、直接按拒绝裁决并回终局反馈。
        # 会话+标题作用域隔离（跨会话/跨标题不受牵连），TTL 滑动刷新（持续重试
        # 期间压制不失效，模型停止后到期恢复），默认 1800s，非正值 = 关闭记忆。
        self._reject_memory_ttl_seconds = (
            _read_env_seconds("HUMAN_INTERACTION_REJECT_MEMORY_TTL_SECONDS", 1800.0)
            if reject_memory_ttl_seconds is None
            else reject_memory_ttl_seconds
        )
        # (session_id, title) → 最近一次拒绝判定的单调时间戳
        self._reject_memory: dict[tuple[str, str], float] = {}
        self._pending_events: dict[str, asyncio.Event] = {}
        self._pending_event_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._requests: dict[str, dict[str, Any]] = {}
        self._responses: dict[str, dict[str, Any]] = {}

    def _set_event_threadsafe(self, request_id: str) -> None:
        """线程安全地设置 Event，确保跨事件循环时也能正确唤醒等待方。

        当 wait_for_choice() 在引擎线程的事件循环中等待，
        而 submit_response() 在 API 路由的事件循环中调用 set() 时，
        直接调用 Event.set() 无法唤醒另一个循环中的 wait()。
        需要通过 call_soon_threadsafe 将 set 操作调度到等待方的事件循环。
        """
        event = self._pending_events.get(request_id)
        if event is None:
            logger.warning(
                "[HumanInteraction] Event NOT found | request_id=%s | pending_keys=%s",
                request_id,
                list(self._pending_events.keys())[:5],
            )
            return

        target_loop = self._pending_event_loops.get(request_id)
        current_loop: asyncio.AbstractEventLoop | None = None
        with contextlib.suppress(RuntimeError):
            current_loop = asyncio.get_running_loop()

        if target_loop is not None and target_loop.is_running() and current_loop is not target_loop:
            logger.info(
                "[HumanInteraction] Event.set() via call_soon_threadsafe | request_id=%s | target_loop=%s | current_loop=%s",
                request_id,
                id(target_loop),
                id(current_loop) if current_loop else None,
            )
            target_loop.call_soon_threadsafe(event.set)
        else:
            logger.info(
                "[HumanInteraction] Event.set() direct | request_id=%s | target_loop=%s | current_loop=%s",
                request_id,
                id(target_loop) if target_loop else None,
                id(current_loop) if current_loop else None,
            )
            event.set()

    async def send_notification(
        self,
        session_id: str,
        thread_id: str,
        title: str,
        message: str = "",
        priority: Priority = Priority.NORMAL,
        progress: float | None = None,
        agent_id: str | None = None,
        file_paths: list[str] | None = None,
        user_id: str | None = None,
    ) -> str:
        """发送非阻塞通知，不等待用户响应，立即返回 request_id。"""
        request_id = str(uuid4())
        record = self._make_request_record(
            request_id=request_id,
            session_id=session_id,
            mode=InteractionMode.NOTIFICATION,
            title=title,
            description=message,
            thread_id=thread_id,
            tab_id="",
            user_id=user_id,
            agent_id=agent_id,
            extra={
                "progress": progress,
                "priority": priority.value,
                "file_paths": file_paths,
            },
        )
        self._requests[request_id] = record
        # 不创建 asyncio.Event，不等待 —— 非阻塞核心逻辑
        if self._notifier:
            await self._notifier.notify_request(record)

        logger.info(
            "[HumanInteraction] 发送通知 | request_id=%s | title=%s",
            request_id,
            title,
        )
        return request_id

    async def create_choice_request(
        self,
        session_id: str,
        thread_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        options: list[dict[str, Any]] | None = None,
        questions: list[str] | None = None,
        timeout_seconds: int | None = None,
        priority: Priority = Priority.NORMAL,
        user_id: str | None = None,
        agent_id: str | None = None,
        file_paths: list[str] | None = None,
        agent_level: str | None = None,
        pipeline_id: str | None = None,
    ) -> str:
        """创建选择模式请求，返回 request_id。

        BUG-14 有界等待：timeout_seconds 收敛到 choice 等待上限（默认 86400s=24h，
        BUG-40 放宽），审批卡不再永久等待；超时按拒绝裁决（见 wait_for_choice/_handle_timeout）。
        BUG-14 拒绝记忆：同 (session_id, title) 的请求在拒绝记忆窗口内重试时，
        不再弹卡（不通知前端、不建等待事件），直接落已拒绝终态——wait_for_choice
        对其立即抛 InteractionDeniedError（携带终局反馈）。
        """
        request_id = str(uuid4())
        timeout: float = timeout_seconds or int(self._default_timeout)
        if self._choice_max_wait_seconds > 0:
            timeout = min(timeout, self._choice_max_wait_seconds)

        record = self._make_request_record(
            request_id=request_id,
            session_id=session_id,
            mode=InteractionMode.CHOICE,
            title=title,
            description=description,
            thread_id=thread_id,
            tab_id=tab_id,
            user_id=user_id,
            agent_id=agent_id,
            extra={
                "options": options,
                "questions": questions,
                "timeout_seconds": timeout,
                "priority": priority.value,
                "timeout_reminded": False,
                "file_paths": file_paths,
                **({"agent_level": agent_level} if agent_level else {}),
                **({"pipeline_id": pipeline_id} if pipeline_id else {}),
            },
        )

        # 拒绝记忆命中：直接按拒绝裁决（滑窗刷新），不弹卡不等待
        if self._reject_memory_hit(session_id, title):
            record["status"] = InteractionStatus.COMPLETED.value
            self._requests[request_id] = record
            self._responses[request_id] = self._make_response_record(
                request_id=request_id,
                session_id=session_id,
                response_type=ResponseType.DENIED.value,
                selected_option=None,
                answers=None,
                feedback=REJECT_MEMORY_FEEDBACK,
            )
            logger.info(
                "[HumanInteraction] 拒绝记忆命中，重试直接拒绝（不弹卡）| "
                "request_id=%s | session_id=%s | title=%s",
                request_id,
                session_id,
                title,
            )
            return request_id

        self._requests[request_id] = record

        async with self._lock:
            self._pending_events[request_id] = asyncio.Event()

        if self._notifier:
            await self._notifier.notify_request(record)

        self._setup_timeout(request_id, timeout, thread_id)

        logger.info(
            "[HumanInteraction] 创建选择请求 | request_id=%s | title=%s",
            request_id,
            title,
        )
        return request_id

    async def create_conversation_request(
        self,
        session_id: str,
        thread_id: str,
        tab_id: str,
        title: str,
        description: str = "",
        initial_message: str | None = None,
        suggestions: list[str] | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
        file_paths: list[str] | None = None,
        agent_level: str | None = None,
        pipeline_id: str | None = None,
    ) -> str:
        """创建对话模式请求，返回 request_id。"""
        request_id = str(uuid4())

        record = self._make_request_record(
            request_id=request_id,
            session_id=session_id,
            mode=InteractionMode.CONVERSATION,
            title=title,
            description=description,
            thread_id=thread_id,
            tab_id=tab_id,
            user_id=user_id,
            agent_id=agent_id,
            extra={
                "initial_message": initial_message,
                "suggestions": suggestions,
                "file_paths": file_paths,
                **({"agent_level": agent_level} if agent_level else {}),
                **({"pipeline_id": pipeline_id} if pipeline_id else {}),
            },
        )
        self._requests[request_id] = record

        async with self._lock:
            self._pending_events[request_id] = asyncio.Event()

        if self._notifier:
            await self._notifier.notify_request(record)
            await self._notifier.notify_conversation_start(
                thread_id=thread_id,
                tab_id=tab_id,
                title=title,
                request_id=request_id,
                initial_message=initial_message,
                suggestions=suggestions,
            )

        logger.info(
            "[HumanInteraction] 创建对话请求 | request_id=%s | thread_id=%s",
            request_id,
            thread_id,
        )
        return request_id

    async def wait_for_conversation_arrival(
        self,
        request_id: str,
        timeout: float = 86400.0,
    ) -> dict[str, Any]:
        """等待用户到达对话页面。"""
        event = self._pending_events.get(request_id)
        if not event:
            return {"status": "timeout", "message": "用户未到达对话页面"}

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return {"status": "timeout", "message": f"用户在 {timeout} 秒内未到达对话页面"}

        return {"status": "arrived", "message": "用户已到达对话页面"}

    async def wait_for_choice(
        self,
        request_id: str,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """等待用户选择。

        BUG-14：终态请求（已拒绝/已取消/已超时，如拒绝记忆自动拒绝的请求）
        不再补建事件空等——直读已存响应立即收敛：拒绝抛 InteractionDeniedError、
        取消抛 InteractionCancelledError；choice 记录的等待时长收敛到
        choice 等待上限（调用方传更大 timeout 也被收敛）。
        """
        current_loop = asyncio.get_running_loop()
        current_loop_id = id(current_loop)

        record = self._requests.get(request_id)
        if record is None:
            raise ValueError(f"请求不存在: {request_id}")

        # 终态请求直读已存响应（拒绝记忆自动拒绝的请求无事件，必须在此收敛）
        if record.get("status") != InteractionStatus.PENDING.value:
            response = self._responses.get(request_id)
            if response is not None:
                resp_data = response.get("message_data", {})
                resp_type = resp_data.get("response_type", "")
                if resp_type == ResponseType.DENIED.value:
                    raise InteractionDeniedError(request_id, resp_data.get("feedback"))
                if resp_type == ResponseType.CANCELLED.value:
                    raise InteractionCancelledError(request_id, resp_data.get("feedback"))
                return {
                    "request_id": request_id,
                    "response_type": resp_type,
                    "selected_option": resp_data.get("selected_option"),
                    "answers": resp_data.get("answers"),
                    "feedback": resp_data.get("feedback"),
                }
            if record.get("status") == InteractionStatus.TIMEOUT.value:
                raise InteractionTimeoutError(request_id, self._effective_wait_timeout(record))
            if record.get("status") == InteractionStatus.CANCELLED.value:
                raise InteractionCancelledError(
                    request_id, (record.get("message_data") or {}).get("cancel_reason")
                )

        event = self._pending_events.get(request_id)
        if not event:
            async with self._lock:
                self._pending_events[request_id] = asyncio.Event()
                event = self._pending_events[request_id]

        # 记录等待方所在的事件循环，供 _set_event_threadsafe 使用
        self._pending_event_loops[request_id] = current_loop

        timeout = self._effective_wait_timeout(record, timeout)

        try:
            logger.info(
                "[HumanInteraction] wait_for_choice() 开始等待 | request_id=%s | timeout=%s | loop_id=%s | event_id=%s | event_is_set=%s",
                request_id,
                timeout,
                current_loop_id,
                id(event),
                event.is_set(),
            )
            await asyncio.wait_for(event.wait(), timeout=timeout)
            logger.info(
                "[HumanInteraction] wait_for_choice() 被唤醒 | request_id=%s",
                request_id,
            )
        except TimeoutError:
            await self._handle_timeout(request_id)
            self._cancel_timeout_task(request_id)
            raise InteractionTimeoutError(request_id, timeout) from None
        finally:
            self._pending_event_loops.pop(request_id, None)

        response = self._responses.get(request_id)
        if not response:
            logger.error(
                "[HumanInteraction] wait_for_choice() 被唤醒但无 response | request_id=%s",
                request_id,
            )
            raise InteractionTimeoutError(request_id, timeout)

        resp_data = response.get("message_data", {})
        resp_type = resp_data.get("response_type", "")

        if resp_type == ResponseType.DENIED.value:
            raise InteractionDeniedError(request_id, resp_data.get("feedback"))

        if resp_type == ResponseType.CANCELLED.value:
            raise InteractionCancelledError(request_id, resp_data.get("feedback"))

        return {
            "request_id": request_id,
            "response_type": resp_type,
            "selected_option": resp_data.get("selected_option"),
            "answers": resp_data.get("answers"),
            "feedback": resp_data.get("feedback"),
        }

    def _effective_wait_timeout(self, record: dict[str, Any], timeout: float | None = None) -> float:
        """解析等待时长：显式 timeout 优先，缺省取记录/默认；choice 记录收敛到上限。"""
        msg_data = record.get("message_data", {})
        resolved = timeout or msg_data.get("timeout_seconds") or self._default_timeout
        if (
            msg_data.get("interaction_mode") == InteractionMode.CHOICE.value
            and self._choice_max_wait_seconds > 0
        ):
            resolved = min(resolved, self._choice_max_wait_seconds)
        return resolved

    def _memorize_rejection(self, session_id: str, title: str) -> None:
        """记录一次拒绝判定（用户拒绝或超时按拒绝裁决），滑窗起点 = 现在。"""
        if self._reject_memory_ttl_seconds <= 0:
            return
        if len(self._reject_memory) >= _REJECT_MEMORY_MAX_ENTRIES:
            oldest = min(self._reject_memory.items(), key=lambda kv: kv[1])
            self._reject_memory.pop(oldest[0], None)
        self._reject_memory[(session_id, title)] = time.monotonic()

    def _reject_memory_hit(self, session_id: str, title: str) -> bool:
        """拒绝记忆是否命中（TTL 内有拒绝判定）；命中即滑窗刷新。"""
        ts = self._reject_memory.get((session_id, title))
        if ts is None:
            return False
        active = (time.monotonic() - ts) < self._reject_memory_ttl_seconds
        if active:
            self._reject_memory[(session_id, title)] = time.monotonic()
        else:
            self._reject_memory.pop((session_id, title), None)
        return active

    @staticmethod
    def _is_denial_submission(
        record: dict[str, Any],
        response_type: str,
        selected_option: str | None,
    ) -> bool:
        """判定一次响应是否为用户拒绝（choice 记录的拒绝记忆触发器）。

        两条路径：response_type=denied（不经前端的直接拒绝）；或前端审批卡的
        answered + 选中"拒绝"语义选项（label/id 命中拒绝词汇）。
        """
        if response_type == ResponseType.DENIED.value:
            return True
        if not selected_option:
            return False
        options = (record.get("message_data") or {}).get("options") or []
        for opt in options:
            if not isinstance(opt, dict):
                continue
            oid = str(opt.get("id", ""))
            label = str(opt.get("label", ""))
            if selected_option in (oid, label) and _is_deny_option(oid, label):
                return True
        return False

    @staticmethod
    def _make_response_record(
        request_id: str,
        session_id: str,
        response_type: str,
        selected_option: str | None,
        answers: list[str] | None,
        feedback: str | None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """构建响应记录（submit_response 与拒绝记忆自动拒绝共用同一形状）。"""
        return {
            "id": str(uuid4()),
            "session_id": session_id,
            "parent_record_id": request_id,
            "type": "interaction_response",
            "status": "completed",
            "message_data": {
                "request_id": request_id,
                "response_type": response_type,
                "selected_option": selected_option,
                "answers": answers,
                "feedback": feedback,
                "user_id": user_id,
            },
        }

    async def respond(self, request_id: str, resp_data: dict[str, Any]) -> bool:
        """处理前端交互响应，解析嵌套数据并路由到 submit_response。"""
        inner = resp_data.get("response", {})
        if not isinstance(inner, dict):
            inner = {}

        response_type = inner.get("response_type", "answered")
        selected_option = inner.get("selected_option")
        feedback = inner.get("feedback")
        answers = inner.get("answers")

        logger.info(
            "[HumanInteraction] respond() | request_id=%s | type=%s | option=%s",
            request_id,
            response_type,
            selected_option,
        )

        result = await self.submit_response(
            request_id=request_id,
            response_type=response_type,
            selected_option=selected_option,
            answers=answers,
            feedback=feedback,
        )

        logger.info(
            "[HumanInteraction] respond() result=%s | request_id=%s",
            result,
            request_id,
        )
        return result

    async def submit_response(
        self,
        request_id: str,
        response_type: str,
        selected_option: str | None = None,
        answers: list[str] | None = None,
        feedback: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        """提交响应。"""
        request_record = self._requests.get(request_id)
        if not request_record:
            logger.warning(
                "[HumanInteraction] 请求不存在 | request_id=%s | 已知 requests=%s",
                request_id,
                list(self._requests.keys())[-5:],
            )
            return False

        if request_record.get("status") != InteractionStatus.PENDING.value:
            logger.warning(
                "[HumanInteraction] 请求状态不允许响应 | request_id=%s | status=%s",
                request_id,
                request_record.get("status"),
            )
            return False

        # BUG-14 拒绝记忆：choice 请求被用户拒绝（直接 denied 或选中拒绝选项）
        # 即武装记忆——同 (session_id, title) 的后续请求不再弹卡，直接按拒绝裁决。
        if (
            (request_record.get("message_data") or {}).get("interaction_mode")
            == InteractionMode.CHOICE.value
            and self._is_denial_submission(request_record, response_type, selected_option)
        ):
            self._memorize_rejection(
                str(request_record.get("session_id") or ""),
                str((request_record.get("message_data") or {}).get("title") or ""),
            )

        now = datetime.now(UTC).isoformat()

        self._responses[request_id] = self._make_response_record(
            request_id=request_id,
            session_id=str(request_record.get("session_id") or ""),
            response_type=response_type,
            selected_option=selected_option,
            answers=answers,
            feedback=feedback,
            user_id=user_id,
        )

        request_record["status"] = InteractionStatus.COMPLETED.value
        msg_data = request_record.setdefault("message_data", {})
        msg_data["responded_at"] = now

        async with self._lock:
            event_exists = request_id in self._pending_events
            if event_exists:
                logger.info(
                    "[HumanInteraction] submit_response() 准备唤醒 | request_id=%s | event_exists=%s",
                    request_id,
                    event_exists,
                )
                self._set_event_threadsafe(request_id)
            else:
                logger.warning(
                    "[HumanInteraction] Event NOT found | request_id=%s | pending_keys=%s",
                    request_id,
                    list(self._pending_events.keys())[:5],
                )
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        logger.info(
            "[HumanInteraction] 响应已提交 | request_id=%s | response_type=%s",
            request_id,
            response_type,
        )
        return True

    async def mark_as_viewed(self, request_id: str) -> bool:
        """标记请求为已查看，conversation 模式下触发到达通知。"""
        record = self._requests.get(request_id)
        if not record or record.get("status") != InteractionStatus.PENDING.value:
            return False

        record["status"] = InteractionStatus.VIEWED.value
        record.setdefault("message_data", {})["viewed_at"] = datetime.now(UTC).isoformat()

        async with self._lock:
            if request_id in self._pending_events:
                self._set_event_threadsafe(request_id)

        return True

    async def cancel_request(
        self,
        request_id: str,
        reason: str | None = None,
    ) -> bool:
        """取消请求。"""
        record = self._requests.get(request_id)
        if not record:
            return False

        status = record.get("status")
        if status in (
            InteractionStatus.COMPLETED.value,
            InteractionStatus.TIMEOUT.value,
            InteractionStatus.CANCELLED.value,
        ):
            return False

        record["status"] = InteractionStatus.CANCELLED.value
        # 记录取消原因：wait 终态直读路径收敛为取消时携带（反馈面可归因）
        record.setdefault("message_data", {})["cancel_reason"] = reason

        async with self._lock:
            if request_id in self._pending_events:
                self._set_event_threadsafe(request_id)
            if request_id in self._timeout_tasks:
                self._timeout_tasks[request_id].cancel()
                del self._timeout_tasks[request_id]

        if self._notifier:
            msg_data = record.get("message_data") or {}
            await self._notifier.notify_cancel(
                request_id,
                reason,
                thread_id=msg_data.get("thread_id", ""),
            )

        logger.info(
            "[HumanInteraction] 请求已取消 | request_id=%s | reason=%s",
            request_id,
            reason,
        )
        return True

    async def auto_complete_conversation_for_pipeline(self, pipeline_id: str) -> int:
        """自动完成指定管道的 pending conversation 模式交互请求。

        当用户通过聊天框发消息时，如果引擎正阻塞在 human_interaction
        (conversation 模式) 的 wait_for_choice() 上，_run_loop 无法进入
        下一轮迭代消费 _pending_notifications。通过自动完成交互请求，
        工具返回 conversation_mode=True，管道正确挂起后立即被通知唤醒。

        仅自动完成 conversation 模式（用户发消息 = 已到达对话页面），
        不触碰 choice 模式（需要用户显式选择选项）。

        Args:
            pipeline_id: 管道 ID（对应 request 中的 session_id）

        Returns:
            被自动完成的请求数量
        """
        completed = 0
        for request_id, record in list(self._requests.items()):
            if record.get("status") != InteractionStatus.PENDING.value:
                continue
            if record.get("session_id") != pipeline_id:
                continue
            msg_data = record.get("message_data", {})
            if msg_data.get("interaction_mode") != InteractionMode.CONVERSATION.value:
                continue
            try:
                await self.submit_response(
                    request_id=request_id,
                    response_type=ResponseType.APPROVED.value,
                )
                completed += 1
                logger.info(
                    "[HumanInteraction] 自动完成 conversation 请求 | request_id=%s | pipeline_id=%s",
                    request_id,
                    pipeline_id,
                )
            except Exception as exc:
                logger.warning(
                    "[HumanInteraction] 自动完成失败 | request_id=%s | error=%s",
                    request_id,
                    exc,
                )
        return completed

    async def cancel_pending_for_thread(self, thread_id: str, reason: str = "new_message_arrived") -> int:
        """取消指定 thread 关联的所有 pending 交互请求。

        当用户通过聊天框发送新消息时，如果引擎正在等待 human_interaction 响应，
        需要取消 pending 请求以解除 _run_loop 的阻塞，让新消息能被消费。

        Args:
            thread_id: 线程/管道 ID
            reason: 取消原因

        Returns:
            被取消的请求数量
        """
        cancelled = 0
        for request_id, record in list(self._requests.items()):
            if record.get("status") != InteractionStatus.PENDING.value:
                continue
            record_thread = record.get("thread_id") or record.get("session_id") or ""
            if record_thread != thread_id:
                continue
            try:
                await self.cancel_request(request_id, reason=reason)
                cancelled += 1
                logger.info(
                    "[HumanInteraction] 取消 pending 请求（新消息到达）| request_id=%s | thread_id=%s",
                    request_id,
                    thread_id,
                )
            except Exception as exc:  # noqa: BLE001 — 单请求取消失败不阻断其余清理
                logger.debug("[HumanInteraction] 取消 pending 请求失败（继续其余清理）| request_id=%s | error=%s", request_id, exc)
        return cancelled

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        """获取请求详情。"""
        return self._requests.get(request_id)

    async def get_pending_requests(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取待处理请求列表。"""
        results: list[dict[str, Any]] = []
        for record in self._requests.values():
            if record.get("status") != InteractionStatus.PENDING.value:
                continue
            msg_data = record.get("message_data") or {}
            if session_id and record.get("session_id") != session_id:
                continue
            if user_id and msg_data.get("user_id") != user_id:
                continue
            results.append(record)
            if len(results) >= limit:
                break
        return results

    async def get_interaction_history(
        self,
        session_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """获取交互历史。"""
        results: list[dict[str, Any]] = []
        for record in self._requests.values():
            if record.get("session_id") == session_id:
                results.append(record)
        for resp in self._responses.values():
            if resp.get("session_id") == session_id:
                results.append(resp)
        return results[:limit]

    def set_notifier(self, notifier: IInteractionNotifier) -> None:
        """设置通知器。"""
        self._notifier = notifier

    def _make_request_record(
        self,
        request_id: str,
        session_id: str,
        mode: InteractionMode,
        title: str,
        description: str,
        thread_id: str,
        tab_id: str,
        user_id: str | None,
        agent_id: str | None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """构建请求记录字典。"""
        message_data: dict[str, Any] = {
            "interaction_mode": mode.value,
            "title": title,
            "description": description,
            "thread_id": thread_id,
            "tab_id": tab_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "viewed_at": None,
        }
        if extra:
            message_data.update(extra)

        return {
            "id": request_id,
            "session_id": session_id,
            "type": "interaction_request",
            "status": InteractionStatus.PENDING.value,
            # created_at：前端审批卡倒计时起点（BUG-14 有界等待可见化）
            "created_at": datetime.now(UTC).isoformat(),
            "message_data": message_data,
        }

    def _setup_timeout(self, request_id: str, timeout_seconds: float, thread_id: str = ""):
        """设置超时任务（F-HI-1：短超时按时收敛 + 任务结束自清理）。

        两种时间尺度：
        - 长超时（timeout >= remind）：``sleep(timeout - remind)`` → 提醒 →
          ``sleep(remind)`` → 超时处理；
        - 短超时（timeout < remind）：**不提醒**，直接 ``sleep(timeout)`` →
          超时处理（短超时按时收敛，不误发"即将超时"提醒）。
        """
        short_timeout = timeout_seconds < self._remind_before_seconds

        async def timeout_handler():
            try:
                if short_timeout:
                    await asyncio.sleep(timeout_seconds)
                    await self._handle_timeout(request_id)
                    return

                await asyncio.sleep(timeout_seconds - self._remind_before_seconds)

                record = self._requests.get(request_id)
                if record and record.get("status") == InteractionStatus.PENDING.value:
                    if self._notifier:
                        msg_data = record.get("message_data") or {}
                        await self._notifier.notify_timeout_reminder(
                            request_id,
                            self._remind_before_seconds,
                            thread_id,
                            title=msg_data.get("title", ""),
                            mode=msg_data.get("interaction_mode", "choice"),
                            options=msg_data.get("options"),
                            questions=msg_data.get("questions"),
                        )

                    record.setdefault("message_data", {})["timeout_reminded"] = True

                await asyncio.sleep(self._remind_before_seconds)

                await self._handle_timeout(request_id)

            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error("[HumanInteraction] 超时处理失败 | error=%s", e)
            finally:
                # 任务结束（正常/取消/异常）都从登记表移除，防悬挂登记
                if self._timeout_tasks.get(request_id) is asyncio.current_task():
                    self._timeout_tasks.pop(request_id, None)

        task = asyncio.create_task(timeout_handler())
        self._timeout_tasks[request_id] = task

    def _cancel_timeout_task(self, request_id: str) -> None:
        """取消并移除请求的后台超时任务（wait_for_choice 超时/完成时调用）。

        审批闭环要求超时"一锤定音"：wait_for_choice 已抛超时后，后台任务若仍
        存活，会悬挂到 +remind 秒才醒来，期间占着事件循环且可能重复触发通知。
        """
        task = self._timeout_tasks.get(request_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._timeout_tasks.pop(request_id, None)

    async def _handle_timeout(self, request_id: str):
        """处理超时（幂等：非 PENDING 状态不再处理/通知）。

        幂等守卫：wait_for_choice 超时与后台 timeout_handler 会在同一时刻竞争
        进入本方法（审批闭环最常见的竞态窗口）。状态检查与置位之间无 await 间隙
        （仅同步代码），先到者把 status 置 TIMEOUT 并通知，后到者直接返回——
        notify_timeout 恰好只触发一次。
        """
        record = self._requests.get(request_id)
        thread_id = ""
        if record:
            msg_data = record.get("message_data") or {}
            thread_id = msg_data.get("thread_id", "")
            if record.get("status") != InteractionStatus.PENDING.value:
                return  # 幂等：已被响应/取消/已超时处理
            record["status"] = InteractionStatus.TIMEOUT.value
            # BUG-14：choice 超时 = 按拒绝裁决，同样武装拒绝记忆（重试不弹卡）
            if msg_data.get("interaction_mode") == InteractionMode.CHOICE.value:
                self._memorize_rejection(
                    str(record.get("session_id") or ""), str(msg_data.get("title") or "")
                )

        async with self._lock:
            if request_id in self._pending_events:
                self._set_event_threadsafe(request_id)

        if self._notifier:
            await self._notifier.notify_timeout(request_id, thread_id=thread_id)

        logger.info("[HumanInteraction] 请求超时 | request_id=%s", request_id)


_service_instance: HumanInteractionService | None = None


def get_human_interaction_service() -> HumanInteractionService:
    """获取服务单例。"""
    global _service_instance  # noqa: PLW0603
    if _service_instance is None:
        _service_instance = HumanInteractionService()
    return _service_instance


def set_human_interaction_service(service: HumanInteractionService) -> None:
    """设置服务单例。"""
    global _service_instance  # noqa: PLW0603
    _service_instance = service


def reset_human_interaction_service() -> None:
    """重置服务单例（用于测试）。"""
    global _service_instance  # noqa: PLW0603
    _service_instance = None
