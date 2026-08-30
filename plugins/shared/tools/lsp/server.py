#!/usr/bin/env python3
"""LSP MCP 服务端——纯接口适配层。

老代码从 0.1 src/lsp/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from gateway import LSPGateway, get_lsp_gateway  # noqa: E402
from lsp_types import Position  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("lsp_service")

_gateway: LSPGateway | None = None


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize LSP gateway on load."""
    global _gateway
    _gateway = await get_lsp_gateway()
    logger.info("LSP gateway initialized")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Shutdown LSP gateway on unload."""
    global _gateway
    if _gateway is not None:
        await _gateway.shutdown()
        _gateway = None
    logger.info("LSP gateway shutdown")


@plugin.tool(
    name="lsp_definition",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "description": "行号（从0开始）"},
            "character": {"type": "integer", "description": "列号（从0开始）", "default": 0},
            "language": {"type": "string", "description": "编程语言（不传则自动检测）"},
        },
        "required": ["file_path", "line"],
    },
    description="跳转到符号定义位置",
)
async def lsp_definition(file_path: str, line: int, character: int = 0, language: str | None = None) -> dict[str, Any]:
    """Go to definition of symbol at given position."""
    if _gateway is None:
        return {"error": "LSP gateway not initialized"}
    position = Position(line=line, character=character)
    locations = await _gateway.go_to_definition(file_path, position, language)
    return {
        "locations": [
            {"uri": loc.uri, "range": {"start": {"line": loc.range.start.line, "character": loc.range.start.character}, "end": {"line": loc.range.end.line, "character": loc.range.end.character}}}
            for loc in locations
        ],
        "count": len(locations),
    }


@plugin.tool(
    name="lsp_references",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "description": "行号（从0开始）"},
            "character": {"type": "integer", "description": "列号（从0开始）", "default": 0},
            "language": {"type": "string", "description": "编程语言（不传则自动检测）"},
        },
        "required": ["file_path", "line"],
    },
    description="查找符号的所有引用位置",
)
async def lsp_references(file_path: str, line: int, character: int = 0, language: str | None = None) -> dict[str, Any]:
    """Find all references of symbol at given position."""
    if _gateway is None:
        return {"error": "LSP gateway not initialized"}
    position = Position(line=line, character=character)
    locations = await _gateway.find_references(file_path, position, language)
    return {
        "references": [
            {"uri": loc.uri, "range": {"start": {"line": loc.range.start.line, "character": loc.range.start.character}, "end": {"line": loc.range.end.line, "character": loc.range.end.character}}}
            for loc in locations
        ],
        "count": len(locations),
    }


@plugin.tool(
    name="lsp_diagnostics",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "language": {"type": "string", "description": "编程语言（不传则自动检测）"},
        },
        "required": ["file_path"],
    },
    description="获取文件的诊断信息（错误和警告）",
)
async def lsp_diagnostics(file_path: str, language: str | None = None) -> dict[str, Any]:
    """Get diagnostics for a file."""
    if _gateway is None:
        return {"error": "LSP gateway not initialized"}
    diagnostics = await _gateway.get_diagnostics(file_path, language)
    return {
        "diagnostics": [
            {
                "range": {
                    "start": {"line": d.range.start.line, "character": d.range.start.character},
                    "end": {"line": d.range.end.line, "character": d.range.end.character},
                },
                "severity": d.severity,
                "code": d.code,
                "source": d.source,
                "message": d.message,
            }
            for d in diagnostics
        ],
        "count": len(diagnostics),
    }


@plugin.tool(
    name="lsp_jump_to_file",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "文件路径"},
            "line": {"type": "integer", "description": "行号（从0开始）", "default": 0},
            "character": {"type": "integer", "description": "列号（从0开始）", "default": 0},
        },
        "required": ["file_path"],
    },
    description="在 IDE 中打开文件并跳转到指定位置",
)
async def lsp_jump_to_file(file_path: str, line: int = 0, character: int = 0) -> dict[str, Any]:
    """Open file in IDE and jump to position."""
    from file_jump import FileJumpProtocol  # noqa: PLC0415
    position = Position(line=line, character=character) if line or character else None
    success = await FileJumpProtocol.jump_to_file(file_path, position)
    return {"success": success, "file_path": file_path, "line": line, "character": character}


if __name__ == "__main__":
    plugin.run()
