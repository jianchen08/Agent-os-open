"""依赖注入能力句柄。

内核在 initialize 时注入 5 个能力句柄：
- pipeline-executor: 管道执行能力
- config-reader: 配置读取能力
- tenant-context: 租户上下文
- event-bus: 事件总线
- logger: 日志服务

[来源: docs/tasks/task_08_python_sdk.md AC-07-3]
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
        context: dict[str, Any] | None = None,
    ) -> None:
        self._name = name
        self._call_fn = call_fn
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


# 5 个标准能力句柄名称
STANDARD_CAPABILITIES = [
    "pipeline-executor",
    "config-reader",
    "tenant-context",
    "event-bus",
    "logger",
]
