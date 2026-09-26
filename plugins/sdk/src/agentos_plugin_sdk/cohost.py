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
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from mcp import types

from agentos_plugin_sdk._logging import setup_sidecar_logging
from agentos_plugin_sdk.plugin import AgentOSPlugin
from agentos_plugin_sdk.server import KernelChannel, McpServer
from agentos_plugin_sdk.types import ResourceDef, ToolDef

logger = logging.getLogger(__name__)

# 内核 → 宿主的单成员热重载请求（带应答；失败以协议错误应答，内核据此
# 回退 force_unload 整组驱逐语义）。加载新实例由 reload_handler（宿主侧
# 遮蔽纪律）承担，本类只做成员换入与聚合表重建。
RELOAD_MEMBER_METHOD = "agentos/reload_member"

# 内核 → 宿主的单成员卸载请求（带应答；ADR 2026-09-24
# member-granularity-unload）：定向 on_unload → 从成员表摘除 → 三张聚合表
# 原位重聚合 → 模块缓存摘除（unload_handler，宿主侧遮蔽纪律，best-effort）。
# 最后成员拒绝成员级卸载（协议错误）——空成员集破坏合宿进程不变量，内核
# 对最后成员走整组回收（kill 进程）。
UNLOAD_MEMBER_METHOD = "agentos/unload_member"


class CohostServer:
    """多 AgentOSPlugin 实例的单进程聚合 MCP 服务端。

    Args:
        members: plugin_id → 成员插件实例。至少一个；键将作为工具/资源
            命名空间前缀，须互不相同。
        reload_handler: 可选异步加载回调 ``(plugin_id) -> AgentOSPlugin``。
            注入后注册 ``agentos/reload_member`` 请求（成员粒度热重载）；
            回调只负责按遮蔽纪律加载新实例（host.py 职责），换入与聚合表
            重建由本类完成。缺省不注册（独占 server.py 复用 McpServer 形态）。
        unload_handler: 可选摘除回调 ``(plugin_id) -> Any``。注册
            ``agentos/unload_member`` 请求；成员退出服务面后由宿主侧摘除其
            模块缓存（_MemberLoader.drop_member）。异常就地隔离留痕——
            服务面已摘除，缓存残留只影响内存不 Correctness。缺省不摘。

    Raises:
        ValueError: 成员为空，或聚合后出现重名工具/资源（成员 id 含点号
            等导致命名空间互相覆盖）。
    """

    def __init__(
        self,
        members: Mapping[str, AgentOSPlugin],
        *,
        reload_handler: Callable[[str], Any] | None = None,
        unload_handler: Callable[[str], Any] | None = None,
    ) -> None:
        if not members:
            raise ValueError("cohost server requires at least one member plugin")
        self._members: dict[str, AgentOSPlugin] = dict(members)
        self._channel = KernelChannel()
        self._tools = self._aggregate_tools()
        self._resources = self._aggregate_resources()
        self._lifecycle_handlers = self._aggregate_lifecycle_handlers()
        self._initialize_params: dict[str, Any] = {}
        handlers: dict[str, tuple[type, Any]] = {
            UNLOAD_MEMBER_METHOD: (types.RequestParams, self._handle_unload_request)
        }
        if reload_handler is not None:
            handlers[RELOAD_MEMBER_METHOD] = (
                types.RequestParams,
                self._handle_reload_request,
            )
        self._server = McpServer(
            tools=self._tools,
            resources=self._resources,
            lifecycle_handlers=self._lifecycle_handlers,
            on_initialize=self._fan_out_initialize,
            kernel_channel=self._channel,
            request_handlers=handlers,
        )
        self._reload_handler = reload_handler
        self._unload_handler = unload_handler

    @property
    def tool_names(self) -> list[str]:
        """聚合后对外的工具全名（``{plugin_id}.{tool_name}``，排序稳定）。"""
        return sorted(self._tools)

    async def serve(self) -> None:
        """启动聚合 MCP 服务端（stdio transport），阻塞运行至 stdin EOF。"""
        setup_sidecar_logging()
        await self._server.run()

    # ── 成员热交换（成员粒度热重载）──────────────────────

    def swap_member(self, plugin_id: str, plugin: AgentOSPlugin) -> None:
        """成员实例热换入：重聚合工具/资源/生命周期三张表并**原位**生效。

        三张聚合表以同一 dict 对象与 McpServer 共享，clear+update 原位替换
        （非重绑定）——协议面在事件循环下一拍即按新表分发，无需重建连接。
        新成员接入共享 KernelChannel 并重放最近一次 initialize 注入（依赖
        注入语义与首次握手一致）。

        Raises:
            ValueError: plugin_id 非当前成员，或换入后聚合表重名。
        """
        if plugin_id not in self._members:
            raise ValueError(f"swap_member: unknown member: {plugin_id}")
        self._members[plugin_id] = plugin
        # 先按新成员集全量重聚合（重名校验在聚合期抛出，失败不落表）。
        new_tools = self._aggregate_tools()
        new_resources = self._aggregate_resources()
        new_handlers = self._aggregate_lifecycle_handlers()
        self._tools.clear()
        self._tools.update(new_tools)
        self._resources.clear()
        self._resources.update(new_resources)
        self._lifecycle_handlers.clear()
        self._lifecycle_handlers.update(new_handlers)
        # 新成员接线：共享反向调用通道 + initialize 注入重放。
        plugin._kernel_channel = self._channel
        plugin._on_initialize(dict(self._initialize_params))
        logger.info("[cohost] member swapped in: %s (tools=%d)", plugin_id, len(self._tools))

    async def _dispatch_member_unload(self, plugin_id: str) -> None:
        """定向给单个成员派发 on_unload（换入前给旧实例收尾机会）。

        与广播扇出同语义：同步/异步 handler 均接受；异常就地隔离留痕
        （收尾失败不得阻断换入——旧实例随换入被丢弃，由 GC 收尾）。
        """
        old = self._members.get(plugin_id)
        if old is None:
            return
        handler = old._lifecycle_handlers.get("on_unload")
        if handler is None:
            return
        try:
            result = handler({})
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("[cohost] 成员 on_unload 定向派发异常（已隔离）: plugin=%s", plugin_id)

    async def _dispatch_member_load(self, plugin_id: str) -> None:
        """给换入的新实例派发 on_load（与首载同语义：初始化副作用必须重跑）。

        首载路径由内核在 spawn 后发 ``notifications/on_load`` 广播；热换入路径
        不经 spawn，内核侧不发该通知——须在此补齐，否则新实例的初始化状态为空
        （实况：cost_control 换入后 ``_budget_manager`` 恒 None，其 /ext 端点
        全 502 直至整组重建）。异常就地隔离留痕：单个成员初始化失败不得使
        换入本身失败（工具表已生效，失败面局限在该成员自身的初始化状态）。
        """
        plugin = self._members.get(plugin_id)
        if plugin is None:
            return
        handler = plugin._lifecycle_handlers.get("on_load")
        if handler is None:
            return
        try:
            result = handler(dict(self._initialize_params))
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            logger.exception("[cohost] 成员 on_load 定向派发异常（已隔离）: plugin=%s", plugin_id)

    async def _handle_reload_request(self, ctx: Any, _params: Any) -> dict[str, Any]:
        """``agentos/reload_member`` 请求处理：旧成员定向 on_unload → 加载 → 换入。

        plugin_id 从 ctx.params 原始 mapping 读取（校验模型只声明 _meta，
        自定义字段不落模型）。任何失败以异常应答（协议错误），内核据此
        回退 force_unload 整组驱逐——宿主状态未被破坏，回退路径安全。
        """
        raw = dict(ctx.params) if ctx.params else {}
        plugin_id = raw.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            raise ValueError("reload_member: missing plugin_id")
        if plugin_id not in self._members:
            raise ValueError(f"reload_member: unknown member: {plugin_id}")
        loader = self._reload_handler
        if loader is None:
            raise ValueError("reload_member: no reload handler registered")
        await self._dispatch_member_unload(plugin_id)
        plugin = loader(plugin_id)
        if asyncio.iscoroutine(plugin):
            plugin = await plugin
        self.swap_member(plugin_id, plugin)
        # 新实例的 on_load 必须重跑（首载由内核 spawn 后广播，热换入无 spawn）：
        # 缺失会让该成员的初始化状态为空而其请求面照常可达（静默半死）。
        await self._dispatch_member_load(plugin_id)
        return {"reloaded": True, "plugin_id": plugin_id, "tools": len(self._tools)}

    async def _handle_unload_request(self, ctx: Any, _params: Any) -> dict[str, Any]:
        """``agentos/unload_member`` 请求处理：定向 on_unload → 摘出成员表 →
        三张聚合表原位重聚合 → 模块缓存摘除（best-effort）。

        plugin_id 从 ctx.params 原始 mapping 读取（校验模型只声明 _meta，
        自定义字段不落模型）。未知成员/缺 plugin_id/最后成员以异常应答
        （协议错误），内核据此回退整组回收——宿主状态未被破坏。"""
        raw = dict(ctx.params) if ctx.params else {}
        plugin_id = raw.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id:
            raise ValueError("unload_member: missing plugin_id")
        if plugin_id not in self._members:
            raise ValueError(f"unload_member: unknown member: {plugin_id}")
        if len(self._members) <= 1:
            raise ValueError(
                "unload_member: refusing to unload the last member "
                "(kernel must use whole-host unload)"
            )
        await self._dispatch_member_unload(plugin_id)
        del self._members[plugin_id]
        # 子集聚合不可能产生新重名（原表合法的子集），聚合期异常在此不可达。
        new_tools = self._aggregate_tools()
        new_resources = self._aggregate_resources()
        new_handlers = self._aggregate_lifecycle_handlers()
        self._tools.clear()
        self._tools.update(new_tools)
        self._resources.clear()
        self._resources.update(new_resources)
        self._lifecycle_handlers.clear()
        self._lifecycle_handlers.update(new_handlers)
        if self._unload_handler is not None:
            try:
                removed = self._unload_handler(plugin_id)
                if asyncio.iscoroutine(removed):
                    await removed
            except Exception:
                logger.exception(
                    "[cohost] 成员模块缓存摘除异常（已隔离，服务面已摘除）: plugin=%s",
                    plugin_id,
                )
        logger.info(
            "[cohost] member unloaded: %s (tools=%d, members=%d)",
            plugin_id,
            len(self._tools),
            len(self._members),
        )
        return {"unloaded": True, "plugin_id": plugin_id, "tools": len(self._tools)}

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
        """成员生命周期钩子 → 每事件一个扇出 handler（携带成员标识用于隔离留痕）。"""
        handlers_by_event: dict[str, list[tuple[str, Callable[..., Any]]]] = {}
        for plugin_id, plugin in self._members.items():
            for event, handler in plugin._lifecycle_handlers.items():
                handlers_by_event.setdefault(event, []).append((plugin_id, handler))
        return {event: self._make_fan_out_handler(handlers) for event, handlers in handlers_by_event.items()}

    def _make_fan_out_handler(self, handlers: list[tuple[str, Callable[..., Any]]]) -> Callable[..., Any]:
        """构造单事件扇出：同步/异步 handler 按成员注册顺序执行。

        生命周期通知是广播语义：单成员 handler 异常就地隔离留痕（含成员
        标识与堆栈）并继续，不中断其余成员的投递——任一成员的生命周期
        装配失败不得使排在它之后的成员失去 on_load 接线。
        """

        async def _fan_out(params: dict[str, Any]) -> None:
            for plugin_id, handler in handlers:
                try:
                    result = handler(dict(params))
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception(
                        "[cohost] 成员生命周期 handler 异常（已隔离，不影响其余成员）: plugin=%s",
                        plugin_id,
                    )

        return _fan_out

    def _fan_out_initialize(self, params: dict[str, Any]) -> None:
        """initialize 握手扇出：全部成员共享本服务端的 KernelChannel 并各自注入。"""
        # 留存最近一次握手参数：成员热换入（swap_member）时按同参重放注入。
        self._initialize_params = dict(params)
        for plugin in self._members.values():
            # 预置共享通道：成员 _on_initialize 懒建 KernelChannel 的分支因此
            # 复用共享实例，成员的反向调用走本服务端唯一 stdio 连接。
            plugin._kernel_channel = self._channel
            plugin._on_initialize(params)
