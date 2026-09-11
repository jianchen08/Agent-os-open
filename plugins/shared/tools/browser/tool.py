"""浏览器工具（browser）——通过 MCP Bridge 网关操作宿主机 Playwright 浏览器。

六件工具名/参沿用 @playwright/mcp 真实契约（Bridge 上游）：
browser_navigate / browser_snapshot / browser_click / browser_type /
browser_take_screenshot / browser_console_messages。

双路径自动（isolation_policy 驱动）：
- 非隔离会话：本 sidecar（宿主）直发 Bridge。
- 隔离任务：isolation_guard 注入 _container_id → 本工具经 docker exec 在
  任务容器内发起 MCP 调用（沙箱内 MCP Client，审计 caller=sandbox）。

工具实现为单 BuiltinTool 多 action 形态（web_operate 同款），server.py 按
工具名分发。截图/快照等文件产物落盘任务 workspace 返回 file_path。
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from agentos_plugin_sdk import (
    BuiltinTool,
    Tool,
    ToolCategory,
    ToolExecutionResult,
    ToolResult,
    ToolSource,
    create_failure_result,
    create_success_result,
)

from bridge_client import BridgeClient, BridgeClientError

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

# 截图文件大小上限（base64 解码后）：超限拒收（防上游返回巨图撑爆上下文）。
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class BrowserTool(BuiltinTool):
    """浏览器操作工具（MCP Bridge 客户端）。"""

    # 最近一次调用的 workspace（param_inject 注入；截图落盘目录）。
    _last_workspace: str = ""

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
        # 容器路径下 _container_id 经环境变量传给 bridge_client（不进 MCP 参数）。
        if container_id:
            os.environ["_CONTAINER_ID"] = container_id
        # 截图落盘目录：param_inject 注入的 workspace（任务工作空间，服务端权威）。
        self._workspace = inputs.pop("workspace", "") or inputs.pop("working_dir", "") or ""
        BrowserTool._last_workspace = self._workspace

        try:
            client = BridgeClient(self._bridge_config, session_id=session_id, caller=caller)
        except BridgeClientError as e:
            return create_failure_result(error=str(e), error_code="BRIDGE_TOKEN_MISSING")

        arguments = _build_arguments(tool_name, inputs)
        try:
            result = client.call(tool_name, arguments)
        except BridgeClientError as e:
            return create_failure_result(error=str(e), error_code="BRIDGE_CALL_FAILED")

        return _to_tool_result(tool_name, result, arguments)


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


# ── 结果归一（MCP content → ToolResult）──────────────────────


def _to_tool_result(tool_name: str, mcp_result: dict, arguments: dict[str, Any] | None = None) -> ToolExecutionResult:
    """MCP content 数组 → 工具出参。

    - text content → snapshot_text / text
    - image content（base64）→ 落盘 workspace，返回 file_path（image 卡渲染）
    - isError → 失败结果（content 里的错误文本透出）
    """
    if mcp_result.get("isError"):
        err_text = _content_text(mcp_result) or "上游工具执行失败"
        return create_failure_result(error=err_text, error_code="UPSTREAM_TOOL_ERROR")

    content = mcp_result.get("content") or []
    texts: list[str] = []
    image_b64 = ""
    image_mime = ""
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image":
            image_b64 = str(item.get("data") or "")
            image_mime = str(item.get("mimeType") or "image/png")
        elif item.get("type") == "text":
            texts.append(str(item.get("text") or ""))
        elif item.get("type") == "resource":
            texts.append(str((item.get("resource") or {}).get("text") or ""))

    out: dict[str, Any] = {"status": 200, "tool": tool_name}
    if texts:
        out["snapshot_text"] = "\n".join(texts)

    if image_b64:
        try:
            raw = base64.b64decode(image_b64, validate=False)
        except (ValueError, TypeError) as e:
            return create_failure_result(error=f"截图 base64 解码失败: {e}", error_code="IMAGE_DECODE_FAILED")
        if len(raw) > MAX_IMAGE_BYTES:
            return create_failure_result(
                error=f"截图超限: {len(raw)} > {MAX_IMAGE_BYTES} bytes", error_code="IMAGE_TOO_LARGE"
            )
        ext = "png" if "png" in image_mime else ("jpg" if "jpeg" in image_mime else "png")
        path = _save_workspace_file(f"browser-{int(time.time() * 1000)}.{ext}", raw)
        out["file_path"] = path
        out["image_mime"] = image_mime

    if tool_name == "browser_navigate":
        out["url"] = str((arguments or {}).get("url") or "")
    return create_success_result(out)


def _content_text(mcp_result: dict) -> str:
    parts = []
    for item in mcp_result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(parts).strip()


def _workspace_root() -> str:
    """任务 workspace：param_inject 注入的 workspace（execute 期存到实例），回退 cwd。"""
    ws = getattr(BrowserTool, "_last_workspace", "") or ""
    if ws:
        return ws
    return os.getcwd()


def _save_workspace_file(filename: str, data: bytes) -> str:
    root = _workspace_root()
    path = os.path.join(root, filename)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path