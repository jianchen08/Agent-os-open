#!/usr/bin/env python3
"""LSP 工具 MCP 服务端——接口适配层。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

# 将 0.1 源码目录加入 sys.path，使老代码的 from tools.* 导入可用
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
_SRC_ROOT = os.path.join(_PROJECT_ROOT, 'src')
if os.path.isdir(_SRC_ROOT):
    sys.path.insert(0, _SRC_ROOT)

from lingxi_plugin_sdk import AgentOSPlugin  # noqa: E402

plugin = AgentOSPlugin("lsp_tool")

@plugin.tool(
    name="lsp_definition",
    schema={"type": "object", "properties": {"file_path": {"type": "string"}, "line": {"type": "integer"}, "character": {"type": "integer", "default": 0}}, "required": ["file_path", "line"]},
    description="跳转到符号定义位置",
)
async def lsp_definition(**kwargs):
    from tool import LSPTools  # noqa: PLC0415
    t = LSPTools()
    result = await t.execute({"action": "definition", **kwargs})
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="lsp_references",
    schema={"type": "object", "properties": {"file_path": {"type": "string"}, "line": {"type": "integer"}, "character": {"type": "integer", "default": 0}}, "required": ["file_path", "line"]},
    description="查找符号的所有引用位置",
)
async def lsp_references(**kwargs):
    from tool import LSPTools  # noqa: PLC0415
    t = LSPTools()
    result = await t.execute({"action": "references", **kwargs})
    return result.output if result.success else {"error": result.error}

@plugin.tool(
    name="lsp_diagnostics",
    schema={"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]},
    description="获取文件的诊断信息",
)
async def lsp_diagnostics(**kwargs):
    from tool import LSPTools  # noqa: PLC0415
    t = LSPTools()
    result = await t.execute({"action": "diagnostics", **kwargs})
    return result.output if result.success else {"error": result.error}

if __name__ == "__main__":
    plugin.run()
