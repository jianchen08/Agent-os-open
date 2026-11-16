"""MCP 服务器条目写面 —— 源 ``.mcp.json`` 的非 OAuth 条目合并进当前配置根。

写目标 = 内核 ``config_root`` 同一解析出的配置根下的 ``mcp.json``（内核
plugin-loader 每轮 discover 读该文件生成 ``mcp:external`` 插件）。根解析复用
插件侧单源 ``plugins/shared/project_root.py``（``AGENTOS_CONFIG_ROOT`` 优先，
未命中自本插件位置向上探测含 ``config`` 的祖先目录）。

合并语义（幂等）：同名条目以**源文件**为准更新，其余条目与顶层其它键原样保留；
现值文件不可解析或没有 ``mcpServers`` 对象 = 该根对内核不可用，拒写且不改动原文件。

**凭据面**：写入内容一律从源 ``.mcp.json`` 现场逐字段读取，不经 IR、不进日志、
不进返回值——本模块返回面只带服务器名、transport 与跳过原因。源里的 ``oauth``
块永不落配置（内核不读 auth 块；``requires_oauth`` 条目整体跳过）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from _root_api import atomic_write_text, resolve_project_root
from import_ir import PluginIR
from translator_common import MCP_TOP_LEVEL_KINDS, mcp_server_map, mcp_servers_from_map

__all__ = [
    "CONFIG_ROOT_ENV",
    "MCP_FILENAME",
    "MCP_SOURCE_FILENAME",
    "McpConfigError",
    "McpWriteReport",
    "SkippedServer",
    "WrittenServer",
    "resolve_config_root",
    "write_mcp_servers",
]

MCP_FILENAME = "mcp.json"
MCP_SOURCE_FILENAME = ".mcp.json"
CONFIG_DIRNAME = "config"
#: 内核钉配置根的环境变量名（与 ``agent_config_load`` 同款直读；值 = ``<根>/config`` 或根本身）
CONFIG_ROOT_ENV = "AGENTOS_CONFIG_ROOT"
#: OAuth 条目的跳过原因（本仓尚无 OAuth 授权能力；写进去就是一个连不上的条目）
OAUTH_SKIP_REASON = "需 OAuth 授权，本机暂无此能力（单独批次在做），故未写入（源 oauth 块不落配置）"


class McpConfigError(Exception):
    """MCP 写面拒收/失败（错误是值：调用方翻译成失败信封，不静默降级）。"""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WrittenServer:
    """一条已写入的服务器（报告面：只带名字与 transport，不带内容）。"""

    name: str
    transport: str


@dataclass(frozen=True)
class SkippedServer:
    """一条未写入的服务器（名字 + 原因；原因不含源内容）。"""

    name: str
    reason: str


@dataclass(frozen=True)
class McpWriteReport:
    """MCP 写面结果：已写入条目 + 跳过条目。"""

    written: tuple[WrittenServer, ...]
    skipped: tuple[SkippedServer, ...]


def resolve_config_root() -> Path:
    """当前配置根（内核 ``config_root`` 同一解析：``AGENTOS_CONFIG_ROOT`` 优先）。

    解析单源 ``plugins/shared/project_root.py``（env 值 = ``<根>/config`` 或根本身，
    按指纹校验后采用；未命中自本插件位置向上探测含 ``config`` 的祖先目录）。内核按
    env 值**逐字**取配置根，故 env 已钉根时解析结果必须就是它指向的根——不一致即拒写
    （回落探测会写到另一个根，造出「写 A 层、内核读 B 层」的分叉）。

    Raises:
        McpConfigError: 两侧皆解析不到（``CONFIG_ROOT_UNRESOLVED``）或 env 与解析结果
            不一致（``CONFIG_ROOT_MISMATCH``）——均拒写不猜。
    """
    root = resolve_project_root(CONFIG_DIRNAME, start=Path(__file__).resolve().parent)
    if root is None:
        raise McpConfigError(
            "配置根解析失败（AGENTOS_CONFIG_ROOT 缺失且非仓库布局），拒写 MCP 配置",
            "CONFIG_ROOT_UNRESOLVED",
        )
    resolved = root / CONFIG_DIRNAME
    env_value = os.environ.get(CONFIG_ROOT_ENV, "").strip()
    if env_value and Path(env_value) not in (resolved, root):
        raise McpConfigError(
            f"AGENTOS_CONFIG_ROOT（{env_value}）与解析到的配置根（{resolved}）不一致，拒写"
            "（不回落其他根：内核按 env 值逐字读取配置根）",
            "CONFIG_ROOT_MISMATCH",
        )
    return resolved


def write_mcp_servers(ir: PluginIR, *, config_root: Path) -> McpWriteReport:
    """把源 ``.mcp.json`` 的非 OAuth 条目合并进 ``config_root`` 下的 ``mcp.json``。

    Args:
        ir: 翻译器产出的插件 IR——只用 ``kind``（形态判定）与 ``source_dir``
            （写面现场重读源文件，条目内容不经 IR 回流）。
        config_root: 配置根目录（调用方经 :func:`resolve_config_root` 解析）。

    Returns:
        已写入条目（名字 + transport）与跳过条目（名字 + 原因）；源无 ``.mcp.json``
        或无条目可写时不触碰配置面。

    Raises:
        TranslationError: 源 ``.mcp.json`` 不可读/不可解析/条目形态非法
            （与翻译面对同一文件同码 ``MCP_INVALID``）。
        McpConfigError: 现值配置不可解析或缺 ``mcpServers`` 对象（``MCP_CONFIG_INVALID``，
            拒写不覆盖真值）/ 写入失败（``MCP_CONFIG_WRITE_FAILED``）。
    """
    source = ir.source_dir / MCP_SOURCE_FILENAME
    if not source.is_file():
        return McpWriteReport(written=(), skipped=())
    servers = mcp_server_map(source, allow_top_level=ir.kind in MCP_TOP_LEVEL_KINDS)
    specs: dict[str, Any] = {}
    written: list[WrittenServer] = []
    skipped: list[SkippedServer] = []
    for fact in mcp_servers_from_map(servers, path=source):
        if fact.requires_oauth:
            skipped.append(SkippedServer(name=fact.name, reason=OAUTH_SKIP_REASON))
            continue
        spec = dict(servers[fact.name])
        # 不变量：写入条目不携带 oauth 块（内核不读 auth 块，凭据块不落配置）
        spec.pop("oauth", None)
        specs[fact.name] = spec
        written.append(WrittenServer(name=fact.name, transport=fact.transport))
    if not specs:
        return McpWriteReport(written=(), skipped=tuple(skipped))
    _merge_into(config_root / MCP_FILENAME, specs)
    return McpWriteReport(written=tuple(written), skipped=tuple(skipped))


def _merge_into(target: Path, specs: Mapping[str, Any]) -> None:
    """合并写入（同名以本次条目为准、其余保留），原子替换（失败目标保持旧内容）。"""
    doc = _load_config(target)
    servers = dict(doc["mcpServers"])
    servers.update(specs)
    doc["mcpServers"] = servers
    try:
        atomic_write_text(target, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise McpConfigError(f"MCP 配置写入失败：{target}（{exc}）", "MCP_CONFIG_WRITE_FAILED") from exc


def _load_config(path: Path) -> dict[str, Any]:
    """读现值配置；文件不存在 = 空配置。

    Raises:
        McpConfigError: 文件存在但不可解析/顶层非对象/无 ``mcpServers`` 对象——
            内核对该形态同判为不可用，故拒写（不覆盖真值、不静默修形）。
    """
    if not path.is_file():
        return {"mcpServers": {}}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise McpConfigError(
            f"现值 MCP 配置不可解析（拒覆盖）：{path}（{type(exc).__name__}: {exc}）",
            "MCP_CONFIG_INVALID",
        ) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("mcpServers"), dict):
        raise McpConfigError(f"现值 MCP 配置缺 mcpServers 对象（拒覆盖）：{path}", "MCP_CONFIG_INVALID")
    return doc
