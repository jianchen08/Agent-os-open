"""AgentOSPlugin 基类。

提供工具注册、资源注册、生命周期钩子、能力句柄获取和 MCP 服务端启动。

[来源: docs/tasks/task_08_python_sdk.md AC-07-1/AC-07-3/AC-07-5]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk.capability import STANDARD_CAPABILITIES, CapabilityHandle
from agentos_plugin_sdk.server import KernelChannel, McpServer
from agentos_plugin_sdk.types import LifecycleEvent, ResourceDef, ToolDef


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
        # sidecar→内核反向调用通道（与 McpServer 共享，复用 stdin 多路复用）
        self._kernel_channel: KernelChannel | None = None

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

    async def record_metric(
        self,
        name: str,
        value: float,
        metric_type: str = "counter",
        labels: dict[str, str] | None = None,
        unit: str | None = None,
        help_text: str | None = None,
    ) -> Any:
        """上报一个业务指标到内核聚合器（监控设计 §三 通道2）。

        内核自动把当前 plugin_id 作为指标的命名空间（series.plugin_id），
        插件只需写短名（如 ``tokens_used``），不必加前缀。

        Args:
            name: 指标短名（如 "tokens_used"、"calls_total"）。
            value: 指标值。
            metric_type: "counter"（单调累加）/ "gauge"（覆盖）/ "histogram"（分布）。
            labels: 维度标签（如 {"model": "deepseek"}）。值禁含换行/双引号。
            unit: 单位（可选，如 "tokens"/"seconds"）。
            help_text: HELP 文本（可选，Prometheus 导出用）。

        Returns:
            内核返回的确认（{"status": "recorded", ...}）。

        Raises:
            KeyError: metrics capability 未注入（内核未启用聚合器）。
            RuntimeError: 调用失败。

        [来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]
        """
        cap = self.get_capability("metrics")
        params: dict[str, Any] = {
            "name": name,
            "value": value,
            "metric_type": metric_type,
        }
        if labels is not None:
            params["labels"] = {str(k): str(v) for k, v in labels.items()}
        if unit is not None:
            params["unit"] = unit
        if help_text is not None:
            params["help"] = help_text
        return await cap.call("record", params)

    # ── MCP 服务端启动 ────────────────────────────────────

    def _on_initialize(self, params: dict[str, Any]) -> None:
        """MCP initialize 握手时被调用——接收依赖注入。

        Args:
            params: 内核传入的 initialize 参数，含 capabilities 和 config。
        """
        # 懒初始化反向调用通道（在事件循环内创建，确保 future 归属正确循环）
        if self._kernel_channel is None:
            self._kernel_channel = KernelChannel()

        # 注入能力句柄——内核通过 capabilities dict 的 key 声明能力可用性，
        # call_fn 统一绑定到反向调用通道（method 由 CapabilityHandle.call 组装为
        # `<capability>.<method>` 命名空间）。
        injected_caps = params.get("capabilities", {})
        channel = self._kernel_channel

        def _make_call_fn(_cap_name: str) -> Any:
            async def _call(method: str, m_params: dict[str, Any]) -> Any:
                # 命名空间方法名：<capability>.<method>（与内核 parse_capability_method 对齐）
                full_method = f"{_cap_name}.{method}"
                return await channel.send_request(full_method, m_params)

            return _call

        def _make_notify_fn(_cap_name: str) -> Any:
            async def _notify(method: str, m_params: dict[str, Any]) -> None:
                # fire-and-forget：不等内核响应，用于流式 chunk 高频推送
                full_method = f"{_cap_name}.{method}"
                await channel.send_notification(full_method, m_params)

            return _notify

        for cap_name in STANDARD_CAPABILITIES:
            if cap_name in injected_caps:
                self._capabilities[cap_name] = CapabilityHandle(
                    name=cap_name,
                    call_fn=_make_call_fn(cap_name),
                    notify_fn=_make_notify_fn(cap_name),
                    context=injected_caps.get(cap_name, {}) or {},
                )

        # 注入配置
        self._injected_config = params.get("config", {})

    def run(self) -> None:
        """启动 MCP 服务端，阻塞运行。

        从 stdin 读取 JSON-RPC 请求，向 stdout 写入响应。
        与 Rust 内核 McpClient 对接。

        反向调用通道（KernelChannel）随服务端一起启动，共享 stdin 多路复用。
        """
        import asyncio

        # 通道由 _on_initialize 在握手时创建；此处兜底确保非 None（McpServer 需引用）
        if self._kernel_channel is None:
            self._kernel_channel = KernelChannel()

        server = McpServer(
            tools=self._tools,
            resources=self._resources,
            lifecycle_handlers=self._lifecycle_handlers,
            on_initialize=self._on_initialize,
            kernel_channel=self._kernel_channel,
        )
        asyncio.run(server.run())
