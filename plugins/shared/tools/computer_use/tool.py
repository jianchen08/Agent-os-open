"""桌面电脑操作工具（computer_use）——经 MCP Bridge 网关操作宿主机 Windows 桌面。

九件工具映射 CursorTouch/Windows-MCP 上游（uvx windows-mcp serve，单
BuiltinTool 多 action，browser 同款；元素树/坐标语义由上游 UIA 定义）：
computer_snapshot→Snapshot / computer_take_screenshot→Screenshot /
computer_click→Click / computer_type→Type / computer_scroll→Scroll /
computer_move→Move / computer_key→Shortcut / computer_wait→Wait /
computer_app→App。

使用契约（操作循环：先看屏→操作→再看屏验证；label 优先于坐标）写在各工具
description——工具面自描述，不依赖外部技能文档。

desktop 属宿主机：隔离任务（_container_id 注入）直接拒绝（ISOLATED_HOST_ONLY），
不做 browser 那样的容器内转发——沙箱容器里没有宿主桌面。上游的 shell/
filesystem 等工具不在 Bridge 白名单（本仓有自有 bash/文件工具与安全治理，
不放行旁路）。

截图类结果回传模型：结果归一统一走 SDK 通用多模态通道
``normalize_mcp_result``（MCP 标准 image content → 落盘 workspace 的
file_path/render:image 卡 + metadata.multimodal_content 注入 vision 消息），
本插件不再自带转换实现。
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

# 声明的工具名（与 plugin.json / bridge.yaml 白名单一致）
COMPUTER_TOOLS = (
    "computer_snapshot",
    "computer_take_screenshot",
    "computer_click",
    "computer_type",
    "computer_scroll",
    "computer_move",
    "computer_key",
    "computer_wait",
    "computer_app",
)

# AgentOS 工具名 → 上游 Windows-MCP 工具名
_UPSTREAM_TOOLS = {
    "computer_snapshot": "Snapshot",
    "computer_take_screenshot": "Screenshot",
    "computer_click": "Click",
    "computer_type": "Type",
    "computer_scroll": "Scroll",
    "computer_move": "Move",
    "computer_key": "Shortcut",
    "computer_wait": "Wait",
    "computer_app": "App",
}

# 透传上游的参数（与 plugin.json input_schema 一致；None 值丢弃）
_PASSTHROUGH: dict[str, tuple[str, ...]] = {
    "computer_snapshot": ("use_vision", "use_dom", "use_annotation", "use_ui_tree", "display", "region"),
    "computer_take_screenshot": ("use_annotation", "display", "region"),
    "computer_click": ("loc", "label", "button", "clicks"),
    "computer_type": ("text", "loc", "label", "clear", "caret_position", "press_enter"),
    "computer_scroll": ("loc", "label", "type", "direction", "wheel_times"),
    "computer_move": ("loc", "label", "drag", "from_loc", "duration"),
    "computer_key": ("shortcut",),
    "computer_wait": ("duration",),
    "computer_app": ("mode", "name", "window_loc", "window_size", "executable", "args", "cwd"),
}

# 上游名（bridge.yaml 的 upstreams 键）
BRIDGE_UPSTREAM = "computer"


class ComputerUseTool(BuiltinTool):
    """宿主机桌面操作工具（MCP Bridge 客户端，仅 host 路径）。"""

    def __init__(self) -> None:
        self._workspace = ""

    @staticmethod
    def get_tool_definition() -> Tool:
        return Tool(
            name="computer_use",
            description="宿主机桌面操作",
            input_schema={"type": "object", "properties": {}},
            source=ToolSource.CODE,
            category=ToolCategory.SYSTEM,
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        tool_name = str(inputs.pop("_tool_name", ""))
        if tool_name not in COMPUTER_TOOLS:
            return create_failure_result(error=f"未知的桌面操作工具: {tool_name}", error_code="UNKNOWN_COMPUTER_TOOL")
        if str(inputs.pop("_container_id", "") or ""):
            return create_failure_result(
                error="computer_use 只能操作宿主机桌面：隔离任务容器内没有宿主桌面，请改用非隔离会话",
                error_code="ISOLATED_HOST_ONLY",
            )
        # 截图落盘目录：param_inject 注入的 workspace（任务工作空间，服务端权威）
        self._workspace = str(inputs.pop("workspace", "") or inputs.pop("working_dir", "") or "")
        session_id = str(inputs.pop("session_id", "") or "")

        arguments = _build_arguments(tool_name, inputs)
        try:
            client = BridgeClient({"upstream": BRIDGE_UPSTREAM}, session_id=session_id, caller="host")
        except BridgeClientError as e:
            return create_failure_result(error=str(e), error_code="BRIDGE_TOKEN_MISSING")
        try:
            result = client.call(_UPSTREAM_TOOLS[tool_name], arguments)
        except BridgeClientError as e:
            return create_failure_result(error=str(e), error_code="BRIDGE_CALL_FAILED")

        return normalize_mcp_result(result, tool_name=tool_name, workspace=self._workspace, stem="computer")


def _build_arguments(tool_name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """LLM 入参 → 上游工具参数（透传声明字段，None 值丢弃，内部键已剥）。"""
    passthrough = _PASSTHROUGH.get(tool_name, ())
    return {k: inputs[k] for k in passthrough if k in inputs and inputs[k] is not None}
