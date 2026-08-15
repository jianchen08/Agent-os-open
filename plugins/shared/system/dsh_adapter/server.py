#!/usr/bin/env python3
"""DSH 插件适配器 MCP 服务端（task_dsh_plugin_adapter 任务 2 + 4）。

三个工具面：
- ``dsh_read`` / ``dsh_glob``：通道 A 桥接——DSH 非 MCP 工具跑在自己的
  Node runtime（runtime/dsh-rpc-bridge.mjs 管理的 DSH cordis context），
  本 sidecar 只作宿主 + JSON-RPC 桥。工具契约（input/output schema +
  render 意图）在 plugin.json 锁定（DSH commit 47f9438），tool_core 按
  output_schema 校验返回、前端按 render 意图路由渲染——闭环即任务 1 的
  消费端。
- ``dsh_translate_manifest``：清单翻译器出口（translator.translate_package）。

生命周期：on_unload 时 shutdown Node 子进程（防孤儿）；桥不可用时工具
返回结构化错误（fail-soft，不影响本插件其他工具）。
"""

from __future__ import annotations

import logging
from typing import Any

from bridge import get_bridge, shutdown_bridge
from translator import load_installed_plugins, translate_package

from agentos_plugin_sdk import AgentOSPlugin

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("dsh_adapter")


@plugin.tool(
    name="dsh_read",
    schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to read, resolved by the filesystem backend."},
            "offset": {"type": "integer", "description": "1-based first line to return. Defaults to 1."},
            "limit": {"type": "integer", "description": "Maximum number of lines to return. Defaults to 2000."},
        },
        "required": ["file_path"],
    },
    description="Read a UTF-8 text file and return line-numbered content (DSH runtime bridge).",
    output_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"number": {"type": "integer"}, "text": {"type": "string"}},
                    "required": ["number", "text"],
                },
            },
            "totalLines": {"type": "integer"},
        },
        "required": ["path", "offset", "lines", "totalLines"],
    },
    render={
        "card": "read",
        "bindings": {"path": "result.path", "lines": "result.lines", "totalLines": "result.totalLines"},
    },
)
async def dsh_read(file_path: str, offset: int | None = None, limit: int | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"file_path": file_path}
    if offset is not None:
        args["offset"] = offset
    if limit is not None:
        args["limit"] = limit
    return await get_bridge().call_tool("read", args)


@plugin.tool(
    name="dsh_glob",
    schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": 'Glob pattern to match file paths against (e.g. "**/*.ts").',
            },
            "path": {"type": "string", "description": "Directory to search in. Defaults to the bridge workspace."},
        },
        "required": ["pattern"],
    },
    description="Discover files whose paths match a glob pattern, sorted by modification time (DSH runtime bridge).",
    output_schema={
        "type": "object",
        "properties": {"root": {"type": "string"}, "paths": {"type": "array", "items": {"type": "string"}}},
        "required": ["root", "paths"],
    },
    render={"card": "search", "bindings": {"paths": "result.paths"}},
)
async def dsh_glob(pattern: str, path: str | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"pattern": pattern}
    if path is not None:
        args["path"] = path
    return await get_bridge().call_tool("glob", args)


@plugin.tool(
    name="dsh_translate_manifest",
    schema={
        "type": "object",
        "properties": {
            "package_path": {"type": "string", "description": "DSH 插件包目录（含 package.json 的源码目录；缺省 = 适配器自己的 dsh_plugins/ 全量）"}
        },
        "required": [],
    },
    description="Translate a DSH plugin package (or all installed ones under dsh_plugins/) into AgentOS-equivalent registration manifests.",
)
async def dsh_translate_manifest(package_path: str | None = None) -> dict[str, Any]:
    """单包翻译（指定路径）或全量装载翻译（缺省扫 dsh_plugins/）。"""
    if package_path is None:
        return load_installed_plugins()
    return translate_package(package_path)


@plugin.tool(
    name="dsh_list_plugins",
    schema={"type": "object", "properties": {}, "required": []},
    description="List DSH plugin packages installed under the adapter's dsh_plugins/ directory (name/version/client/renderers).",
)
async def dsh_list_plugins() -> dict[str, Any]:
    """汇报已装载的 DSH 插件包（轻量：不跑 Node runtime，纯清单翻译）。"""
    loaded = load_installed_plugins()
    return {
        "count": loaded["count"],
        "base_dir": loaded["base_dir"],
        "plugins": [
            {
                "package": p["source"]["package"],
                "version": p["source"]["version"],
                "is_client_plugin": p["client"]["is_client_plugin"],
                "renderers": [r["tool"] for r in p["client"]["renderers"]],
                "adapter_scope": p["client"]["adapter_scope"],
            }
            for p in loaded["packages"]
        ],
        "errors": loaded["errors"],
    }


@plugin.on_unload
async def _on_dsh_adapter_unload(params: dict) -> None:  # noqa: ARG001
    await shutdown_bridge()
    logger.info("dsh_adapter: node runtime bridge shut down")


if __name__ == "__main__":
    plugin.run()
