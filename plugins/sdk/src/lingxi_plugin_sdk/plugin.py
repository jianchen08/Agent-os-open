"""AgentOSPlugin 基类。

提供工具注册、资源注册、生命周期钩子、能力句柄获取和 MCP 服务端启动。

[来源: docs/tasks/task_08_python_sdk.md AC-07-1/AC-07-3/AC-07-5]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lingxi_plugin_sdk.capability import STANDARD_CAPABILITIES, CapabilityHandle
from lingxi_plugin_sdk.server import McpServer
from lingxi_plugin_sdk.types import LifecycleEvent, ResourceDef, ToolDef


class AgentOSPlugin:
    """插件基类——所有插件的入口点。

    使用方式：
        plugin = AgentOSPlugin("my_plugin")

        @plugin.tool(name="search", schema={"type": "object", ...})
        async def search(query: str) -> dict:
            return {"results": [...]}

        if __name__ == "__main__":
            plugin.run()

    Attributes:
        name: 插件名称。
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._tools: dict[str, ToolDef] = {}
        self._resources: dict[str, ResourceDef] = {}
        self._lifecycle_handlers: dict[str, Callable[..., Any]] = {}
        self._capabilities: dict[str, CapabilityHandle] = {}
        self._injected_config: dict[str, Any] = {}

    # ── 工具注册 ──────────────────────────────────────────

    def register_tool(
        self,
        name: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        description: str = "",
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        """注册工具。

        Args:
            name: 工具名称。
            schema: JSON Schema 描述输入参数。
            handler: 处理函数（async 或 sync）。
            description: 工具描述。
            output_schema: 输出 JSON Schema（可选）。
        """
        self._tools[name] = ToolDef(
            name=name,
            schema=schema,
            handler=handler,
            description=description,
            output_schema=output_schema,
        )

    def tool(
        self,
        name: str,
        schema: dict[str, Any],
        description: str = "",
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器快捷方式——注册工具。

        Usage:
            @plugin.tool(name="search", schema={...})
            async def search(query: str) -> dict:
                ...
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register_tool(name, schema, func, description)
            return func

        return decorator

    # ── 资源注册 ──────────────────────────────────────────

    def register_resource(
        self,
        uri: str,
        handler: Callable[..., Any],
        name: str = "",
        description: str = "",
        mime_type: str = "application/json",
    ) -> None:
        """注册资源。

        Args:
            uri: 资源 URI。
            handler: 资源读取函数。
            name: 资源名称。
            description: 资源描述。
            mime_type: MIME 类型。
        """
        self._resources[uri] = ResourceDef(
            uri=uri,
            handler=handler,
            name=name,
            description=description,
            mime_type=mime_type,
        )

    # ── 生命周期钩子 ──────────────────────────────────────

    def on_lifecycle(self, event: str, handler: Callable[..., Any]) -> None:
        """注册生命周期钩子。

        Args:
            event: 事件类型（on_load/on_unload/on_config_change 等）。
            handler: 事件处理函数。
        """
        self._lifecycle_handlers[event] = handler

    def on_load(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """装饰器——注册 on_load 钩子。

        Usage:
            @plugin.on_load
            async def handle_load(params: dict) -> None:
                ...
        """
        self._lifecycle_handlers[LifecycleEvent.ON_LOAD.value] = func
        return func

    def on_unload(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """装饰器——注册 on_unload 钩子。"""
        self._lifecycle_handlers[LifecycleEvent.ON_UNLOAD.value] = func
        return func

    def on_config_change(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """装饰器——注册 on_config_change 钩子。"""
        self._lifecycle_handlers[LifecycleEvent.ON_CONFIG_CHANGE.value] = func
        return func

    # ── 能力句柄 ──────────────────────────────────────────

    def get_capability(self, name: str) -> CapabilityHandle:
        """获取依赖注入的能力句柄。

        Args:
            name: 能力名称（如 pipeline-executor/config-reader/tenant-context 等）。

        Returns:
            能力句柄实例。

        Raises:
            KeyError: 如果能力未注入。
        """
        if name not in self._capabilities:
            raise KeyError(f"capability not injected: {name}")
        return self._capabilities[name]

    def get_config(self) -> dict[str, Any]:
        """获取内核注入的插件配置。"""
        return self._injected_config

    # ── MCP 服务端启动 ────────────────────────────────────

    def _on_initialize(self, params: dict[str, Any]) -> None:
        """MCP initialize 握手时被调用——接收依赖注入。

        Args:
            params: 内核传入的 initialize 参数，含 capabilities 和 config。
        """
        # 注入能力句柄
        injected_caps = params.get("capabilities", {})
        for cap_name in STANDARD_CAPABILITIES:
            cap_info = injected_caps.get(cap_name)
            if cap_info is not None:
                self._capabilities[cap_name] = CapabilityHandle(
                    name=cap_name,
                    call_fn=cap_info.get("call_fn"),
                    context=cap_info.get("context", {}),
                )

        # 注入配置
        self._injected_config = params.get("config", {})

    def run(self) -> None:
        """启动 MCP 服务端，阻塞运行。

        从 stdin 读取 JSON-RPC 请求，向 stdout 写入响应。
        与 Rust 内核 McpClient 对接。
        """
        import asyncio

        server = McpServer(
            tools=self._tools,
            resources=self._resources,
            lifecycle_handlers=self._lifecycle_handlers,
            on_initialize=self._on_initialize,
        )
        asyncio.run(server.run())
