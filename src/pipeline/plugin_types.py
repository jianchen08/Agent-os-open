"""插件类型插槽机制。

提供命名空间隔离的类型注册能力，允许插件在加载时注册自定义枚举、
常量、状态键和处理函数，而不修改核心 types.py。

设计原则：
- 命名空间隔离，不同插件用不同 namespace，不会冲突
- 只做加法，不动核心类型
- 完全向后兼容
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any


class PluginTypeSlot:
    """插件类型插槽，提供命名空间隔离的注册与读取 API。

    每个管道共享一个 PluginTypeSlot 实例，插件通过 register_types
    类方法在加载时注册自定义类型，其他插件通过 ctx.plugin_types 读取。

    Usage::

        slot = PluginTypeSlot()
        # 注册
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        slot.register_constant("retry", "max_attempts", 3)
        slot.register_state_key("retry", "attempt_count", default=0)
        slot.register_handler("retry", "on_failure", my_handler)

        # 读取
        StatusEnum = slot.get_enum_class("retry", "status")
        max_val = slot.get_constant("retry", "max_attempts")
        key = slot.get_state_key("retry", "attempt_count")  # "retry.attempt_count"
        handler = slot.get_handler("retry", "on_failure")
    """

    def __init__(self) -> None:
        self._constants: dict[str, dict[str, Any]] = {}
        self._enums: dict[str, dict[str, list[str]]] = {}
        self._state_keys: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, dict[str, Callable[..., Any]]] = {}
        self._enum_cache: dict[str, type[Enum]] = {}

    # ── 注册 API ──────────────────────────────────────────

    def register_enum(self, namespace: str, name: str, values: list[str]) -> None:
        """注册一个动态枚举类型。

        Args:
            namespace: 命名空间标识
            name: 枚举名称（如 "status"）
            values: 枚举值列表

        Raises:
            ValueError: 同一 namespace + name 已注册时抛出
        """
        if namespace not in self._enums:
            self._enums[namespace] = {}
        if name in self._enums[namespace]:
            raise ValueError(f"Enum '{namespace}.{name}' already registered")
        self._enums[namespace][name] = list(values)

    def register_constant(self, namespace: str, key: str, value: Any) -> None:
        """注册一个常量值。

        Args:
            namespace: 命名空间标识
            key: 常量键名
            value: 常量值

        Raises:
            ValueError: 同一 namespace + key 已注册时抛出
        """
        if namespace not in self._constants:
            self._constants[namespace] = {}
        if key in self._constants[namespace]:
            raise ValueError(f"Constant '{namespace}.{key}' already registered")
        self._constants[namespace][key] = value

    def register_state_key(self, namespace: str, key: str, default: Any = None) -> None:
        """注册一个 state key 及其默认值。

        注册后的 key 格式为 "namespace.key"（如 "retry.attempt_count"）。

        Args:
            namespace: 命名空间标识
            key: 状态键名
            default: 默认值

        Raises:
            ValueError: 同一 namespace + key 已注册时抛出
        """
        if namespace not in self._state_keys:
            self._state_keys[namespace] = {}
        if key in self._state_keys[namespace]:
            raise ValueError(f"State key '{namespace}.{key}' already registered")
        self._state_keys[namespace][key] = default

    def register_handler(self, namespace: str, name: str, handler: Callable[..., Any]) -> None:
        """注册一个处理函数。

        Args:
            namespace: 命名空间标识
            name: 处理函数名称
            handler: 可调用对象

        Raises:
            ValueError: 同一 namespace + name 已注册时抛出
        """
        if namespace not in self._handlers:
            self._handlers[namespace] = {}
        if name in self._handlers[namespace]:
            raise ValueError(f"Handler '{namespace}.{name}' already registered")
        self._handlers[namespace][name] = handler

    # ── 读取 API ──────────────────────────────────────────

    def get_constant(self, namespace: str, key: str, default: Any = None) -> Any:
        """获取常量值。

        Args:
            namespace: 命名空间标识
            key: 常量键名
            default: 未找到时的默认返回值

        Returns:
            常量值，未找到返回 default
        """
        return self._constants.get(namespace, {}).get(key, default)

    def get_enum_class(self, namespace: str, name: str) -> type[Enum]:
        """获取动态生成的枚举类。

        根据 register_enum 注册的信息，动态创建 Enum 子类。
        类名格式：{NamespacePascalCase}{Name}（如 RetryStatus）。

        Args:
            namespace: 命名空间标识
            name: 枚举名称

        Returns:
            动态创建的 Enum 子类

        Raises:
            KeyError: 枚举未注册时抛出
        """
        cache_key = f"{namespace}.{name}"
        if cache_key in self._enum_cache:
            return self._enum_cache[cache_key]

        values = self._enums.get(namespace, {}).get(name)
        if values is None:
            raise KeyError(f"Enum '{cache_key}' not registered")

        # 动态创建 Enum 子类，类名如 RetryStatus
        class_name = f"{namespace.title().replace('_', '')}{name.title().replace('_', '')}"
        members = {v.upper(): v for v in values}
        enum_cls = Enum(class_name, members)  # type: ignore[misc]
        self._enum_cache[cache_key] = enum_cls
        return enum_cls

    def get_state_key(self, namespace: str, key: str) -> str:
        """获取 state key 名。

        Args:
            namespace: 命名空间标识
            key: 状态键名

        Returns:
            格式为 "namespace.key" 的完整键名
        """
        return f"{namespace}.{key}"

    def get_handler(self, namespace: str, name: str) -> Callable[..., Any] | None:
        """获取处理函数。

        Args:
            namespace: 命名空间标识
            name: 处理函数名称

        Returns:
            处理函数，未找到返回 None
        """
        return self._handlers.get(namespace, {}).get(name)

    def get_initial_state_defaults(self) -> dict[str, Any]:
        """获取所有已注册的 state key 默认值。

        Returns:
            字典，键为 "namespace.key" 格式，值为默认值
        """
        defaults: dict[str, Any] = {}
        for namespace, keys in self._state_keys.items():
            for key, default in keys.items():
                defaults[f"{namespace}.{key}"] = default
        return defaults

    # ── 元信息 API ────────────────────────────────────────

    def list_namespaces(self) -> list[str]:
        """列出所有已注册的命名空间。

        Returns:
            命名空间标识列表（去重排序）
        """
        all_ns: set[str] = set()
        all_ns.update(self._constants.keys())
        all_ns.update(self._enums.keys())
        all_ns.update(self._state_keys.keys())
        all_ns.update(self._handlers.keys())
        return sorted(all_ns)

    def list_all(self, namespace: str) -> dict[str, Any]:
        """列出某命名空间下的所有注册项。

        Args:
            namespace: 命名空间标识

        Returns:
            包含 constants、enums、state_keys、handlers 四个键的字典
        """
        return {
            "constants": dict(self._constants.get(namespace, {})),
            "enums": dict(self._enums.get(namespace, {})),
            "state_keys": dict(self._state_keys.get(namespace, {}).items()),
            "handlers": {k: repr(v) for k, v in self._handlers.get(namespace, {}).items()},
        }
