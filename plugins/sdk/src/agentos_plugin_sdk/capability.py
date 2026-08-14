"""依赖注入能力句柄。

内核在 initialize 时注入 6 个能力句柄：
- pipeline-executor: 管道执行能力
- config-reader: 配置读取能力
- tenant-context: 租户上下文
- event-bus: 事件总线
- logger: 日志服务
- metrics: 指标上报（record_metric，监控设计 §三 通道2）

[来源: docs/tasks/task_08_python_sdk.md AC-07-3]
[来源: docs/working/重要设计/插件监控与指标机制设计.md §三 通道2]
"""

from __future__ import annotations

from typing import Any


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

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        """调用内核能力。

        Args:
            method: 要调用的方法名。
            params: 方法参数。

        Returns:
            内核返回的结果。

        Raises:
            RuntimeError: 如果句柄未连接到内核。
        """
        if self._call_fn is None:
            raise RuntimeError(f"capability '{self._name}' is not connected to kernel")
        return await self._call_fn(method, params)

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


# 标准能力句柄名称（与内核 STANDARD_CAPABILITIES 对齐）
STANDARD_CAPABILITIES = [
    "pipeline-executor",
    "config-reader",
    "tenant-context",
    "event-bus",
    "logger",
    "metrics",
    "tool-executor",
    "service-registry",
    "frontend",
]


class FrontendEmitter:
    """frontend.emit capability 的高层封装（ADR §3.5，task_observability 前置）。

    「插件 → 内核 → 前端」的一次性事件推送出口：emit(event, payload) 经
    capability notify（fire-and-forget JSON-RPC notification）发往内核，
    内核路由到 session.emit_event 推前端 WS（事件信封与 tool_start 等
    现有前端事件一致：{type, data, sequence}）。

    与 event-bus.emit 的分工：event-bus 承载流式 chunk（llm_core 逐字推送），
    frontend.emit 承载低频观测/进度事件（cost_update / tool_progress /
    termination_status）。

    推送失败（通道关闭、内核未实现等）静默降级——可观测性出口绝不阻断
    插件主流程。
    """

    def __init__(self, handle: CapabilityHandle | None) -> None:
        self._handle = handle

    @classmethod
    def from_plugin(cls, plugin: Any) -> "FrontendEmitter | None":
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
            event: 事件名（如 cost_update / tool_progress / termination_status）。
            payload: 事件数据。须携带前端路由键（thread_id/pipeline_id，
                工具类事件另需 message_id/call_id），缺失会被内核丢弃。
        """
        if self._handle is None:
            return
        try:
            await self._handle.notify("emit", {"event": event, "payload": payload})
        except Exception:
            # 通道异常静默：观测推送失败不影响插件主流程
            pass

