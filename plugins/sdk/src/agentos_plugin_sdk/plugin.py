"""AgentOSPlugin 基类。

提供工具注册、步骤服务注册、管道钩子注册、资源注册、生命周期钩子、能力
句柄获取和 MCP 服务端启动。

- 步骤服务：``@plugin.step(name)`` 注册管道具名步骤（与 plugin.json 的
  ``capabilities.steps`` 对齐）；内核以 ``config["_step_method"]`` 明示调用
  时，SDK 分发到对应 handler，未注册即拒绝（fail-closed）。未注入该键的
  存量调用走原默认 execute 路径，插件零感知。
- 管道钩子：``@plugin.pipe_hook(event)`` 注册管道级观察者；内核以
  ``config["_pipe_hook"]`` 经同一 execute 通道同步调用，handler 返回 dict
  （可含结构化否决指令 ``{"decision": "terminate", "reason": ...}``）。
  见 docs/working/管道步骤服务化与能力本位提案_20260827.md §3.4/§3.6。

[来源: docs/tasks/task_08_python_sdk.md AC-07-1/AC-07-3/AC-07-5]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentos_plugin_sdk.capability import CapabilityHandle
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
        # 管道步骤服务注册表：name → handler（name 与 manifest capabilities.steps 一致）
        self.steps: dict[str, Callable[..., Any]] = {}
        # 管道钩子注册表：event → handler 列表（同一事件多 handler 顺序调用）
        self.pipe_hooks: dict[str, list[Callable[..., Any]]] = {}
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
        render: dict[str, Any] | None = None,
    ) -> None:
        """注册工具。

        Args:
            name: 工具名称。
            schema: JSON Schema 描述输入参数。
            handler: 处理函数（async 或 sync）。
            description: 工具描述。
            output_schema: 输出 JSON Schema（可选）。
            render: 渲染意图声明（可选），见 ToolDef.render。
        """
        self._tools[name] = ToolDef(
            name=name,
            schema=schema,
            handler=handler,
            description=description,
            output_schema=output_schema,
            render=render,
        )

    def tool(
        self,
        name: str,
        schema: dict[str, Any],
        description: str = "",
        output_schema: dict[str, Any] | None = None,
        render: dict[str, Any] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器快捷方式——注册工具。

        Usage:
            @plugin.tool(name="search", schema={...})
            async def search(query: str) -> dict:
                ...
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register_tool(name, schema, func, description, output_schema, render)
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

    # ── 步骤服务注册（管道服务化，提案 §3.4）──────────────

    def step(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器——注册管道具名步骤 handler。

        name 必须与 plugin.json 的 ``capabilities.steps`` 里声明的步骤名一致
        （内核按 manifest 具名调用，声明与实现错位属 manifest 错误）。

        Usage:
            @plugin.step("task.remind")
            async def remind(state: dict, config: dict | None = None) -> dict:
                return {"state_updates": {...}}

        与 execute 相同的返回契约（state updates dict 等）；handler 收到
        state/config 原样透传（``config["_step_method"]`` 保留，供 handler
        内省）。
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.steps[name] = func
            return func

        return decorator

    def pipe_hook(self, event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器——注册管道钩子观察者 handler。

        同一事件可注册多个 handler，分发时按注册顺序 await 调用。

        Usage:
            @plugin.pipe_hook("stream_chunk")
            async def on_chunk(payload: dict) -> dict | None:
                if payload.get("bad"):
                    return {"decision": "terminate", "reason": "..."}
                return None

        Returns:
            handler 返回 dict（非空返回收集进分发结果，可含结构化否决指令
            ``{"decision": "terminate", "reason": <str>}``）；返回 None 表示
            观察但不产生输出。
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.pipe_hooks.setdefault(event, []).append(func)
            return func

        return decorator

    def get_declared_steps(self) -> list[str]:
        """返回已注册的步骤名清单（排序稳定）。

        用于 manifest 一致性自检：与 plugin.json ``capabilities.steps`` 声明
        核对（SDK 不持有 manifest 文件路径时，手工核对即可——内核按 manifest
        具名调用，声明与注册错位会在调用时以 StepNotFoundError fail-closed）。
        """
        return sorted(self.steps)

    # ── 资源注册 ──────────────────────────────────────────

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

    def on_domain_event(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """装饰器——注册域事件钩子（通用域事件通道）。

        内核在域事件锚点（会话创建/删除/活跃切换等）发 notifications/domain_event，
        params 携带 ``event``（事件名，如 "session.created"）+ 任意标签
        （session_id/pipeline_id/user_id 等）。订阅前提：plugin.json 的
        ``capabilities.lifecycle_hooks`` 含 ``"domain_event"``。

        Usage:
            @plugin.on_domain_event
            async def handle_domain(params: dict) -> None:
                if params.get("event") == "session.created":
                    ...
        """
        self._lifecycle_handlers[LifecycleEvent.DOMAIN_EVENT.value] = func
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
            async def _call(method: str, m_params: dict[str, Any], timeout: float | None = None) -> Any:
                # 命名空间方法名：<capability>.<method>（与内核 parse_capability_method 对齐）
                full_method = f"{_cap_name}.{method}"
                return await channel.send_request(full_method, m_params, timeout)

            return _call

        def _make_notify_fn(_cap_name: str) -> Any:
            async def _notify(method: str, m_params: dict[str, Any]) -> None:
                # fire-and-forget：不等内核响应，用于流式 chunk 高频推送
                full_method = f"{_cap_name}.{method}"
                await channel.send_notification(full_method, m_params)

            return _notify

        # 遍历内核实际声明的 capabilities（而非固定 STANDARD_CAPABILITIES 清单），
        # 这样内核动态注册的 namespace（如 human-interaction，M4 插件自注册）
        # 也能被 SDK 创建 CapabilityHandle。STANDARD_CAPABILITIES 仅作 SDK 侧的
        # 文档/校验参考，不再限制注入范围。
        declared_caps = list(injected_caps.keys()) if isinstance(injected_caps, dict) else []
        for cap_name in declared_caps:
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

        反向调用通道（KernelChannel）由服务端 middleware 在 initialize 握手时
        绑定到官方 SDK 的连接级通道。
        """
        import asyncio

        # sidecar 日志统一初始化：stdout 被 JSON-RPC 占用，日志输出到 stderr，
        # 由内核 McpClient 的 stderr reader 消费转发到 tracing。
        from agentos_plugin_sdk._logging import setup_sidecar_logging

        setup_sidecar_logging()

        # 通道由 _on_initialize 在握手时创建；此处兜底确保非 None（McpServer 需引用）
        if self._kernel_channel is None:
            self._kernel_channel = KernelChannel()

        server = McpServer(
            tools=self._tools,
            resources=self._resources,
            lifecycle_handlers=self._lifecycle_handlers,
            on_initialize=self._on_initialize,
            kernel_channel=self._kernel_channel,
            steps=self.steps,
            pipe_hooks=self.pipe_hooks,
        )
        asyncio.run(server.run())
