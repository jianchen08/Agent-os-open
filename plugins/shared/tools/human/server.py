#!/usr/bin/env python3
"""Human Interaction 工具 MCP 服务端——接口适配层。

M5 改造：优先通过 human-interaction capability 反向调用 human_interaction_service
sidecar（经内核 McpBridge 路由），把"用户交互状态"留在 service sidecar 唯一一份。
capability 未注入时降级到 in-process tool.py（0.1 兼容，主进程单例模式）。

链路（M5 闭环）：
    LLM 调 human_interaction 工具
      → 本 handler 调 plugin.get_capability("human-interaction")
      → KernelChannel 发 JSON-RPC 到内核
      → reader loop 识别（M2 动态白名单）
      → KernelCapabilityRouter 查 handler_registry（M5-3）
      → ProvidedCapabilityHandler → McpBridge（M5-2）
      → invoker.invoke_tool("human_interaction_service", "interaction.<method>")
      → human_interaction_service sidecar 执行
      → EventBusNotifier 经 event-bus.emit 推前端（M5-4）
      → 返回结果回传
"""
from __future__ import annotations
import os
import sys
import logging
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("human_interaction_tool")

# human-interaction capability 的 method 清单（与 service sidecar 的工具名对应，
# 去掉 interaction. 前缀）。用于 capability 调用前的可用性检查。
_CAP_METHODS = {
    "choice": ("create_choice", "wait_for_choice"),
    "conversation": ("create_conversation", "wait_for_choice"),
    "notification": ("send_notification",),
}


def _get_human_interaction_cap():
    """获取 human-interaction capability handle，未注入返回 None。"""
    try:
        return plugin.get_capability("human-interaction")
    except (KeyError, AttributeError):
        return None


@plugin.tool(
    name="human_interaction",
    schema={"type": "object", "properties": {"mode": {"type": "string", "enum": ["choice", "conversation", "notification"]}, "title": {"type": "string"}, "description": {"type": "string"}, "options": {"type": "array"}, "questions": {"type": "array"}, "initial_message": {"type": "string"}, "file_paths": {"type": "array"}, "timeout_seconds": {"type": "number", "default": 86400}, "priority": {"type": "string", "default": "normal"}}, "required": ["mode", "title"]},
    description="与用户交互",
)
async def human_interaction(**kwargs):
    """人类交互工具入口。

    优先走 human-interaction capability（M5 sidecar 架构）；
    capability 未注入时降级到 in-process tool.py（0.1 兼容）。
    """
    cap = _get_human_interaction_cap()
    if cap is not None:
        return await _via_capability(cap, kwargs)
    logger.info("[human_interaction] capability not injected; fallback to in-process tool")
    return await _via_inprocess_tool(kwargs)


async def _via_capability(cap: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """通过 human-interaction capability 调用 service sidecar。

    把工具参数映射成 service sidecar 的 method + params。
    sidecar 内部负责创建 request、推前端（EventBusNotifier）、等待响应。
    """
    mode = kwargs.get("mode", "choice")
    timeout = kwargs.get("timeout_seconds", 86400)

    try:
        if mode == "notification":
            # 通知模式：调 send_notification，非阻塞
            result = await cap.call("send_notification", {
                "session_id": kwargs.get("session_id") or kwargs.get("pipeline_id", ""),
                "thread_id": kwargs.get("session_id") or kwargs.get("pipeline_id", ""),
                "title": kwargs.get("title", ""),
                "message": kwargs.get("description") or kwargs.get("initial_message") or "",
                "priority": kwargs.get("priority", "normal"),
            })
            return result if isinstance(result, dict) else {"status": "sent", "raw": result}

        # choice / conversation 模式：两步——创建请求 + 等待响应
        create_method = "create_choice" if mode == "choice" else "create_conversation"
        common_params: dict[str, Any] = {
            "session_id": kwargs.get("session_id") or kwargs.get("pipeline_id", ""),
            "thread_id": kwargs.get("session_id") or kwargs.get("pipeline_id", ""),
            "tab_id": kwargs.get("pipeline_id", ""),
            "title": kwargs.get("title", ""),
            "description": kwargs.get("description", ""),
            "file_paths": kwargs.get("file_paths"),
        }
        if mode == "choice":
            common_params["options"] = kwargs.get("options")
            common_params["questions"] = kwargs.get("questions")
            common_params["timeout_seconds"] = timeout
            common_params["priority"] = kwargs.get("priority", "normal")
        else:  # conversation
            common_params["initial_message"] = kwargs.get("initial_message")
            common_params["suggestions"] = kwargs.get("suggestions")

        # 第一步：创建请求
        create_result = await cap.call(create_method, common_params)
        if not isinstance(create_result, dict):
            return {"error": f"create returned non-dict: {create_result}"}
        if create_result.get("error"):
            return create_result

        request_id = create_result.get("request_id")
        if not request_id:
            return {"error": f"create did not return request_id: {create_result}"}

        # 第二步：等待用户响应（阻塞直到用户操作或超时）
        wait_result = await cap.call("wait_for_choice", {
            "request_id": request_id,
            "timeout": timeout,
        })
        return wait_result if isinstance(wait_result, dict) else {"raw": wait_result}

    except Exception as exc:
        logger.exception("[human_interaction] capability call failed")
        return {"error": f"人类交互执行失败: {exc}"}


async def _via_inprocess_tool(kwargs: dict[str, Any]) -> dict[str, Any]:
    """降级路径：in-process tool.py（0.1 兼容，主进程单例模式）。"""
    from tool import HumanInteractionTool  # noqa: PLC0415
    t = HumanInteractionTool()
    result = await t.execute(kwargs)
    return result.output if result.success else {"error": result.error}


if __name__ == "__main__":
    plugin.run()
