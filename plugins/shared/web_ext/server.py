#!/usr/bin/env python3
"""Web Operate 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations

import os
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from _libs.tool_server_base import load_tool_class, tool_result_envelope  # noqa: E402

plugin = AgentOSPlugin("web_operate_tool")

_web_tool_cls: Any = None


def _load_web_tool() -> Any:
    """显式按文件路径加载本目录 tool.py 的 WebTool（合宿防遮蔽：裸名 ``tool``
    槽位被多成员争用，摘槽/还槽语义单源在 _libs helper）。"""
    global _web_tool_cls
    if _web_tool_cls is not None:
        return _web_tool_cls
    _web_tool_cls = load_tool_class(
        os.path.dirname(__file__), "web_operate_tool_impl", "WebTool"
    )
    return _web_tool_cls


@plugin.tool(
    name="web_operate",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["get", "post", "fetch"],
                "default": "get",
                "description": "操作类型：get（HTTP GET 请求）、post（HTTP POST 请求）、fetch（抓取网页并提取内容）",
            },
            "url": {
                "type": "string",
                "description": "目标 URL，支持 http 和 https 协议",
            },
            "headers": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "请求头（可选），自定义 HTTP 请求头，如 {'Authorization': 'Bearer token'}",
            },
            "data": {
                "type": ["object", "array"],
                "description": "请求体数据（可选，仅 action=post 时使用），对象或数组，将作为 JSON 发送",
            },
            "params": {
                "type": "object",
                "additionalProperties": {"type": "string"},
                "description": "URL 查询参数（可选），将自动附加到 URL 后",
            },
            "timeout": {
                "type": "integer",
                "default": 30,
                "description": "超时时间（秒，可选），默认 30 秒",
            },
            "extract_text": {
                "type": "boolean",
                "default": True,
                "description": "是否提取纯文本（可选，仅在 fetch 时有效），默认 true，将去除 HTML 标签返回纯文本",
            },
        },
        "required": ["url"],
    },
    description=(
        "Web 操作工具：执行 HTTP 请求和网页抓取。支持 GET/POST 请求和网页内容抓取。"
        "使用场景：调用外部 API；抓取网页内容；发送 HTTP 请求获取远程数据。"
        "限制：请求默认 30 秒超时；响应大小限制为 10MB；某些域名可能被安全策略阻止。"
    ),
)
async def web_operate(**kwargs: dict[str, Any]) -> dict[str, Any]:
    t = _load_web_tool()()
    result = await t.execute(kwargs)
    # 失败信封显式 success=False（ADR 2026-09-28 收编契约）：本工具
    # output_schema required 含 status，裸 error 载荷缺 required 必撞校验红。
    return tool_result_envelope(result)

if __name__ == "__main__":
    plugin.run()
