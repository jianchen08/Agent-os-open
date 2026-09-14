"""文档处理工具 MCP 服务端——Markdown↔Word↔PDF 互转 + 文本提取。

两个工具：
- ``doc_convert``：格式转换（md/docx/pdf 三格式互转，同格式显式拒绝）
- ``doc_extract_text``：文本提取（任意支持格式 → 纯文本，供 LLM 摘要/翻译）

摘要/翻译的"内容理解"走 LLM（本工具只做格式转换与文本提取，不调 LLM）。

合宿契约：server.py 暴露模块级 plugin 实例（host.py getattr(module, "plugin")），
注册在装载期完成。本插件依赖 python-docx/pypdf/reportlab（重依赖，不在共享
venv），不设 host_group——走独立 sidecar venv（与 builtin_tools/lsp 同款）。

[来源: plugins/shared/tools/simple/server.py 模块级实例形态；
 plugins/shared/tools/media/server.py 工具注册形态]
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from agentos_plugin_sdk import AgentOSPlugin, ToolResult, create_failure_result, create_success_result

logger = logging.getLogger(__name__)

from doc_convert import convert_file, extract_text
from doc_core import (
    FORMAT_EXTENSIONS,
    Block,
    DocumentError,
    detect_format,
    guard_input_file,
    resolve_in_workspace,
    run_with_timeout,
)

plugin = AgentOSPlugin("doc_tools")

# ── 工具 schema ───────────────────────────────────────────────

DOC_CONVERT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_path": {
            "type": "string",
            "description": "源文件路径（.md/.markdown/.docx/.pdf，相对工作区或工作区内绝对路径）",
        },
        "target_format": {
            "type": "string",
            "enum": ["md", "docx", "pdf"],
            "description": "目标格式：md（Markdown）/ docx（Word）/ pdf",
        },
        "target_path": {
            "type": "string",
            "description": "目标文件路径（缺省=源文件同目录同名换扩展名）",
        },
        "overwrite": {
            "type": "boolean",
            "description": "目标文件已存在时是否覆盖（默认 false，显式拒绝防误写）",
        },
    },
    "required": ["source_path", "target_format"],
}

DOC_CONVERT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["output", "source_format", "target_format", "blocks"],
    "properties": {
        "output": {"type": "string", "description": "输出文件绝对路径"},
        "source_format": {"type": "string", "description": "源格式（md/docx/pdf）"},
        "target_format": {"type": "string", "description": "目标格式（md/docx/pdf）"},
        "blocks": {"type": "integer", "description": "转换的文档块数"},
        "size": {"type": "integer", "description": "输出文件字节数"},
    },
}

DOC_EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_path": {
            "type": "string",
            "description": "源文件路径（.md/.markdown/.docx/.pdf，相对工作区或工作区内绝对路径）",
        },
        "max_chars": {
            "type": "integer",
            "description": "文本截断上限（字符数，缺省不截断）",
        },
    },
    "required": ["source_path"],
}

DOC_EXTRACT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text", "source_format", "chars"],
    "properties": {
        "text": {"type": "string", "description": "提取的纯文本内容"},
        "source_format": {"type": "string", "description": "源格式（md/docx/pdf）"},
        "chars": {"type": "integer", "description": "文本字符数（截断后）"},
        "truncated": {"type": "boolean", "description": "是否被 max_chars 截断"},
    },
}


# ── 工具实现 ──────────────────────────────────────────────────


def _default_target_path(source: Any, target_format: str) -> Any:
    """缺省目标路径：源文件同目录同名换扩展名。"""
    return source.with_suffix(FORMAT_EXTENSIONS[target_format])


async def doc_convert(
    source_path: str,
    target_format: str,
    target_path: str | None = None,
    overwrite: bool = False,
    workspace: str | None = None,
    project_root: str | None = None,
) -> ToolResult:
    """文档格式转换（md↔docx↔pdf）。

    workspace/project_root 为 param_inject 运行时注入参数（服务端权威值，
    不出现在 LLM schema）；路径以工作区为锚 fail-closed。
    """
    try:
        source = resolve_in_workspace(source_path, workspace, project_root)
        guard_input_file(source)
        source_format = detect_format(source)
        if target_format not in FORMAT_EXTENSIONS:
            raise DocumentError(
                f"不支持的目标格式：{target_format}（支持：md/docx/pdf）",
                code="DOC_UNSUPPORTED_FORMAT",
            )
        if source_format == target_format:
            raise DocumentError(
                f"源格式与目标格式相同（{source_format}），无需转换。请指定不同目标格式。",
                code="DOC_SAME_FORMAT",
            )
        target = (
            resolve_in_workspace(target_path, workspace, project_root)
            if target_path
            else _default_target_path(source, target_format)
        )
        if target.exists() and not overwrite:
            raise DocumentError(
                f"目标文件已存在：{target.name}。如需覆盖请显式传 overwrite=true。",
                code="DOC_TARGET_EXISTS",
            )
        info = await run_with_timeout(convert_file, source, target, target_format)
        info["size"] = target.stat().st_size
        return create_success_result(info)
    except DocumentError as e:
        return create_failure_result(str(e), error_code=e.code)
    except Exception as e:  # noqa: BLE001 - 未预期异常转显式失败，不静默
        logger.exception("[doc_tools] doc_convert 未预期失败")
        return create_failure_result(
            f"转换失败（未预期错误）：{type(e).__name__}: {e}", error_code="DOC_FAILED"
        )


async def doc_extract_text(
    source_path: str,
    max_chars: int | None = None,
    workspace: str | None = None,
    project_root: str | None = None,
) -> ToolResult:
    """文档文本提取（md/docx/pdf → 纯文本，供 LLM 摘要/翻译消费）。"""
    try:
        source = resolve_in_workspace(source_path, workspace, project_root)
        info = await run_with_timeout(extract_text, source, max_chars)
        return create_success_result(info)
    except DocumentError as e:
        return create_failure_result(str(e), error_code=e.code)
    except Exception as e:  # noqa: BLE001 - 未预期异常转显式失败，不静默
        logger.exception("[doc_tools] doc_extract_text 未预期失败")
        return create_failure_result(
            f"文本提取失败（未预期错误）：{type(e).__name__}: {e}", error_code="DOC_FAILED"
        )


# ── 注册 ──────────────────────────────────────────────────────


def create_plugin() -> AgentOSPlugin:
    """注册工具（幂等；供独立运行与测试复用）。"""
    plugin.register_tool(
        "doc_convert",
        DOC_CONVERT_SCHEMA,
        doc_convert,
        "文档格式转换（Markdown/Word/PDF 互转）",
        output_schema=DOC_CONVERT_OUTPUT_SCHEMA,
        render={"card": "file", "title": "文档转换"},
    )
    plugin.register_tool(
        "doc_extract_text",
        DOC_EXTRACT_SCHEMA,
        doc_extract_text,
        "文档文本提取（Word/PDF/Markdown → 纯文本）",
        output_schema=DOC_EXTRACT_OUTPUT_SCHEMA,
        render={"card": "read", "title": "文本提取"},
    )
    return plugin


create_plugin()


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


TOOL_REGISTRY = {
    "doc_convert": (DOC_CONVERT_SCHEMA, doc_convert),
    "doc_extract_text": (DOC_EXTRACT_SCHEMA, doc_extract_text),
}


if __name__ == "__main__":
    run()
