#!/usr/bin/env python3
"""审批系统 MCP 服务端（v0.2 审批闭环版）。

通过 pipeline-executor 能力调用内核 suspend/resume 管道，形成完整的
"创建审批 → 挂起管道 → 前端审阅 → submit → 恢复管道" 闭环。

核心业务逻辑参考 0.1 src/human_interaction/service.py（复制逻辑，不 import src/）。
artifacts/annotations 数据结构参考 src/review/models.py 的 ReviewRequest/ReviewFeedback。

能力依赖（由内核 initialize 注入，缺失时优雅降级为纯内存模拟）：
- pipeline-executor: suspend/resume/start_run 挂起恢复管道
- event-bus: emit 审批创建事件通知前端打开审阅界面
- logger: 结构化日志

[来源: docs/tasks/task_10_system_plugins.md AC-09-2; v0.2 P1-2 审批闭环补全]
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("approval_service")

# 待处理审批请求：approval_id → 请求记录
# 记录字段：
#   id / mode / status / created_at / artifacts / annotations / summary / options / timeout
#   task_id / run_id（所属管道运行 ID）
#   suspend_handle（挂起内核管道返回的句柄，含 run_id/branch_id/seq，resume 时回传）
_pending: dict[str, dict[str, Any]] = {}


# ── 内核能力访问（防御式：能力未注入时优雅降级） ──────────────────────


def _log(level: str, msg: str, **fields: Any) -> None:
    """结构化日志（fire-and-forget，绝不影响主流程）。

    通过 logger 能力异步记录；无事件循环或能力未注入时静默丢弃。
    日志失败绝不能抛出或阻塞——调用方不期望日志有副作用。
    """
    try:
        logger = plugin.get_capability("logger")
    except KeyError:
        return
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环（不应发生于工具调用路径）——直接丢弃
        return

    async def _do_log() -> None:
        try:
            # logger 能力约定 log 方法：{level, message, fields}
            await logger.call("log", {"level": level, "message": msg, "fields": fields})
        except Exception:
            pass

    loop.create_task(_do_log())


async def _suspend_pipeline(run_id: str, approval_id: str) -> dict[str, Any] | None:
    """通过 pipeline-executor 能力挂起当前管道。

    Args:
        run_id: 当前管道运行 ID（由调用方传入）。
        approval_id: 关联的审批 ID，透传给内核便于关联。

    Returns:
        内核返回的 suspend_handle（含 run_id/branch_id/seq），供 resume 使用。
        None 表示能力不可用或调用失败（已降级为只存 _pending）。
    """
    try:
        pipeline = plugin.get_capability("pipeline-executor")
    except KeyError:
        _log(
            "warning",
            "pipeline-executor capability not injected; approval degrades to memory-only",
            approval_id=approval_id,
        )
        return None

    try:
        result = await pipeline.call(
            "suspend",
            {"run_id": run_id, "approval_id": approval_id},
        )
    except Exception as exc:
        _log(
            "warning",
            "pipeline suspend call failed; approval degrades to memory-only",
            approval_id=approval_id,
            error=str(exc),
        )
        return None

    # 内核应返回包含 run_id/branch_id/seq 的句柄；规范化兜底
    if isinstance(result, dict):
        handle = {
            "run_id": result.get("run_id", run_id),
            "branch_id": result.get("branch_id"),
            "seq": result.get("seq"),
        }
        return handle

    # 内核返回了非 dict（罕见），仍用传入的 run_id 构造一个最小句柄
    return {"run_id": run_id, "branch_id": None, "seq": None}


async def _resume_pipeline(handle: dict[str, Any], approval_id: str, result: Any) -> bool:
    """通过 pipeline-executor 能力恢复管道。

    Args:
        handle: _suspend_pipeline 返回的 suspend_handle（含 run_id/branch_id/seq）。
        approval_id: 审批 ID（透传）。
        result: 审批结果（透传给内核供后续节点读取）。

    Returns:
        True 表示恢复成功；False 表示能力不可用或失败（已 log warning）。
    """
    try:
        pipeline = plugin.get_capability("pipeline-executor")
    except KeyError:
        _log(
            "warning",
            "pipeline-executor capability not injected; cannot resume pipeline",
            approval_id=approval_id,
        )
        return False

    params: dict[str, Any] = {
        "approval_id": approval_id,
        "result": result,
    }
    # 仅在句柄字段非 None 时回传，避免覆盖内核默认值
    for key in ("run_id", "branch_id", "seq"):
        val = handle.get(key)
        if val is not None:
            params[key] = val

    try:
        await pipeline.call("resume", params)
        return True
    except Exception as exc:
        _log(
            "warning",
            "pipeline resume call failed",
            approval_id=approval_id,
            error=str(exc),
        )
        return False


async def _emit_approval_created(payload: dict[str, Any]) -> None:
    """通过 event-bus 能力通知前端打开审阅界面。

    事件名 approval.created；能力不可用时静默降级（前端拿不到事件，但审批记录已落库）。
    """
    try:
        event_bus = plugin.get_capability("event-bus")
    except KeyError:
        _log("warning", "event-bus capability not injected; skip notify", **payload)
        return

    try:
        await event_bus.call("emit", {"event": "approval.created", "payload": payload})
    except Exception as exc:
        _log(
            "warning",
            "event-bus emit failed",
            error=str(exc),
            event="approval.created",
        )


# ── 工具：create_review_request（v0.2 新增） ──────────────────────


@plugin.tool(
    name="approval.create_review_request",
    schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "关联任务 ID"},
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": "待审阅制品 ID 列表",
                "default": [],
            },
            "annotations": {
                "type": "array",
                "items": {"type": "object"},
                "description": "结构化批注（可选），每项为批注字典",
                "default": [],
            },
            "summary": {"type": "string", "description": "审批描述/摘要"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "审批选项（可选，如 approve/reject/modify）",
            },
            "run_id": {
                "type": "string",
                "description": "当前管道运行 ID（用于挂起/恢复管道；省略则不挂起）",
            },
            "timeout": {"type": "number", "default": 300},
        },
        "required": ["task_id", "artifacts", "summary"],
    },
    description="Create a document review approval request that pauses the pipeline",
)
async def create_review_request(
    task_id: str,
    artifacts: list[str],
    summary: str,
    annotations: list[dict[str, Any]] | None = None,
    options: list[str] | None = None,
    run_id: str | None = None,
    timeout: float = 300,
) -> dict[str, Any]:
    """创建文档审阅审批请求。

    流程：
    1. 生成 request_id，记录 artifacts/annotations/summary/options 到 _pending
    2. 若提供 run_id，通过 pipeline-executor 挂起管道，存 suspend_handle 供 resume 用
    3. 通过 event-bus 发 approval.created 通知前端打开审阅界面

    Returns:
        request_id + suspend_handle（能力不可用时 suspend_handle 为 None）
    """
    approval_id = f"rev_{uuid.uuid4().hex[:12]}"
    request: dict[str, Any] = {
        "id": approval_id,
        "mode": "review",
        "task_id": task_id,
        "summary": summary,
        "artifacts": artifacts,
        "annotations": annotations or [],
        "options": options or [],
        "status": "pending",
        "timeout": timeout,
        "run_id": run_id,
        "created_at": time.time(),
    }

    # 挂起管道（能力不可用时 degrade，仅存 _pending）
    suspend_handle: dict[str, Any] | None = None
    if run_id:
        suspend_handle = await _suspend_pipeline(run_id, approval_id)
    request["suspend_handle"] = suspend_handle

    _pending[approval_id] = request

    # 通知前端打开审阅界面
    await _emit_approval_created(
        {
            "request_id": approval_id,
            "task_id": task_id,
            "artifacts": artifacts,
            "annotations": request["annotations"],
            "summary": summary,
            "mode": "review",
            "run_id": run_id,
        }
    )

    _log(
        "info",
        "review approval created",
        approval_id=approval_id,
        task_id=task_id,
        artifacts_count=len(artifacts),
        suspended=suspend_handle is not None,
    )

    return {
        "request_id": approval_id,
        "status": "pending",
        "mode": "review",
        "suspend_handle": suspend_handle,
    }


# ── 工具：create_choice / create_conversation（v0.2 接入闭环） ─────


@plugin.tool(
    name="approval.create_choice",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "artifacts": {"type": "array", "items": {"type": "string"}, "default": []},
            "run_id": {
                "type": "string",
                "description": "当前管道运行 ID（用于挂起管道；省略则不挂起）",
            },
            "timeout": {"type": "number", "default": 300},
        },
        "required": ["title", "options"],
    },
    description="Create a choice-mode approval request that pauses the pipeline",
)
async def create_choice(
    title: str,
    options: list[str],
    artifacts: list[str] | None = None,
    run_id: str | None = None,
    timeout: float = 300,
) -> dict[str, Any]:
    """Create a choice approval request.

    Pauses the pipeline (if run_id given) until user selects an option.
    """
    approval_id = f"appr_{uuid.uuid4().hex[:8]}"
    request = {
        "id": approval_id,
        "mode": "choice",
        "title": title,
        "options": options,
        "artifacts": artifacts or [],
        "status": "pending",
        "timeout": timeout,
        "run_id": run_id,
        "created_at": time.time(),
    }

    suspend_handle: dict[str, Any] | None = None
    if run_id:
        suspend_handle = await _suspend_pipeline(run_id, approval_id)
    request["suspend_handle"] = suspend_handle

    _pending[approval_id] = request

    await _emit_approval_created(
        {
            "request_id": approval_id,
            "title": title,
            "options": options,
            "artifacts": request["artifacts"],
            "mode": "choice",
            "run_id": run_id,
        }
    )

    return {"approval_id": approval_id, "status": "pending", "mode": "choice", "suspend_handle": suspend_handle}


@plugin.tool(
    name="approval.create_conversation",
    schema={
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "artifacts": {"type": "array", "items": {"type": "string"}, "default": []},
            "run_id": {
                "type": "string",
                "description": "当前管道运行 ID（用于挂起管道；省略则不挂起）",
            },
            "timeout": {"type": "number", "default": 300},
        },
        "required": ["message"],
    },
    description="Create a conversation-mode approval request",
)
async def create_conversation(
    message: str,
    artifacts: list[str] | None = None,
    run_id: str | None = None,
    timeout: float = 300,
) -> dict[str, Any]:
    """Create a conversation approval request for free-text input."""
    approval_id = f"appr_{uuid.uuid4().hex[:8]}"
    request = {
        "id": approval_id,
        "mode": "conversation",
        "message": message,
        "artifacts": artifacts or [],
        "status": "pending",
        "timeout": timeout,
        "run_id": run_id,
        "created_at": time.time(),
    }

    suspend_handle: dict[str, Any] | None = None
    if run_id:
        suspend_handle = await _suspend_pipeline(run_id, approval_id)
    request["suspend_handle"] = suspend_handle

    _pending[approval_id] = request

    await _emit_approval_created(
        {
            "request_id": approval_id,
            "message": message,
            "artifacts": request["artifacts"],
            "mode": "conversation",
            "run_id": run_id,
        }
    )

    return {"approval_id": approval_id, "status": "pending", "mode": "conversation", "suspend_handle": suspend_handle}


# ── 工具：submit（v0.2 真打通 resume） ──────────────────────────


@plugin.tool(
    name="approval.submit",
    schema={
        "type": "object",
        "properties": {
            "approval_id": {"type": "string"},
            "result": {
                "type": "string",
                "description": "审批结果文本（selected option / feedback / approved 等）",
            },
            "annotations": {
                "type": "array",
                "items": {"type": "object"},
                "description": "用户提交的结构化批注（review 模式可选）",
            },
        },
        "required": ["approval_id", "result"],
    },
    description="Submit approval result to resume the pipeline",
)
async def submit(
    approval_id: str,
    result: str,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Submit approval result, persist feedback, and resume the suspended pipeline.

    取消 DEBT 注释：现在真正调用 pipeline-executor.resume 恢复管道。
    """
    request = _pending.get(approval_id)
    if request is None:
        return {"error": "approval not found", "approval_id": approval_id}

    request["status"] = "resolved"
    request["result"] = result
    if annotations is not None:
        request["submitted_annotations"] = annotations
    request["resolved_at"] = time.time()

    # 取消 v0.1 的 DEBT 注释——真正打通 resume 闭环。
    # 拿到创建时存的 suspend_handle，调 pipeline-executor.resume 恢复管道。
    resumed = False
    suspend_handle = request.get("suspend_handle")
    if suspend_handle is not None:
        resumed = await _resume_pipeline(suspend_handle, approval_id, result)

    _log(
        "info",
        "approval resolved",
        approval_id=approval_id,
        resumed=resumed,
    )

    return {
        "approval_id": approval_id,
        "status": "resolved",
        "result": result,
        "resumed": resumed,
    }


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize approval service on load."""
    _log("info", "approval_service loaded")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup pending approvals on unload."""
    _log("info", "approval_service unloaded", pending=len(_pending))


if __name__ == "__main__":
    plugin.run()
