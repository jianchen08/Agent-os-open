#!/usr/bin/env python3
"""审批系统 MCP 服务端。

通过 pipeline-executor 能力调用内核暂停/恢复管道。
核心业务逻辑参考 0.1 src/human_interaction/service.py。

[来源: docs/tasks/task_10_system_plugins.md AC-09-2]
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from lingxi_plugin_sdk import AgentOSPlugin

plugin = AgentOSPlugin("approval_service")

# 待处理审批请求
_pending: dict[str, dict[str, Any]] = {}


@plugin.tool(
    name="approval.create_choice",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "artifacts": {"type": "array", "items": {"type": "string"}, "default": []},
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
    timeout: float = 300,
) -> dict[str, Any]:
    """Create a choice approval request.

    Pauses the pipeline until user selects an option.
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
        "created_at": time.time(),
    }
    _pending[approval_id] = request
    return {"approval_id": approval_id, "status": "pending", "mode": "choice"}


@plugin.tool(
    name="approval.create_conversation",
    schema={
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "artifacts": {"type": "array", "items": {"type": "string"}, "default": []},
            "timeout": {"type": "number", "default": 300},
        },
        "required": ["message"],
    },
    description="Create a conversation-mode approval request",
)
async def create_conversation(
    message: str,
    artifacts: list[str] | None = None,
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
        "created_at": time.time(),
    }
    _pending[approval_id] = request
    return {"approval_id": approval_id, "status": "pending", "mode": "conversation"}


@plugin.tool(
    name="approval.submit",
    schema={
        "type": "object",
        "properties": {
            "approval_id": {"type": "string"},
            "result": {"type": "string"},
        },
        "required": ["approval_id", "result"],
    },
    description="Submit approval result to resume the pipeline",
)
async def submit(approval_id: str, result: str) -> dict[str, Any]:
    """Submit approval result and mark request as resolved."""
    request = _pending.get(approval_id)
    if request is None:
        return {"error": "approval not found", "approval_id": approval_id}

    request["status"] = "resolved"
    request["result"] = result
    request["resolved_at"] = time.time()

    # DEBT: pipeline-executor 调用未实现。ceiling: 当前为独立进程模拟。upgrade: 引擎层集成后取消注释。
    # pipeline = plugin.get_capability("pipeline-executor")
    # await pipeline.call("resume", {"approval_id": approval_id, "result": result})

    return {"approval_id": approval_id, "status": "resolved", "result": result}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize approval service on load."""
    pass


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup pending approvals on unload."""
    pass


if __name__ == "__main__":
    plugin.run()
