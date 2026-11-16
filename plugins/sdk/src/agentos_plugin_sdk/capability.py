"""依赖注入能力句柄。

内核在 initialize 时注入 7 个能力句柄：
- pipeline-executor: 管道执行能力
- tenant-context: 租户上下文
- event-bus: 事件总线
- metrics: 指标上报（record_metric，监控设计 §三 通道2）
- tool-executor: 工具执行
- service-registry: 服务注册表
- frontend: 前端事件出口

[来源: docs/tasks/task_08_python_sdk.md AC-07-3]
[来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable
from typing import Any, Protocol

# ── 反向调用身份透传（刀1，ADR docs/working/2026-10-15-reverse-call-tenant-
#    passthrough-adr-draft.md）──────────────────────────────────────────────
#
# 内核调用插件时经 invocation `_identity` 下发执行上下文身份（tenant_id/
# session_id，原头=内核，插件不可指派）；server 在调用边界捕获到本 contextvar
# （asyncio 每消息独立 task，天然 per-invocation 隔离，等价内核侧 task_local
# 语义），capability 反调出站时（plugin._make_call_fn）自动回带 `_tenant_id`/
# `_session_id`。内核桥入口（PluginScopedRouter）消费两键并建立租户 scope。
_identity_var: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "agentos_reverse_call_identity", default=None
)


def bind_reverse_call_identity(
    identity: Any,
) -> contextvars.Token[dict[str, str] | None]:
    """绑定本次调用的执行上下文身份（server 调用边界调用）。

    `identity` 须为 ``{"tenant_id": ..., "session_id": ...}`` dict；非 dict
    （旧内核/畸形）按无身份处理（返回None 形态 token 亦可安全 reset）。
    """
    value = identity if isinstance(identity, dict) and identity else None
    return _identity_var.set(value)


def reset_reverse_call_identity(
    token: contextvars.Token[dict[str, str] | None],
) -> None:
    """调用边界结束时复位身份（防跨调用残留）。"""
    _identity_var.reset(token)


def reverse_call_identity_params() -> dict[str, str]:
    """反调出站参数附加：有身份 → ``{"_tenant_id", "_session_id", "_user_id"}``；无 → 空。

    `_user_id` 为刀2 增量（ADR docs/decisions/2026-10-17-reverse-call-user-
    identity-injection.md）：内核 `_identity.user_id` 回显，桥消费后派发面
    以注入身份为准（插件手写 user 不可指派）。非字符串/空串按缺席处理，
    与旧内核（不认识该键，当普通 params 剥除/忽略）双向兼容。
    """
    identity = _identity_var.get()
    if not identity:
        return {}
    params: dict[str, str] = {}
    tenant = identity.get("tenant_id")
    if isinstance(tenant, str) and tenant:
        params["_tenant_id"] = tenant
    session = identity.get("session_id")
    if isinstance(session, str) and session:
        params["_session_id"] = session
    user = identity.get("user_id")
    if isinstance(user, str) and user:
        params["_user_id"] = user
    return params


def current_reverse_call_tenant_id() -> str | None:
    """直调面消费：当前调用身份中的租户 ID；无身份/畸形 → None。

    进程内直调（shared 平铺模块 import，非 capability 反调）不走
    :func:`reverse_call_identity_params` 的出站回带，取租户方在本进程内
    读同一 contextvar（原头仍是内核 `_identity` 下发，非插件可指派）。
    调用方按「显式形参 > 本函数 > default」取序。
    """
    identity = _identity_var.get()
    if not identity:
        return None
    tenant = identity.get("tenant_id")
    return tenant if isinstance(tenant, str) and tenant else None


class CapabilityHandle:
    """能力句柄——插件通过此对象调用内核提供的能力。

    内核在 initialize 握手时注入能力信息，插件通过 get_capability 获取句柄。

    Attributes:
        name: 能力名称（如 pipeline-executor）
        call_fn: 内核注入的调用函数（向内核发送 JSON-RPC 请求）
        context: 内核注入的上下文数据
    """

    def __init__(
        self,
        name: str,
        call_fn: Any | None = None,
        notify_fn: Any | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._call_fn = call_fn
        self._notify_fn = notify_fn
        self._context = context or {}

    @property
    def name(self) -> str:
        """能力名称。"""
        return self._name

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        """调用内核能力。

        Args:
            method: 要调用的方法名。
            params: 方法参数。
            timeout: 等待响应超时（秒）；None 用 SDK 默认（30s）。
                长等待方法（如 human-interaction.wait_for_choice）必须显式传大值。

        Returns:
            内核返回的结果。

        Raises:
            RuntimeError: 如果句柄未连接到内核。
        """
        if self._call_fn is None:
            raise RuntimeError(f"capability '{self._name}' is not connected to kernel")
        return await self._call_fn(method, params, timeout)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        """向内核发送 fire-and-forget 通知（不等响应）。

        用于流式 chunk 推送：每生成一个 chunk 就 notify 一次，内核收到后
        直接推前端。不等响应避免每个 chunk 阻塞（send_request 不可用于高频流式）。

        Args:
            method: 要调用的方法名。
            params: 方法参数。

        Raises:
            RuntimeError: 如果句柄未连接到内核。
        """
        if self._notify_fn is None:
            raise RuntimeError(f"capability '{self._name}' notify not connected to kernel")
        await self._notify_fn(method, params)

    def get(self, key: str) -> Any:
        """读取上下文信息。

        Args:
            key: 上下文键名。

        Returns:
            对应的值，不存在返回 None。
        """
        return self._context.get(key)

    def has(self, key: str) -> bool:
        """检查上下文中是否存在指定键。"""
        return key in self._context

    def keys(self) -> list[str]:
        """返回所有上下文键。"""
        return list(self._context.keys())


# 标准能力句柄名称（与内核 STANDARD_CAPABILITIES 对齐）。
# 已删除的死能力（两端同步）：`logger`（内核从未实现任何 method）、
# `config-reader`（内核 handler 已删，插件配置改走 manifest config_files
# + /api/v1/plugins/{id}/config 配置面）。
STANDARD_CAPABILITIES = [
    "pipeline-executor",
    "pipeline-state",
    "tenant-context",
    "event-bus",
    "metrics",
    "tool-executor",
    "tool-surface",
    "service-registry",
    "frontend",
]


# capability_caller 约定：接收**完整** wire method（如 ``tool-executor.invoke``）
# 或短方法名均可（见 bind_capability_caller 的前缀剥离）；timeout 可选，
# 传 None = SDK 默认 30s。
class CapabilityCaller(Protocol):
    """capability_caller 协议：``await caller(method, params[, timeout])``。"""

    def __call__(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float | None = ...,
    ) -> Awaitable[Any]: ...


def bind_capability_caller(handle: CapabilityHandle, cap_name: str) -> CapabilityCaller:
    """绑定能力句柄与命名空间，构造 async caller ``(method, params, timeout=None) -> Any``。

    caller 剥掉已含的能力前缀后转交 ``handle.call``，避免句柄再拼成双命名空间
    （如 ``tool-executor.tool-executor.invoke``）；未带前缀的方法原样透传。

    闭包通过函数参数绑定 cap_name，规避 B023（循环变量绑定）。

    Args:
        handle: CapabilityHandle 实例（其 call 会拼接 ``f"{cap}.{method}"``）
        cap_name: 能力命名空间（如 "tool-executor"）

    Returns:
        async caller；timeout 透传 :meth:`CapabilityHandle.call`
        （None = SDK 默认 30s，长等待语义的调用方须显式传大值）。
    """
    prefix = f"{cap_name}."

    async def _call(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        stripped = method[len(prefix) :] if method.startswith(prefix) else method
        return await handle.call(stripped, params, timeout)

    return _call


class FrontendEmitter:
    """frontend.emit capability 的高层封装（ADR §3.5，task_observability 前置）。

    「插件 → 内核 → 前端」的一次性事件推送出口：emit(event, payload) 经
    capability notify（fire-and-forget JSON-RPC notification）发往内核，
    内核路由到 session.emit_event 推前端 WS（事件信封与 tool_start 等
    现有前端事件一致：{type, data, sequence}）。

    与 event-bus.emit 的分工：event-bus 承载流式 chunk（llm_core 逐字推送），
    frontend.emit 承载低频观测/进度事件（cost_update / tool_progress）。

    推送失败（通道关闭、内核未实现等）静默降级——可观测性出口绝不阻断
    插件主流程。
    """

    def __init__(self, handle: CapabilityHandle | None) -> None:
        self._handle = handle

    @classmethod
    def from_plugin(cls, plugin: Any) -> FrontendEmitter | None:
        """从 AgentOSPlugin 实例解析 frontend capability。

        内核未声明 frontend（旧内核）时 get_capability 抛 KeyError，
        返回 None 由调用方优雅降级（不推送）。
        """
        try:
            return cls(plugin.get_capability("frontend"))
        except Exception:
            return None

    @property
    def available(self) -> bool:
        """frontend capability 是否可用。"""
        return self._handle is not None

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        """推送一次性事件到前端（fire-and-forget，异常静默）。

        Args:
            event: 事件名（如 cost_update / tool_progress）。
            payload: 事件数据。须携带前端路由键（thread_id/pipeline_id，
                工具类事件另需 message_id/call_id），缺失会被内核丢弃。
        """
        if self._handle is None:
            return
        try:
            await self._handle.notify("emit", {"event": event, "payload": payload})
        except Exception:  # noqa: S110 —— 观测推送失败不影响插件主流程（有注释说明意图）
            pass


class MetricsReporter:
    """metrics capability 桥接——插件上报业务指标到内核聚合器。

    与 FrontendEmitter 同构：句柄缺失（旧内核未注入 metrics）时优雅降级，
    上报失败不阻断插件主流程。指标由内核聚合器统一承接，供 /metrics
    （Prometheus）与 metrics-admin 查询面消费（监控设计 §三 通道2）。
    """

    def __init__(self, handle: CapabilityHandle | None) -> None:
        self._handle = handle

    @classmethod
    def from_plugin(cls, plugin: Any) -> MetricsReporter | None:
        """从 AgentOSPlugin 实例解析 metrics capability。

        内核未声明 metrics（旧内核）时 get_capability 抛 KeyError，
        返回 None 由调用方优雅降级（不上报）。
        """
        try:
            return cls(plugin.get_capability("metrics"))
        except Exception:
            return None

    @property
    def available(self) -> bool:
        """metrics capability 是否可用。"""
        return self._handle is not None

    async def record(
        self,
        name: str,
        value: float,
        metric_type: str = "counter",
        labels: dict[str, str] | None = None,
        unit: str | None = None,
        help_text: str | None = None,
    ) -> None:
        """上报一个指标到内核聚合器（fire-and-forget，异常静默）。

        Args:
            name: 指标短名（如 "tokens_used"）。
            value: 指标值。
            metric_type: "counter"（单调累加）/ "gauge"（覆盖）。
            labels: 维度标签（如 {"model": "deepseek"}）。
            unit: 单位（可选，如 "tokens"）。
            help_text: HELP 文本（可选，Prometheus 导出用）。
        """
        if self._handle is None:
            return
        try:
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
            await self._handle.call("record", params)
        except Exception:  # noqa: S110 —— 观测上报失败不影响插件主流程（有注释说明意图）
            pass
