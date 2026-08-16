"""MCP 服务端（官方 mcp SDK v2 承载）。

传输/协议层复用官方 ``mcp`` 包（``mcp.server.lowlevel.Server`` + stdio transport），
与 Rust 内核 rmcp 同源不同实现，协议合规由官方维护。AgentOS 私有协议扩展以
最小侵入方式挂接：

- **initialize 依赖注入**：内核在标准 initialize params 之外附送 ``capabilities``
  （可反调的内核能力命名空间）与 ``config``（插件配置）。经 ServerMiddleware
  观察原始 params（pydantic 校验前，自定义字段不受模型约束），回调
  ``on_initialize`` 完成 CapabilityHandle 注入。
- **生命周期通知**：内核以自定义通知 ``notifications/<hook>`` 推送生命周期事件
  （on_load / on_unload / on_config_change / on_pipeline_start / on_pipeline_end /
  on_error），经 ``add_notification_handler`` 注册分发。params 为任意 JSON
  （config / tags），从 ``ctx.params`` 原始 mapping 读取。
- **反向 capability 调用**：插件通过 KernelChannel 持连接级 ``Outbound`` 通道，
  以 ``send_raw_request("<capability>.<method>")`` / ``notify`` 发起反向调用，
  内核 reader loop 路由回写——与内核 ``McpClient::handle_incoming_request``
  对齐。请求/响应关联、并发分发、stdout 串行化均由官方 SDK 的 dispatcher 负责。

入站请求（tools/call 等）由官方 dispatcher 每消息独立 task 分发，工具 handler
内部发起的反向调用不会被读循环阻塞（无死锁）。

``AgentOSPlugin`` 基类 API 不变；插件代码零改动。

[来源: docs/tasks/task_08_python_sdk.md AC-07-2]
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp_types.jsonrpc import INVALID_PARAMS

from agentos_plugin_sdk.types import LifecycleEvent, ResourceDef, ToolDef

logger = logging.getLogger(__name__)

SERVER_NAME = "agentos-plugin-sdk"
SERVER_VERSION = "0.2.0"


def _augment_description(description: str, output_schema: dict[str, Any] | None) -> str:
    """声明了 output_schema 时，description 追加单行输出契约摘要。

    模型/客户端不解析结构化 outputSchema 字段时，仍能从描述读到输出形状
    （task_dsh_plugin_adapter 任务 1：输出契约"模型可读"）。
    """
    if not output_schema:
        return description
    try:
        compact = json.dumps(output_schema, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return description
    line = f"Output contract: {compact}"
    return f"{description}\n{line}" if description else line


# 反向 capability 调用等待内核响应的超时（与旧自研通道一致的 30s 默认）。
CAPABILITY_CALL_TIMEOUT_S = 30.0


def _bind_log_context(log_ctx: dict[str, Any]) -> contextlib.AbstractContextManager[Any]:
    """将内核注入的 per-request 上下文绑定到当前 async 上下文的日志追踪字段。

    优先复用 ``agentos_plugin_sdk.logging.LogContext``（contextvars，async 安全）；不可用时
    返回 no-op context manager（降级场景，绑定信息丢失但日志仍正常输出）。

    返回一个 context manager：进入时 bind，退出时自动恢复（防并发污染）。
    """
    # 仅保留非空字段（None / "-" 视为未设置）
    fields = {k: str(v) for k, v in log_ctx.items() if v not in (None, "", "-")}
    if not fields:
        from contextlib import nullcontext

        return nullcontext()

    try:
        from agentos_plugin_sdk.logging import LogContext  # noqa: PLC0415

        return LogContext.scoped(**fields)
    except Exception:  # noqa: BLE001
        from contextlib import nullcontext

        return nullcontext()


def _filter_handler_kwargs(handler: Any, arguments: dict[str, Any]) -> dict[str, Any]:
    """按 handler 签名过滤工具参数，避免内部注入字段污染纯函数工具。

    param_inject 插件会向所有工具参数注入内部上下文字段（parent_agent_level、
    timestamp 等）。task 系工具签名含 **kwargs（VAR_KEYWORD），依赖
    parent_agent_level 做权限校验，必须全量透传；纯函数工具无 **kwargs，
    只传签名中声明的参数名，内部注入字段被过滤。

    Args:
        handler: 工具 handler。
        arguments: 内核透传的工具参数（_log_ctx 已由调用方 pop）。

    Returns:
        过滤后的参数 dict。签名获取失败时回退全量透传（保持旧行为）。
    """
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return arguments
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
        return arguments
    return {k: v for k, v in arguments.items() if k in sig.parameters}


def _coerce_args_by_schema(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    """按 schema 把字符串数值参数强制转为数值类型。

    LLM 经常把数值参数生成成字符串（如 ``"start_line": "5"``、``"limit": "20"``），
    而 JSON Schema 声明的是 integer/number。SDK 分发层若不转换，handler 内的算术
    或比较（``start_line - 1``、``len(lines) - tail``）会抛 TypeError，导致整段功能
    不可用。仅对 schema 中声明为 integer/number 且实际值为 str 的参数做 best-effort
    转换，转换失败则保留原值（不破坏既有行为，也绝不静默吞掉真实类型错误）。
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict):
        return arguments
    coerced = dict(arguments)
    for key, decl in props.items():
        if key not in coerced or not isinstance(decl, dict):
            continue
        val = coerced[key]
        if not isinstance(val, str):
            continue
        declared = decl.get("type")
        declared = declared if isinstance(declared, list) else [declared]
        if "integer" in declared:
            try:
                coerced[key] = int(val)
            except (ValueError, TypeError):
                pass
        elif "number" in declared:
            try:
                coerced[key] = float(val)
            except (ValueError, TypeError):
                pass
    return coerced


class KernelChannel:
    """sidecar→内核反向调用通道。

    插件通过 CapabilityHandle.call()/notify() 发起反向调用时，本通道经
    官方 SDK 的连接级 ``Outbound``（与入站共用同一 stdio 连接）发出：

    - request（``send_raw_request``）：method 形如 ``<capability>.<method>``
      （如 ``pipeline-executor.resume``），内核 reader loop 识别后路由到
      CapabilityRouter 并回写响应，SDK dispatcher 自动完成 id 关联。
    - notification（``notify``）：fire-and-forget（如 ``event-bus.emit`` 流式
      chunk 推送），内核不回响应。

    通道在首条入站消息（initialize）到达时由 McpServer 的 middleware 绑定；
    此前调用会抛 RuntimeError（与旧通道"未连接"语义一致）。
    """

    def __init__(self) -> None:
        self._outbound: Any | None = None

    def attach(self, outbound: Any) -> None:
        """绑定连接级 Outbound 通道（由 McpServer middleware 在首条消息时调用）。"""
        self._outbound = outbound

    def is_attached(self) -> bool:
        """通道是否已绑定（initialize 握手后为 True）。"""
        return self._outbound is not None

    def _require_outbound(self) -> Any:
        if self._outbound is None:
            raise RuntimeError("kernel channel not attached (initialize handshake not received)")
        return self._outbound

    async def send_request(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        """向内核发起一次反向 capability 调用，等待响应。

        Args:
            method: 形如 "pipeline-executor.resume" 的命名空间方法名
            params: 调用参数
            timeout: 等待响应超时（秒）；None 用 CAPABILITY_CALL_TIMEOUT_S。
                长等待语义的方法（如 human-interaction.wait_for_choice 等
                用户响应）必须显式传大值，否则默认 30s 会先于用户操作掐断。

        Returns:
            内核返回的 result（dict）

        Raises:
            RuntimeError: 内核返回 error、超时（timeout / CAPABILITY_CALL_TIMEOUT_S），
                或通道未绑定（initialize 前）。
        """
        outbound = self._require_outbound()
        try:
            return await outbound.send_raw_request(
                method,
                params or {},
                {"timeout": timeout if timeout is not None else CAPABILITY_CALL_TIMEOUT_S},
            )
        except MCPError as e:
            raise RuntimeError(f"kernel capability call failed [{e.error.code}] {method}: {e.error.message}") from None

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """向内核发起一次 fire-and-forget 的 capability 通知（不等响应）。

        用于流式 chunk 推送：sidecar 每生成一个 chunk 就发一个 notification，
        内核收到后直接推前端。不等响应避免每个 chunk 阻塞（send_request
        不可用于高频流式）。无 id，内核不回 response。

        Args:
            method: 形如 "event-bus.emit" 的命名空间方法名
            params: 通知参数（如 {"event": "stream_chunk", "thread_id": ..., "chunk": ...}）

        Raises:
            RuntimeError: 通道未绑定（initialize 前）。
        """
        outbound = self._require_outbound()
        await outbound.notify(method, params or {})


class McpServer:
    """MCP 服务端（官方 ``mcp.server.lowlevel.Server`` 门面）。

    把 AgentOSPlugin 注册的 tools / resources / lifecycle handlers 桥接到
    官方 SDK 的 handler 体系，通过 stdio transport 与 Rust 内核 McpClient 对接。

    私有协议扩展的挂接点：
    - initialize 依赖注入：middleware 观察 initialize 原始 params
    - 生命周期通知：``notifications/<hook>`` 自定义通知分发
    - 反向调用通道：initialize 时绑定 KernelChannel
    """

    def __init__(
        self,
        tools: dict[str, ToolDef],
        resources: dict[str, ResourceDef],
        lifecycle_handlers: dict[str, Any],
        on_initialize: Any | None = None,
        kernel_channel: KernelChannel | None = None,
    ) -> None:
        self._tools = tools
        self._resources = resources
        self._lifecycle_handlers = lifecycle_handlers
        self._on_initialize = on_initialize
        self._kernel_channel = kernel_channel
        self._sdk = self._build_sdk_server()

    # ── 官方 SDK 装配 ────────────────────────────────────

    def _build_sdk_server(self) -> Server:
        """构建官方 low-level Server 并挂接 AgentOS 私有扩展。"""
        server: Server = Server(
            SERVER_NAME,
            version=SERVER_VERSION,
            on_list_tools=self._on_list_tools,
            on_call_tool=self._on_call_tool,
            on_list_resources=self._on_list_resources,
            on_read_resource=self._on_read_resource,
        )
        server.middleware.append(self._kernel_bridge_middleware)
        for event in LifecycleEvent:
            server.add_notification_handler(
                f"notifications/{event.value}",
                types.NotificationParams,
                self._make_lifecycle_handler(event.value),
            )
        return server

    async def _kernel_bridge_middleware(self, ctx: Any, call_next: Any) -> Any:
        """AgentOS 私有扩展与官方协议的桥接 middleware。

        覆盖每条入站消息（含 initialize）：

        1. 首条消息到达时，把连接级 Outbound 绑定到 KernelChannel（此后插件
           可发起反向 capability 调用）。stdio 单连接生命周期内只需绑一次。
        2. initialize 握手时，把原始 params（内核附送 capabilities/config 自定义
           字段，pydantic 模型会忽略、原始 mapping 保留全量）回调给
           ``on_initialize`` 完成依赖注入。

        注意：initialize 由官方 runner inline 处理（读循环停靠等待），此处
        绝不能 await 反向请求（会死锁）——仅做同步引用记录，安全。
        """
        if self._kernel_channel is not None and not self._kernel_channel.is_attached():
            # ServerSession 未公开 connection，经 _connection 取连接级通道
            # （与 ServerSession.send_request 无 related_request_id 时同一通道）。
            connection = getattr(ctx.session, "_connection", None)
            if connection is not None:
                self._kernel_channel.attach(connection.outbound)

        if ctx.method == "initialize" and self._on_initialize is not None:
            raw = dict(ctx.params) if ctx.params else {}
            self._on_initialize(raw)

        return await call_next(ctx)

    # ── tools/list · tools/call ──────────────────────────

    async def _on_list_tools(self, ctx: Any, params: Any) -> types.ListToolsResult:
        """tools/list——返回已注册工具（schema 以 ToolDef 声明为准，不从签名推导）。

        output_schema 经 MCP 标准 ``outputSchema`` 字段下发（2025-06-18 结构化输出）；
        render 意图经 ``_meta`` 透传（协议安全通道）。声明了 output_schema 的工具，
        description 追加单行输出契约摘要——消费端（模型/客户端）不读结构化字段也能
        知道输出形状（task_dsh_plugin_adapter 任务 1）。
        """
        tools: list[types.Tool] = []
        for td in self._tools.values():
            tool = types.Tool(
                name=td.name,
                description=_augment_description(td.description, td.output_schema),
                input_schema=td.schema,
            )
            if td.output_schema is not None:
                tool.output_schema = td.output_schema
            if td.render is not None:
                tool.meta = {"render": td.render}
            tools.append(tool)
        return types.ListToolsResult(tools=tools)

    async def _on_call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        """tools/call——调用指定工具并返回结果（原始 dict 分发层见 _handle_tools_call）。"""
        return await self._handle_tools_call({"name": params.name, "arguments": dict(params.arguments or {})})

    async def _handle_tools_call(self, params: dict[str, Any]) -> types.CallToolResult:
        """分发 tools/call 到已注册 handler（保留旧分发语义，供单测直调）。"""
        name = params.get("name", "")
        arguments = dict(params.get("arguments") or {})

        td = self._tools.get(name)
        if td is None:
            raise MCPError(code=INVALID_PARAMS, message=f"tool not found: {name}")

        # 内核在 tool_args 注入 _log_ctx（per-request 上下文：pipeline_id 等）。
        # 在调 handler 前绑定到日志上下文（contextvars，async 安全），请求结束自动恢复。
        # 从 arguments 移除，避免作为工具参数传给 handler（handler 只期望 state/config）。
        log_ctx = arguments.pop("_log_ctx", None) or {}

        with _bind_log_context(log_ctx):
            arguments = _coerce_args_by_schema(td.schema, arguments)
            kwargs = _filter_handler_kwargs(td.handler, arguments)
            # 预校验形参绑定：必填参数缺失时（如 trigger_review 的 task_id 未到达
            # sidecar），Python 会在调用处抛裸 TypeError（"missing positional
            # argument"）。提前用 signature.bind 探测并转为结构化错误，避免崩溃
            # 冒泡为未捕获异常。bind 只校验形参绑定、不执行 handler 体，因此不会
            # 吞掉 handler 内部的真实 TypeError。签名不可内省（部分内置/动态
            # handler，inspect 抛 ValueError）时跳过，回退到直接调用。
            try:
                inspect.signature(td.handler).bind(**kwargs)
            except ValueError:
                pass
            except TypeError as e:
                logger.warning("[mcp] 工具 %s 参数绑定失败: %s | kwargs=%s", name, e, list(kwargs))
                return types.CallToolResult(
                    content=[
                        types.TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "success": False,
                                    "error": f"参数不匹配: {e}",
                                    "error_code": "INVALID_ARGUMENTS",
                                },
                                default=str,
                            ),
                        )
                    ],
                    is_error=True,
                )
            result = td.handler(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result

        # ToolResult 等携带 to_dict() 的结果对象必须序列化为 JSON 对象
        # （内核 invoker 按 content[0].text 的 JSON 对象解析 success/output/metadata），
        # 否则 default=str 会退化成字符串，丢失结构。
        if hasattr(result, "to_dict") and callable(result.to_dict):
            result = result.to_dict()

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result, default=str))],
            is_error=False,
        )

    async def _on_list_resources(self, ctx: Any, params: Any) -> types.ListResourcesResult:
        """resources/list——返回已注册资源。"""
        resources = [
            types.Resource(
                uri=rd.uri,
                name=rd.name or rd.uri,
                description=rd.description or None,
                mime_type=rd.mime_type,
            )
            for rd in self._resources.values()
        ]
        return types.ListResourcesResult(resources=resources)

    # ── resources/read ───────────────────────────────────

    async def _on_read_resource(self, ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        """resources/read——读取指定资源。"""
        return await self._handle_resources_read({"uri": str(params.uri)})

    async def _handle_resources_read(self, params: dict[str, Any]) -> types.ReadResourceResult:
        """分发 resources/read（保留旧分发语义，供单测直调）。"""
        uri = params.get("uri", "")
        rd = self._resources.get(uri)
        if rd is None:
            raise MCPError(code=INVALID_PARAMS, message=f"resource not found: {uri}")

        result = rd.handler()
        if asyncio.iscoroutine(result):
            result = await result

        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=uri,
                    mime_type=rd.mime_type,
                    text=json.dumps(result, default=str),
                )
            ],
        )

    # ── 生命周期通知 ─────────────────────────────────────

    def _make_lifecycle_handler(self, event_value: str) -> Any:
        """构造 notifications/<hook> 处理器（闭包捕获事件名）。

        params 从 ctx.params 原始 mapping 读取——内核推送的 config/tags 是
        任意 JSON，NotificationParams 模型只声明 _meta，校验模型会丢弃
        其余字段。
        """

        async def _handler(ctx: Any, params: Any) -> None:
            handler = self._lifecycle_handlers.get(event_value)
            if handler is None:
                return
            raw = dict(ctx.params) if ctx.params else {}
            result = handler(raw)
            if asyncio.iscoroutine(result):
                await result

        return _handler

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """分发生命周期通知（保留旧入口，供单测直调）。

        非生命周期方法（含协议通知 notifications/initialized）安全忽略。
        """
        event_map = {
            "notifications/on_load": LifecycleEvent.ON_LOAD,
            "notifications/on_unload": LifecycleEvent.ON_UNLOAD,
            "notifications/on_config_change": LifecycleEvent.ON_CONFIG_CHANGE,
            "notifications/on_pipeline_start": LifecycleEvent.ON_PIPELINE_START,
            "notifications/on_pipeline_end": LifecycleEvent.ON_PIPELINE_END,
            "notifications/on_error": LifecycleEvent.ON_ERROR,
            "notifications/domain_event": LifecycleEvent.DOMAIN_EVENT,
        }

        event = event_map.get(method)
        if event is None:
            return

        handler = self._lifecycle_handlers.get(event.value)
        if handler is not None:
            result = handler(dict(params))
            if asyncio.iscoroutine(result):
                await result

    # ── 启动 ─────────────────────────────────────────────

    async def run(self) -> None:
        """启动服务端——官方 stdio transport，阻塞运行直到 stdin EOF。

        Windows 兼容由官方 stdio_server 保证：底层以线程池包裹阻塞 IO，
        不依赖 ProactorEventLoop 的 pipe 支持；服务期间 fd0/fd1 重定向
        （fd1 → stderr 副本），插件代码的 stray print 不会污染协议通道。
        """
        async with stdio_server() as (read_stream, write_stream):
            await self._sdk.run(
                read_stream,
                write_stream,
                self._sdk.create_initialization_options(),
                raise_exceptions=False,
            )
