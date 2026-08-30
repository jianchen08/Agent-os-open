"""合宿宿主聚合 MCP 服务端（co-hosting aggregate server）。

单进程承载多个 ``AgentOSPlugin`` 实例（插件合宿进程模型，
docs/working/插件合宿进程模型优化方案_20260826.md §4.3）：

- **工具命名空间**：成员插件工具注册为 ``{plugin_id}.{tool_name}``
  （schema/description/output_schema/render 原样保留），tools/call 经聚合
  工具表直达成员 handler——前缀即路由，无需运行期解析；
- **initialize 依赖注入扇出**：内核一次握手注入的 capabilities/config 分发
  到全部成员，全部成员共享同一 KernelChannel（反向调用走本服务端唯一的
  stdio 连接）；
- **生命周期通知扇出**：``notifications/<hook>`` 分发到全部成员的对应
  handler（params 逐成员拷贝，防前一个成员修改影响后一个）；
- **资源前缀聚合**：成员资源以 ``{plugin_id}.{uri}`` 命名空间聚合。

成员定位与加载（sys.modules 平铺裸名隔离）是宿主进程的职责，见
``plugins/shared/_host/host.py``；本类只做协议层聚合。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from agentos_plugin_sdk._logging import setup_sidecar_logging
from agentos_plugin_sdk.plugin import AgentOSPlugin
from agentos_plugin_sdk.server import KernelChannel, McpServer
from agentos_plugin_sdk.types import ResourceDef, ToolDef


class CohostServer:
    """多 AgentOSPlugin 实例的单进程聚合 MCP 服务端。

    Args:
        members: plugin_id → 成员插件实例。至少一个；键将作为工具/资源
            命名空间前缀，须互不相同。

    Raises:
        ValueError: 成员为空，或聚合后出现重名工具/资源（成员 id 含点号
            等导致命名空间互相覆盖）。
    """

    def __init__(self, members: Mapping[str, AgentOSPlugin]) -> None:
        if not members:
            raise ValueError("cohost server requires at least one member plugin")
        self._members: dict[str, AgentOSPlugin] = dict(members)
        self._channel = KernelChannel()
        self._tools = self._aggregate_tools()
        self._resources = self._aggregate_resources()
        self._server = McpServer(
            tools=self._tools,
            resources=self._resources,
            lifecycle_handlers=self._aggregate_lifecycle_handlers(),
            on_initialize=self._fan_out_initialize,
            kernel_channel=self._channel,
        )

    @property
    def tool_names(self) -> list[str]:
        """聚合后对外的工具全名（``{plugin_id}.{tool_name}``，排序稳定）。"""
        return sorted(self._tools)

    async def serve(self) -> None:
        """启动聚合 MCP 服务端（stdio transport），阻塞运行至 stdin EOF。"""
        setup_sidecar_logging()
        await self._server.run()

    # ── 聚合装配 ──────────────────────────────────────────

    def _aggregate_tools(self) -> dict[str, ToolDef]:
        """成员工具 → ``{plugin_id}.{tool_name}``（handler 引用原样共享）。"""
        tools: dict[str, ToolDef] = {}
        for plugin_id, plugin in self._members.items():
            for name, tool_def in plugin._tools.items():
                full_name = f"{plugin_id}.{name}"
                if full_name in tools:
                    raise ValueError(f"cohost tool name conflict: {full_name}")
                tools[full_name] = replace(tool_def, name=full_name)
        return tools

    def _aggregate_resources(self) -> dict[str, ResourceDef]:
        """成员资源 → ``{plugin_id}.{uri}`` 前缀聚合。"""
        resources: dict[str, ResourceDef] = {}
        for plugin_id, plugin in self._members.items():
            for uri, resource_def in plugin._resources.items():
                full_uri = f"{plugin_id}.{uri}"
                if full_uri in resources:
                    raise ValueError(f"cohost resource uri conflict: {full_uri}")
                resources[full_uri] = replace(resource_def, uri=full_uri)
        return resources

    def _aggregate_lifecycle_handlers(self) -> dict[str, Callable[..., Any]]:
        """成员生命周期钩子 → 每事件一个扇出 handler。"""
        handlers_by_event: dict[str, list[Callable[..., Any]]] = {}
        for plugin in self._members.values():
            for event, handler in plugin._lifecycle_handlers.items():
                handlers_by_event.setdefault(event, []).append(handler)
        return {event: self._make_fan_out_handler(handlers) for event, handlers in handlers_by_event.items()}

    def _make_fan_out_handler(self, handlers: list[Callable[..., Any]]) -> Callable[..., Any]:
        """构造单事件扇出：同步/异步 handler 按成员注册顺序执行。"""

        async def _fan_out(params: dict[str, Any]) -> None:
            for handler in handlers:
                result = handler(dict(params))
                if asyncio.iscoroutine(result):
                    await result

        return _fan_out

    def _fan_out_initialize(self, params: dict[str, Any]) -> None:
        """initialize 握手扇出：全部成员共享本服务端的 KernelChannel 并各自注入。"""
        for plugin in self._members.values():
            # 预置共享通道：成员 _on_initialize 懒建 KernelChannel 的分支因此
            # 复用共享实例，成员的反向调用走本服务端唯一 stdio 连接。
            plugin._kernel_channel = self._channel
            plugin._on_initialize(params)
