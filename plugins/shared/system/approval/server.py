#!/usr/bin/env python3
"""审批系统 MCP 服务端（v0.2 审批闭环版）。

职责收窄为"管道挂起/恢复 + 审批语义"——交互请求的创建、前端通知、
用户响应等待统一委托给 human-interaction capability（交互工具插件）。
approval 不再自建 request 结构、不再自己发 event-bus。

能力依赖（由内核 initialize 注入）：
- human-interaction: create_choice/create_conversation/wait_for_choice——交互请求全权委托
- pipeline-executor: suspend/resume——挂起/恢复管道（approval 独有职责）
- logger: 结构化日志

闭环：
    创建审批 → human-interaction.create_choice（弹窗）+ pipeline-executor.suspend（挂起）
    → 用户选择 → human-interaction.wait_for_choice 返回
    → pipeline-executor.resume（恢复管道）

[来源: docs/tasks/task_10_system_plugins.md AC-09-2; v0.2 审批闭环]
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("approval_service")

# 审批挂起句柄：approval_id → {suspend_handle, run_id, created_at}
# 仅存恢复管道所需的最小信息（交互状态由 human-interaction 插件管）。
_suspended: dict[str, dict[str, Any]] = {}


def _get_cap(name: str) -> Any | None:
    """获取 capability handle，未注入返回 None。"""
    try:
        return plugin.get_capability(name)
    except KeyError:
        return None


async def _suspend_pipeline(run_id: str, approval_id: str) -> dict[str, Any] | None:
    """通过 pipeline-executor 挂起管道，返回 suspend_handle。"""
    pipeline = _get_cap("pipeline-executor")
    if pipeline is None:
        logger.warning("[approval] pipeline-executor not injected; skip suspend | id=%s", approval_id)
        return None
    try:
        result = await pipeline.call("suspend", {"run_id": run_id, "approval_id": approval_id})
    except Exception as exc:
        logger.warning("[approval] suspend failed | id=%s | err=%s", approval_id, exc)
        return None
    if isinstance(result, dict):
        return {
            "run_id": result.get("run_id", run_id),
            "branch_id": result.get("branch_id"),
            "seq": result.get("seq"),
        }
    return {"run_id": run_id, "branch_id": None, "seq": None}


async def _resume_pipeline(handle: dict[str, Any], approval_id: str, result: Any) -> bool:
    """通过 pipeline-executor 恢复管道。"""
    pipeline = _get_cap("pipeline-executor")
    if pipeline is None:
        logger.warning("[approval] pipeline-executor not injected; cannot resume | id=%s", approval_id)
        return False
    params: dict[str, Any] = {"approval_id": approval_id, "result": result}
    for key in ("run_id", "branch_id", "seq"):
        val = handle.get(key)
        if val is not None:
            params[key] = val
    try:
        await pipeline.call("resume", params)
        return True
    except Exception as exc:
        logger.warning("[approval] resume failed | id=%s | err=%s", approval_id, exc)
        return False


def _build_options(label_list: list[str]) -> list[dict[str, str]]:
    """把字符串选项列表转成 human-interaction 的 [{id, label}] 格式。"""
    return [{"id": str(i), "label": label} for i, label in enumerate(label_list)]


# ── 工具：create_choice（转调 human-interaction） ──────────────────


@plugin.tool(
    name="approval.create_choice",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}},
            "run_id": {"type": "string", "description": "管道运行 ID（用于挂起管道）"},
            "timeout": {"type": "number", "default": 300},
        },
        "required": ["title", "options"],
    },
    description="Create a choice-mode approval（委托 human-interaction 弹窗 + 挂起管道）",
)
async def create_choice(
    title: str,
    options: list[str],
    run_id: str | None = None,
    timeout: float = 300,
) -> dict[str, Any]:
    """创建选择审批——交互委托 human-interaction，approval 只管管道挂起/恢复。"""
    hi = _get_cap("human-interaction")
    if hi is None:
        return {"error": "human-interaction capability not injected"}

    # 第一步：经 human-interaction 创建交互请求
    session_id = run_id or "approval"
    create_res = await hi.call("create_choice", {
        "session_id": session_id,
        "thread_id": session_id,
        "tab_id": session_id,
        "title": title,
        "description": "",
        "options": _build_options(options),
        "timeout_seconds": int(timeout),
    })
    if not isinstance(create_res, dict) or create_res.get("error"):
        return {"error": f"create_choice failed: {create_res}"}
    request_id = create_res.get("request_id", "")

    # 第二步：挂起管道（approval 独有职责）
    suspend_handle = None
    if run_id:
        suspend_handle = await _suspend_pipeline(run_id, request_id)
        _suspended[request_id] = {
            "suspend_handle": suspend_handle,
            "run_id": run_id,
            "created_at": time.time(),
        }

    # 第三步：等待用户选择（阻塞，由 human-interaction 管 Event + 前端回路）
    wait_res = await hi.call("wait_for_choice", {"request_id": request_id, "timeout": timeout})
    selected = wait_res.get("selected_option", "") if isinstance(wait_res, dict) else ""

    # 第四步：恢复管道
    resumed = False
    if suspend_handle is not None:
        resumed = await _resume_pipeline(suspend_handle, request_id, selected)

    _suspended.pop(request_id, None)
    return {
        "request_id": request_id,
        "status": "resolved",
        "selected_option": selected,
        "resumed": resumed,
    }


# ── 工具：submit（恢复挂起的管道） ──────────────────────────────


@plugin.tool(
    name="approval.submit",
    schema={
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "result": {"type": "string"},
        },
        "required": ["request_id", "result"],
    },
    description="Submit approval result and resume the suspended pipeline",
)
async def submit(request_id: str, result: str) -> dict[str, Any]:
    """提交审批结果，恢复挂起的管道。

    注：create_choice 已内置 wait_for_choice + resume，多数场景不需要单独调 submit。
    本工具保留给"前端直接提交恢复"的路径（如 review 模式异步审批）。
    """
    record = _suspended.get(request_id)
    if record is None:
        return {"error": "no suspended approval for this request_id", "request_id": request_id}

    handle = record.get("suspend_handle")
    resumed = False
    if handle is not None:
        resumed = await _resume_pipeline(handle, request_id, result)

    _suspended.pop(request_id, None)
    logger.info("[approval] submitted | id=%s | resumed=%s", request_id, resumed)
    return {"request_id": request_id, "status": "resolved", "result": result, "resumed": resumed}


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    logger.info("approval_service loaded（委托 human-interaction 处理交互）")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    logger.info("approval_service unloaded | suspended=%d", len(_suspended))


if __name__ == "__main__":
    plugin.run()
