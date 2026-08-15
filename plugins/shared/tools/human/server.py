#!/usr/bin/env python3
"""Human Interaction 插件——双角色（LLM 工具 + 交互服务）。

合并自原 tools/human（工具）和 system/human_interaction（服务）两个 sidecar。
现在单进程自包含，service 状态唯一：

  角色 1：LLM 工具（human_interaction）
    LLM 调 choice/conversation/notification → 进程内直接调 service → event-bus.notify → 前端

  角色 2：交互服务（interaction.* 工具 + provides: human-interaction）
    其他插件（approval/security_check 等）或前端响应回路经 capability 调：
      其他插件 → cap.call("human-interaction","create_choice") → 内核 McpBridge
      → invoke_tool(本插件, "interaction.create_choice") → 进程内 service

链路（notification，3 跳）：
    LLM 调 human_interaction 工具
      → handler 进程内调 service.send_notification
      → service 经 _EventBusNotifier.notify → event-bus.emit → 内核 → 前端
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 包路径导入（human.models / human.service 等）需要 tools 目录在 sys.path
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from human.interfaces import IInteractionNotifier  # noqa: E402
from human.models import InteractionMode, Priority, ResponseType  # noqa: E402
from human.service import (  # noqa: E402
    HumanInteractionService,
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("human_interaction_tool")

# 进程内 service 单例（on_load 时初始化，注入 _EventBusNotifier）
_service: HumanInteractionService | None = None


class _EventBusNotifier(IInteractionNotifier):
    """把交互事件经 event-bus capability 推到前端（fire-and-forget）。

    一个 _emit 方法统一调 plugin.get_capability("event-bus").notify("emit", ...)，
    5 个 notify_xxx 只是构造不同 event 名/payload。用 notify 而非 call：避免
    service 在执行工具期间发起的 emit 与内核的 tools/call 响应形成嵌套死锁。
    """

    def __init__(self, plugin_ref: Any) -> None:
        self._plugin = plugin_ref

    def _bus(self) -> Any | None:
        try:
            return self._plugin.get_capability("event-bus")
        except (KeyError, AttributeError):
            return None

    async def _emit(self, event: str, payload: dict[str, Any], thread_id: str = "") -> bool:
        bus = self._bus()
        if bus is None:
            logger.warning("[HumanInteraction] event-bus not injected; skip %s", event)
            return False
        try:
            await bus.notify("emit", {"event": event, "payload": payload, "thread_id": thread_id})
            return True
        except Exception:
            logger.exception("[HumanInteraction] emit %s failed", event)
            return False

    async def notify_request(self, request: Any) -> bool:
        record = request if isinstance(request, dict) else {}
        msg = record.get("message_data", {}) if isinstance(record.get("message_data"), dict) else {}
        payload: dict[str, Any] = {
            "request_id": record.get("id", ""),
            "session_id": record.get("session_id", ""),
            "status": record.get("status", ""),
            "thread_id": msg.get("thread_id", ""),
            "tab_id": msg.get("tab_id", ""),
            "interaction_mode": msg.get("interaction_mode", ""),
            "title": msg.get("title", ""),
            "description": msg.get("description", ""),
        }
        for key in ("options", "questions", "initial_message", "suggestions",
                    "file_paths", "progress", "priority", "timeout_seconds",
                    "agent_level", "pipeline_id", "agent_id"):
            if msg.get(key) is not None:
                payload[key] = msg[key]
        return await self._emit("interaction_request", payload, payload.get("thread_id", ""))

    async def notify_cancel(self, request_id: str, reason: str | None = None, thread_id: str = "") -> bool:
        return await self._emit("interaction_cancelled",
                                {"request_id": request_id, "reason": reason}, thread_id)

    async def notify_timeout(self, request_id: str, thread_id: str = "") -> bool:
        return await self._emit("interaction_timeout", {"request_id": request_id}, thread_id)

    async def notify_timeout_reminder(
        self, request_id: str, remaining_seconds: int, thread_id: str = "", *,
        title: str = "", mode: str = "",
        options: list[dict] | None = None, questions: list[str] | None = None,
    ) -> bool:
        logger.info("[HumanInteraction] timeout reminder | request_id=%s | remaining=%ss",
                    request_id, remaining_seconds)
        return True

    async def notify_conversation_start(
        self, thread_id: str, tab_id: str, title: str, request_id: str = "",
        initial_message: str | None = None, suggestions: list[str] | None = None,
    ) -> bool:
        return await self._emit("interaction_conversation_start", {
            "request_id": request_id, "thread_id": thread_id, "tab_id": tab_id,
            "title": title, "initial_message": initial_message, "suggestions": suggestions,
        }, thread_id)


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """初始化 service 单例，注入 EventBusNotifier。"""
    global _service
    _service = HumanInteractionService()
    _service.set_notifier(_EventBusNotifier(plugin))
    logger.info("Human interaction service initialized (dual-role: tool + service)")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    global _service
    _service = None
    logger.info("Human interaction service unloaded")


# ════════════════════════════════════════════════════════════════════
# 角色 1：LLM 工具（human_interaction）
# LLM 调此工具发起交互。进程内直接调 service，零中转。
# ════════════════════════════════════════════════════════════════════


def _normalize_options(raw: Any) -> list[dict[str, Any]] | None:
    """LLM 传参容错：把 options 归一化为前端 InteractionOption 的 [{id,label}]。

    schema 只声明 ``array``，LLM 常按直觉传字符串数组（``["批准","拒绝"]``）；
    前端 InteractionCard 渲染 ``opt.label``，字符串元素会导致按钮文字为空，
    表现为"审批卡片只有输入框、没有选项按钮"。
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            out.append({"id": str(i), "label": item})
        elif isinstance(item, dict):
            label = item.get("label") or item.get("text") or item.get("name")
            if not label:
                continue
            entry: dict[str, Any] = {"id": str(item.get("id", i)), "label": label}
            if item.get("description"):
                entry["description"] = item["description"]
            out.append(entry)
    return out or None

@plugin.tool(
    name="human_interaction",
    schema={
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["choice", "conversation", "notification"]},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "options": {"type": "array"},
            "questions": {"type": "array"},
            "initial_message": {"type": "string"},
            "file_paths": {"type": "array"},
            "timeout_seconds": {"type": "number", "default": 86400},
            "priority": {"type": "string", "default": "normal"},
        },
        "required": ["mode", "title"],
    },
    description="与用户交互（choice/conversation/notification 三种模式）",
)
async def human_interaction(**kwargs: Any) -> dict[str, Any]:
    """LLM 工具入口——进程内直接调 service，零跨进程中转。"""
    # 冷启动竞态自愈：内核 spawn 完成后 on_load 是 fire-and-forget 通知
    # （invoker.rs get_or_create_mcp_client），紧随其后的首次 tools/call 可能
    # 抢在 _on_load（初始化 _service）之前到达——立即返回 error 会让 LLM 误判
    # "工具不可用"并放弃。on_load 完成后 _service 全局可见，短暂等待即可跨过窗口。
    global _service
    if _service is None:
        for _ in range(50):  # 最多 ~10s，覆盖 Python import 慢的冷启动
            await asyncio.sleep(0.2)
            if _service is not None:
                break
    if _service is None:
        return {"error": "service not initialized (on_load not finished in 10s)"}

    # 兼容 LLM 偶发的参数别名（type→mode、message→title），避免参数名错配
    # 把 notification 误判成阻塞 choice。notification 语义为非阻塞；
    # mode 缺省值不得使 notification 退化为 wait_for_choice。
    if "mode" not in kwargs and "type" in kwargs:
        kwargs["mode"] = kwargs["type"]
    if not kwargs.get("title") and kwargs.get("message"):
        kwargs["title"] = kwargs["message"]

    mode = kwargs.get("mode")
    if mode not in ("choice", "conversation", "notification"):
        return {"error": "参数 mode 必填，取值 choice/conversation/notification"}

    pipeline_id = kwargs.get("pipeline_id") or kwargs.get("session_id") or ""
    session_id = kwargs.get("session_id") or pipeline_id
    timeout = kwargs.get("timeout_seconds", 86400)

    try:
        if mode == InteractionMode.NOTIFICATION.value:
            return await _do_notification(kwargs, session_id)
        if mode == InteractionMode.CHOICE.value:
            return await _do_choice(kwargs, session_id, pipeline_id, timeout)
        if mode == InteractionMode.CONVERSATION.value:
            return await _do_conversation(kwargs, session_id, pipeline_id, timeout)
        return {"error": f"不支持的交互模式: {mode}"}
    except InteractionTimeoutError as e:
        return {"error": f"人类交互超时（{e.timeout}秒）", "error_code": "INTERACTION_TIMEOUT"}
    except InteractionCancelledError as e:
        return {"error": f"交互已取消: {e.reason or '用户取消'}", "error_code": "INTERACTION_CANCELLED"}
    except InteractionDeniedError as e:
        return {"status": "denied", "selected_option": "用户拒绝", "reason": e.reason or "用户拒绝"}
    except Exception as exc:
        logger.exception("[human_interaction] 执行失败")
        return {"error": f"人类交互执行失败: {exc}"}


async def _do_notification(kwargs: dict[str, Any], session_id: str) -> dict[str, Any]:
    """通知模式——非阻塞，创建 record + event-bus.notify 后立即返回。"""
    rid = await _service.send_notification(
        session_id=session_id,
        thread_id=session_id,
        title=kwargs.get("title", ""),
        message=kwargs.get("description") or kwargs.get("initial_message") or "",
        priority=Priority(kwargs.get("priority", "normal")),
        agent_id=kwargs.get("pipeline_id"),
    )
    return {"status": "sent", "request_id": rid}


async def _do_choice(
    kwargs: dict[str, Any], session_id: str, pipeline_id: str, timeout: int
) -> dict[str, Any]:
    """选择模式——创建请求 + 阻塞等待用户选择。"""
    rid = await _service.create_choice_request(
        session_id=session_id,
        thread_id=session_id,
        tab_id=pipeline_id,
        title=kwargs.get("title", ""),
        description=kwargs.get("description", ""),
        options=_normalize_options(kwargs.get("options")),
        questions=kwargs.get("questions"),
        timeout_seconds=timeout,
        priority=Priority(kwargs.get("priority", "normal")),
        agent_id=pipeline_id,
        pipeline_id=pipeline_id,
    )
    response = await _service.wait_for_choice(rid, timeout=timeout)
    result: dict[str, Any] = {"status": "completed", "response_type": response.get("response_type")}
    if response.get("selected_option"):
        result["selected_option"] = response["selected_option"]
    if response.get("answers"):
        result["answers"] = response["answers"]
    if response.get("feedback"):
        result["feedback"] = response["feedback"]
    return result


async def _do_conversation(
    kwargs: dict[str, Any], session_id: str, pipeline_id: str, timeout: int
) -> dict[str, Any]:
    """对话模式——创建请求 + 等待用户到达对话页。"""
    rid = await _service.create_conversation_request(
        session_id=session_id,
        thread_id=session_id,
        tab_id=pipeline_id,
        title=kwargs.get("title", ""),
        description=kwargs.get("description", ""),
        initial_message=kwargs.get("initial_message"),
        suggestions=kwargs.get("suggestions"),
        agent_id=pipeline_id,
        pipeline_id=pipeline_id,
    )
    response = await _service.wait_for_choice(rid, timeout=timeout)
    resp_type = response.get("response_type", "")
    if resp_type == ResponseType.APPROVED.value:
        return {
            "status": "user_arrived",
            "conversation_mode": True,
            "message": "用户已进入对话标签页，管道自动挂起等待新消息。",
        }
    result: dict[str, Any] = {"status": "completed", "response_type": resp_type}
    if response.get("feedback"):
        result["feedback"] = response["feedback"]
    return result


# ════════════════════════════════════════════════════════════════════
# 角色 2：交互服务工具（interaction.*）
# 其他插件经 capability 调这些工具，或前端响应回路经内核转发到此。
# ════════════════════════════════════════════════════════════════════

@plugin.tool(
    name="interaction.send_notification",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "thread_id": {"type": "string"},
            "title": {"type": "string"},
            "message": {"type": "string", "default": ""},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
        },
        "required": ["session_id", "thread_id", "title"],
    },
    description="发送非阻塞通知（服务能力，供其他插件经 capability 调用）",
)
async def interaction_send_notification(
    session_id: str, thread_id: str, title: str, message: str = "", priority: str = "normal",
) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    rid = await _service.send_notification(
        session_id=session_id, thread_id=thread_id, title=title,
        message=message, priority=Priority(priority),
    )
    return {"request_id": rid, "status": "sent"}


@plugin.tool(
    name="interaction.create_choice",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"}, "thread_id": {"type": "string"},
            "tab_id": {"type": "string"}, "title": {"type": "string"},
            "description": {"type": "string", "default": ""},
            "options": {"type": "array"},
            "questions": {"type": "array", "items": {"type": "string"}},
            "timeout_seconds": {"type": "integer", "default": 86400},
            "priority": {"type": "string", "enum": ["low", "normal", "high", "critical"], "default": "normal"},
        },
        "required": ["session_id", "thread_id", "tab_id", "title"],
    },
    description="创建选择模式交互请求（服务能力）",
)
async def interaction_create_choice(
    session_id: str, thread_id: str, tab_id: str, title: str, description: str = "",
    options: list[dict[str, Any]] | None = None, questions: list[str] | None = None,
    timeout_seconds: int = 86400, priority: str = "normal",
) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    rid = await _service.create_choice_request(
        session_id=session_id, thread_id=thread_id, tab_id=tab_id, title=title,
        description=description, options=options, questions=questions,
        timeout_seconds=timeout_seconds, priority=Priority(priority),
    )
    return {"request_id": rid, "status": "pending"}


@plugin.tool(
    name="interaction.wait_for_choice",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "timeout": {"type": "number", "default": 86400},
        },
        "required": ["request_id"],
    },
    description="等待用户选择响应（服务能力，阻塞直到用户操作或超时）",
)
async def interaction_wait_for_choice(request_id: str, timeout: float = 86400) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    try:
        return await _service.wait_for_choice(request_id, timeout)
    except (InteractionTimeoutError, InteractionCancelledError, InteractionDeniedError) as e:
        return {"error": str(e), "request_id": request_id}


@plugin.tool(
    name="interaction.respond",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "response_type": {"type": "string", "description": "approved/denied/answered/timeout/cancelled"},
            "selected_option": {"type": "string"},
            "answers": {"type": "array", "items": {"type": "string"}},
            "feedback": {"type": "string"},
        },
        "required": ["request_id", "response_type"],
    },
    description="提交交互响应（前端用户操作经内核转发到此，唤醒 wait_for_choice）",
)
async def interaction_respond(
    request_id: str, response_type: str, selected_option: str | None = None,
    answers: list[str] | None = None, feedback: str | None = None,
) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    success = await _service.submit_response(
        request_id=request_id, response_type=response_type,
        selected_option=selected_option, answers=answers, feedback=feedback,
    )
    return {"ok": success, "request_id": request_id, "status": "submitted" if success else "not_found"}


@plugin.tool(
    name="interaction.cancel",
    schema={
        "type": "object",
        "properties": {"request_id": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["request_id"],
    },
    description="取消交互请求（服务能力）",
)
async def interaction_cancel(request_id: str, reason: str | None = None) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    success = await _service.cancel_request(request_id, reason)
    return {"ok": success, "request_id": request_id, "status": "cancelled" if success else "not_found"}


@plugin.tool(
    name="interaction.get_pending",
    schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "limit": {"type": "integer", "default": 50},
        },
    },
    description="获取待处理请求列表（服务能力）",
)
async def interaction_get_pending(session_id: str | None = None, limit: int = 50) -> dict[str, Any]:
    if _service is None:
        return {"error": "service not initialized"}
    requests = await _service.get_pending_requests(session_id=session_id, limit=limit)
    return {"requests": requests, "count": len(requests)}


if __name__ == "__main__":
    plugin.run()
