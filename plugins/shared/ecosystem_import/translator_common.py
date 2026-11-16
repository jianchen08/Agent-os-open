"""外部生态翻译器共用面：翻译错误类型 + 清单/正文读取（Codex 与 Claude 前端单源）。

每家前端（:mod:`codex_translator` / :mod:`claude_translator`）只实现自己的
目录布局差异；JSON 清单读取、作者/许可证字段归一、Markdown frontmatter
解析、MCP 服务器事实位判定统一在此——两份实现必然漂移（同一坏件在两家
前端下报不同结论），故收敛为单源。

坏件一律 fail-closed：:class:`TranslationError` 带机器可读 ``code``，由工具
边界翻译成失败信封。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml
from import_ir import McpServer

__all__ = [
    "MCP_TOP_LEVEL_KINDS",
    "TranslationError",
    "author_name",
    "license_value",
    "load_json_object",
    "mcp_server_map",
    "mcp_servers_from_map",
    "read_frontmatter",
    "read_frontmatter_best_effort",
]

#: 允许省略 ``mcpServers`` 包裹层（服务器名放顶层）的来源类型：Claude Code 两种
#: 形态皆属官方现行规范（见 ``claude_translator`` 模块 docstring）；Codex 实测只有
#: 包裹形态，其顶层形态仍按坏件拒（``MCP_INVALID``）。事实面与写面共用本判定。
MCP_TOP_LEVEL_KINDS: frozenset[str] = frozenset({"claude_code"})

# frontmatter 段：文件开头的 ``---`` 行到下一个 ``---`` 行（CRLF 容忍）
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


class TranslationError(Exception):
    """翻译失败（错误是值：工具边界翻译成失败信封，不静默降级）。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def load_json_object(path: Path, *, error_code: str, label: str) -> dict[str, Any]:
    """读 JSON 对象；不可读/不可解析/顶层非对象 → 显式 TranslationError。

    ``UnicodeDecodeError`` 属 ValueError 族（非 OSError），读面必须显式纳入。
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TranslationError(f"{label} 不可解析：{path}（{type(exc).__name__}: {exc}）", error_code) from exc
    if not isinstance(data, dict):
        raise TranslationError(f"{label} 顶层不是 JSON 对象：{path}", error_code)
    return data


def author_name(raw: Any) -> str:
    """作者声明两种形态（``{"name": ...}`` / 裸字符串）归一为名字串。"""
    if isinstance(raw, dict):
        return str(raw.get("name") or "").strip()
    return str(raw or "").strip()


def license_value(raw: Any) -> str | None:
    """许可证声明：非字符串形态按缺失处理（闸门侧 fail-closed 拒收）。"""
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def mcp_server_map(path: Path, *, allow_top_level: bool) -> dict[str, Any]:
    """读 ``.mcp.json`` 的服务器映射（事实面与写面共用的解析单源）。

    Args:
        path: 源 ``.mcp.json`` 路径。
        allow_top_level: True 时顶层服务器映射与 ``{"mcpServers": {...}}`` 包裹层
            同语义（见 :data:`MCP_TOP_LEVEL_KINDS`）。

    Raises:
        TranslationError: 文件不可读/不可解析，或该形态下没有服务器映射
            （``MCP_INVALID``，不猜）。
    """
    doc = load_json_object(path, error_code="MCP_INVALID", label=".mcp.json")
    servers = doc.get("mcpServers", doc) if allow_top_level else doc.get("mcpServers")
    if not isinstance(servers, dict):
        raise TranslationError(f".mcp.json 缺 mcpServers 映射：{path}", "MCP_INVALID")
    return servers


def mcp_servers_from_map(servers: dict[str, Any], *, path: Path) -> list[McpServer]:
    """服务器映射 → MCP 事实位（transport 判定 + 是否带 OAuth 块）。

    只产出事实位：``url`` / ``command`` / ``env`` / ``oauth`` 内容一律不入 IR
    （凭据与端点不得经导入链回流到 LLM 摘要、日志或落库产物）。

    Raises:
        TranslationError: 条目非对象或无法判定运输（``MCP_INVALID``，不猜）。
    """
    entries: list[McpServer] = []
    for name in sorted(servers):
        entry = servers[name]
        if not isinstance(entry, dict):
            raise TranslationError(f"{path.name} 服务器 {name} 不是对象：{path}", "MCP_INVALID")
        if entry.get("url"):
            transport = "http"
        elif entry.get("command"):
            transport = "stdio"
        else:
            raise TranslationError(f"{path.name} 服务器 {name} 缺 url/command（无法判定运输）：{path}", "MCP_INVALID")
        entries.append(McpServer(name=str(name), transport=transport, requires_oauth="oauth" in entry))
    return entries


def read_frontmatter(path: Path, *, error_code: str, label: str) -> dict[str, Any]:
    """严格读取 Markdown 的 YAML frontmatter（坏件 fail-closed）。

    无 frontmatter 段 = 空映射（字段缺失由调用方按声明面回落处理）。

    Raises:
        TranslationError: 文件不可读/非 UTF-8，或 frontmatter 段不可解析/非映射。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TranslationError(f"{label} 不可读：{path}（{exc}）", error_code) from exc
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise TranslationError(f"{label} frontmatter 不可解析：{path}（{exc}）", error_code) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise TranslationError(f"{label} frontmatter 不是映射：{path}", error_code)
    return data


def read_frontmatter_best_effort(path: Path) -> dict[str, Any]:
    """宽松读取 Markdown 的 YAML frontmatter（事实面用，永不抛错）。

    与 Claude 装载语义对齐：frontmatter 不解析的文件照常装载、字段整体忽略
    （agents/commands 是只上报的事实面，一个坏文件不应拖垮整包翻译）；
    不可解析时按无 frontmatter 处理（字段回落文件名/空值）。
    """
    try:
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        data = yaml.safe_load(match.group(1)) if match is not None else None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}
