"""浏览器工具（browser）——通过 MCP Bridge 网关操作宿主机 Playwright 浏览器。

六件工具名/参沿用 @playwright/mcp 真实契约（Bridge 上游）：
browser_navigate / browser_snapshot / browser_click / browser_type /
browser_take_screenshot / browser_console_messages。

双路径自动（isolation_policy 驱动）：
- 非隔离会话：本 sidecar（宿主）直发 Bridge。
- 隔离任务：isolation_guard 注入 _container_id → 本工具经 docker exec 在
  任务容器内发起 MCP 调用（沙箱内 MCP Client，审计 caller=sandbox）。

工具实现为单 BuiltinTool 多 action 形态（web_operate 同款），server.py 按
工具名分发。结果归一（含截图→通用多模态通道）统一走 SDK
``normalize_mcp_result``：落盘任务 workspace 返回 file_path（前端 image 卡），
并构建 metadata.multimodal_content 回传视觉模型。
"""

from __future__ import annotations

import logging
from typing import Any

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolSource,
    create_failure_result,
)
from agentos_plugin_sdk.bridge_client import BridgeClient, BridgeClientError
from agentos_plugin_sdk.multimodal import normalize_mcp_result

logger = logging.getLogger(__name__)

# 声明的工具名（与 plugin.json / bridge.yaml 白名单一致）。
BROWSER_TOOLS = (
    "browser_navigate",
    "browser_snapshot",
    "browser_click",
    "browser_type",
    "browser_take_screenshot",
    "browser_console_messages",
)

class BrowserTool(BuiltinTool):
    """浏览器操作工具（MCP Bridge 客户端）。"""

    def __init__(self, bridge_config: dict[str, Any] | None = None):
        self._bridge_config = bridge_config or {}
        self._upstream = str(self._bridge_config.get("upstream", "browser"))

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="browser",
            description="浏览器操作",
            input_schema={"type": "object", "properties": {}},
            source=ToolSource.CODE,
            category=ToolCategory.WEB,
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        tool_name = str(inputs.pop("_tool_name", ""))
        if tool_name not in BROWSER_TOOLS:
            return create_failure_result(error=f"未知的浏览器工具: {tool_name}", error_code="UNKNOWN_BROWSER_TOOL")

        session_id = str(inputs.pop("session_id", "") or "")
        container_id = str(inputs.pop("_container_id", "") or "")
        caller = "sandbox" if container_id else "host"
        # exec_backend 与 _container_id 同级的服务端注入（isolation_guard）：
        # 仅容器路径且原生环境（wsl_native 等）非 None——SDK 据此选沙箱内
        # 传输通道（wsl 传输 / docker exec）。无 _container_id 时不取。
        raw_exec_backend = inputs.pop("_exec_backend", None) if container_id else None
        exec_backend = raw_exec_backend if isinstance(raw_exec_backend, dict) else None
        # 截图落盘目录：param_inject 注入的 workspace（任务工作空间，服务端权威）。
        self._workspace = inputs.pop("workspace", "") or inputs.pop("working_dir", "") or ""

        try:
            # 容器身份显式传参（sidecar 多会话共享进程，不得经 os.environ 传递，
            # 否则并发交错时 A 会话可携 B 容器身份发出）。
            client = BridgeClient(
                self._bridge_config,
                session_id=session_id,
                caller=caller,
                container_id=container_id,
                exec_backend=exec_backend,
            )
        except BridgeClientError as e:
            return create_failure_result(error=str(e), error_code="BRIDGE_TOKEN_MISSING")

        arguments = _build_arguments(tool_name, inputs)
        try:
            result = client.call(tool_name, arguments)
        except BridgeClientError as e:
            return create_failure_result(error=str(e), error_code="BRIDGE_CALL_FAILED")

        extra_data = {"url": arguments["url"]} if tool_name == "browser_navigate" and arguments.get("url") else None
        return normalize_mcp_result(
            result, tool_name=tool_name, workspace=self._workspace, stem="browser", extra_data=extra_data
        )


# ── 参数构造（LLM 输入 → @playwright/mcp 契约参数）─────────────


def _build_arguments(tool_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """把 LLM 入参映射为上游工具参数（透传声明字段，剥内部键）。"""
    passthrough = {
        "browser_navigate": ("url",),
        "browser_snapshot": (),
        "browser_click": ("target", "element", "doubleClick", "button", "modifiers"),
        "browser_type": ("target", "element", "text", "slowly", "submit"),
        "browser_take_screenshot": ("type", "filename", "fullPage", "scale"),
        "browser_console_messages": ("level", "all", "filename"),
    }.get(tool_name, ())
    args = {k: inputs[k] for k in passthrough if k in inputs and inputs[k] is not None}
    if tool_name == "browser_take_screenshot":
        args.setdefault("scale", "css")
        # 统一 png 落盘（image 卡按扩展名渲染）；上游按 type 生成。
        args.setdefault("type", "png")
    return args
