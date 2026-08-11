"""MCP JSON-RPC 服务端。

自研轻量级 JSON-RPC 2.0 over stdio 实现，不依赖外部 MCP 包。
响应 initialize / tools/list / tools/call / resources/read / notifications 请求。

双向通信：
- 入站（内核→sidecar）：initialize / tools/list / tools/call / notifications/*
- 出站（sidecar→内核）：capability 反向调用（如 pipeline-executor.resume）
  通过 KernelChannel 发出 JSON-RPC request，内核 reader loop 识别后路由并回写响应。

[来源: docs/tasks/task_08_python_sdk.md AC-07-2]
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import uuid
from typing import Any

from agentos_plugin_sdk.types import LifecycleEvent, ResourceDef, ToolDef

logger = logging.getLogger(__name__)


def _bind_log_context(log_ctx: dict[str, Any]):
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


class KernelChannel:
    """sidecar→内核反向调用通道。

    插件通过 CapabilityHandle.call() 发起反向调用时，本通道：
    1. 生成 JSON-RPC request（method=`<capability>.<method>`），写入 stdout
    2. 注册 pending future，等待内核回写的 response

    response 通过 McpServer.run() 的 stdin 读取循环到达（与入站请求复用同一 stdin），
    由 McpServer._dispatch_response 解析并 resolve 对应 future。

    线程/协程安全：pending 表用 asyncio.Lock 保护；stdout 写入串行化。
    """

    def __init__(self) -> None:
        self._stdout_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_lock = asyncio.Lock()

    def register_pending(self, req_id: str, future: asyncio.Future[Any]) -> None:
        """注册一个等待内核响应的 future（由 McpServer.run 的同步包装调用）。"""
        self._pending[req_id] = future

    def resolve_pending(self, req_id: str, result: Any, error: Any) -> bool:
        """内核响应到达时 resolve 对应 future。

        Returns:
            True 表示该 id 是反向调用的响应（已处理）；False 表示不是（交给入站分发）。
        """
        future = self._pending.pop(req_id, None)
        if future is None or future.done():
            return False
        if error is not None:
            future.set_exception(
                RuntimeError(f"kernel capability call failed: {error}")
            )
        else:
            future.set_result(result)
        return True

    async def send_request(self, method: str, params: dict[str, Any]) -> Any:
        """向内核发起一次反向 capability 调用，等待响应。

        Args:
            method: 形如 "pipeline-executor.resume" 的命名空间方法名
            params: 调用参数

        Returns:
            内核返回的 result 字段

        Raises:
            RuntimeError: 内核返回 error，或超时（30s）
        """
        req_id = uuid.uuid4().hex
        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self.register_pending(req_id, future)

        async with self._stdout_lock:
            sys.stdout.write(json.dumps(request) + "\n")
            sys.stdout.flush()

        try:
            return await asyncio.wait_for(future, timeout=30.0)
        except TimeoutError:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"kernel capability call timeout: {method}") from None

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """向内核发起一次 fire-and-forget 的 capability 通知（不等响应）。

        用于流式 chunk 推送：sidecar 每生成一个 chunk 就发一个 notification，
        内核收到后直接推前端。不等响应避免每个 chunk 阻塞 30s（send_request
        不可用于高频流式）。无 id，内核不回 response，无 pending future 泄漏。

        Args:
            method: 形如 "event-bus.emit" 的命名空间方法名
            params: 通知参数（如 {"event": "stream_chunk", "thread_id": ..., "chunk": ...}）
        """
        notification = {
            "jsonrpc": "2.0",
            # 注意：无 id 字段 → JSON-RPC notification（内核不回 response）
            "method": method,
            "params": params,
        }
        async with self._stdout_lock:
            sys.stdout.write(json.dumps(notification) + "\n")
            sys.stdout.flush()


class McpServer:
    """MCP JSON-RPC 服务端。

    通过 stdin/stdout 收发 JSON-RPC 2.0 消息，与 Rust 内核 McpClient 对接。

    支持的 MCP 方法：
    - initialize: 协议握手 + 依赖注入
    - tools/list: 列出已注册工具
    - tools/call: 调用工具
    - resources/read: 读取资源
    - notifications/*: 生命周期通知（fire-and-forget）
    """

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_INFO = {"name": "agentos-plugin-sdk", "version": "0.2.0"}

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
        self._running = False

    async def run(self) -> None:
        """启动服务端，从 stdin 读取 JSON-RPC 请求，向 stdout 写入响应。

        阻塞运行直到 stdin EOF 或收到 shutdown 信号。

        Windows 兼容：Python 3.14 的 ProactorEventLoop 对 `connect_read_pipe(sys.stdin)`
        支持不稳定（_ProactorReadPipeTransport._loop_reading 崩溃），故 Windows 上改用
        线程内同步读取 stdin + run_coroutine_threadsafe 回到事件循环处理消息。
        """
        self._running = True
        if sys.platform == "win32":
            await self._run_win32()
        else:
            await self._run_unix()

    async def _run_unix(self) -> None:
        """Unix：用 asyncio stream reader 读 stdin（原生异步）。"""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(lambda: protocol, sys.stdin)

        while self._running:
            line = await reader.readline()
            if not line:
                break
            await self._process_line(line)

    async def _run_win32(self) -> None:
        """Windows：线程内同步读 stdin，通过 run_coroutine_threadsafe 回事件循环。

        connect_read_pipe 在 Windows ProactorEventLoop 上会崩溃（Python 3.14），
        故用阻塞 IO 读 stdin，每读到一行就调度回事件循环处理（保持 _handle_message
        的 async 语义和 KernelChannel 的 future 归属正确）。
        """
        import threading  # noqa: PLC0415

        loop = asyncio.get_running_loop()
        lines_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _stdin_reader() -> None:
            """同步阻塞读 stdin，投递到队列；EOF 投 None。"""
            try:
                for raw in sys.stdin.buffer:
                    asyncio.run_coroutine_threadsafe(lines_queue.put(raw), loop).result()
            except (OSError, ValueError):
                pass
            finally:
                asyncio.run_coroutine_threadsafe(lines_queue.put(None), loop).result()

        threading.Thread(target=_stdin_reader, daemon=True).start()

        while self._running:
            raw = await lines_queue.get()
            if raw is None:
                break
            await self._process_line(raw)

    async def _process_line(self, raw: bytes) -> None:
        """处理一行原始字节消息。"""
        line_str = raw.decode("utf-8", errors="replace").strip()
        if not line_str:
            return
        try:
            msg = json.loads(line_str)
        except json.JSONDecodeError:
            return
        await self._handle_message(msg)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        """处理单条 JSON-RPC 消息。

        三类消息分流：
        1. response（无 method，有 id，有 result/error）：内核对 sidecar 反向调用的响应
           → 交给 KernelChannel.resolve_pending
        2. notification（有 method，无 id）：fire-and-forget 生命周期通知
        3. request（有 method + id）：内核发起的请求，需响应
        """
        method = msg.get("method")
        msg_id = msg.get("id")

        # 分支1：内核对反向调用的响应（无 method，有 id，且 kernel_channel 在等）
        if method is None and msg_id is not None and self._kernel_channel is not None:
            result = msg.get("result")
            error = msg.get("error")
            # resolve_pending 返回 True 表示这是反向调用的响应，已处理
            if self._kernel_channel.resolve_pending(msg_id, result, error):
                return
            # 不是反向调用响应 → 落到下面的 request 处理（罕见）

        # 分支2：notification（无 id）—— fire-and-forget
        if msg_id is None:
            await self._handle_notification(method, params=msg.get("params", {}))
            return

        # 分支3：request（有 id）—— 需要响应
        # 用 create_task 并发处理，不阻塞主读取循环。
        # 原因：工具 handler 内部可能发起反向 capability 调用（cap.call），
        # 该调用的 response 会通过 stdin 到达。若 await dispatch 阻塞主循环，
        # response 永远进不来 → handler 的 future 永不 resolve → 死锁。
        # create_task 让主循环立即回到 lines_queue.get() 读下一条消息
        #（含反向调用的 response），handler 在后台 task 里 await 完成。
        asyncio.create_task(self._handle_request_async(msg_id, method, msg.get("params", {})))

    async def _handle_request_async(
        self, msg_id: Any, method: str, params: dict[str, Any]
    ) -> None:
        """异步处理单个 JSON-RPC request 并回写 response。"""
        try:
            result = await self._dispatch(method, params)
            await self._send_response_async(msg_id, result)
        except Exception as e:
            await self._send_error_async(msg_id, -32603, str(e))

    async def _send_response_async(self, msg_id: Any, result: Any) -> None:
        """向 stdout 发送 JSON-RPC 成功响应（加锁，与 KernelChannel 共享同一 lock）。"""
        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        # 复用 KernelChannel 的 _stdout_lock，保证 response / 反向 request / notification
        # 三类 stdout 写入互斥（create_task 并发后必须有锁，否则行交错）。
        lock = self._kernel_channel._stdout_lock if self._kernel_channel else _dummy_lock()
        async with lock:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

    async def _send_error_async(self, msg_id: Any, code: int, message: str) -> None:
        """向 stdout 发送 JSON-RPC 错误响应（加锁，与 KernelChannel 共享同一 lock）。"""
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
        lock = self._kernel_channel._stdout_lock if self._kernel_channel else _dummy_lock()
        async with lock:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        """分发 JSON-RPC 请求到对应处理器。"""
        if method == "initialize":
            return self._handle_initialize(params)
        if method == "tools/list":
            return self._handle_tools_list()
        if method == "tools/call":
            return await self._handle_tools_call(params)
        if method == "resources/read":
            return await self._handle_resources_read(params)
        raise ValueError(f"unknown method: {method}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理 initialize 请求——协议握手 + 接收依赖注入。"""
        if self._on_initialize is not None:
            self._on_initialize(params)
        return {
            "protocolVersion": self.PROTOCOL_VERSION,
            "serverInfo": self.SERVER_INFO,
            "capabilities": {
                "tools": {},
                "resources": {},
            },
        }

    def _handle_tools_list(self) -> dict[str, Any]:
        """处理 tools/list 请求——返回已注册工具列表。"""
        tools = []
        for td in self._tools.values():
            tools.append(
                {
                    "name": td.name,
                    "description": td.description,
                    "inputSchema": td.schema,
                }
            )
        return {"tools": tools}

    async def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理 tools/call 请求——调用指定工具并返回结果。"""
        name = params.get("name", "")
        arguments = dict(params.get("arguments") or {})

        td = self._tools.get(name)
        if td is None:
            raise ValueError(f"tool not found: {name}")

        # 内核在 tool_args 注入 _log_ctx（per-request 上下文：pipeline_id 等）。
        # 在调 handler 前绑定到日志上下文（contextvars，async 安全），请求结束自动恢复。
        # 从 arguments 移除，避免作为工具参数传给 handler（handler 只期望 state/config）。
        log_ctx = arguments.pop("_log_ctx", None) or {}

        with _bind_log_context(log_ctx):
            kwargs = _filter_handler_kwargs(td.handler, arguments)
            result = td.handler(**kwargs) if kwargs else td.handler()
            if asyncio.iscoroutine(result):
                result = await result

        # ToolResult 等携带 to_dict() 的结果对象必须序列化为 JSON 对象
        #（内核 invoker 按 content[0].text 的 JSON 对象解析 success/output/metadata），
        # 否则 default=str 会退化成字符串，丢失结构。
        if hasattr(result, "to_dict") and callable(result.to_dict):
            result = result.to_dict()

        return {
            "content": [{"type": "text", "text": json.dumps(result, default=str)}],
            "isError": False,
        }

    async def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """处理 resources/read 请求——读取指定资源。"""
        uri = params.get("uri", "")
        rd = self._resources.get(uri)
        if rd is None:
            raise ValueError(f"resource not found: {uri}")

        result = rd.handler()
        if asyncio.iscoroutine(result):
            result = await result

        return {
            "contents": [
                {
                    "uri": uri,
                    "mimeType": rd.mime_type,
                    "text": json.dumps(result, default=str),
                }
            ],
        }

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """处理 notification（生命周期事件）。"""
        # 映射 MCP notification 方法到生命周期事件
        event_map = {
            "notifications/on_load": LifecycleEvent.ON_LOAD,
            "notifications/on_unload": LifecycleEvent.ON_UNLOAD,
            "notifications/on_config_change": LifecycleEvent.ON_CONFIG_CHANGE,
            "notifications/on_pipeline_start": LifecycleEvent.ON_PIPELINE_START,
            "notifications/on_pipeline_end": LifecycleEvent.ON_PIPELINE_END,
            "notifications/on_error": LifecycleEvent.ON_ERROR,
            "notifications/initialized": None,  # 协议通知，忽略
        }

        event = event_map.get(method)
        if event is None:
            return

        handler = self._lifecycle_handlers.get(event.value)
        if handler is not None:
            result = handler(params)
            if asyncio.iscoroutine(result):
                await result

    def _send_response(self, msg_id: str, result: Any) -> None:
        """向 stdout 发送 JSON-RPC 成功响应。"""
        response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    def _send_error(self, msg_id: str, code: int, message: str) -> None:
        """向 stdout 发送 JSON-RPC 错误响应。"""
        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    def stop(self) -> None:
        """停止服务端。"""
        self._running = False


class _DummyLock:
    """无 channel 时的空 lock 兜底（测试场景 / initialize 前）。"""
    async def __aenter__(self) -> None:
        pass
    async def __aexit__(self, *args: Any) -> None:
        pass


_DUMMY_LOCK = _DummyLock()


def _dummy_lock() -> _DummyLock:
    return _DUMMY_LOCK
