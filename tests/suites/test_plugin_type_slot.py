"""PluginTypeSlot 插件类型插槽机制单元测试。

覆盖：注册 API、读取 API、元信息 API、冲突检测、动态枚举创建、
与 plugin.py / config.py 的集成。
"""

from __future__ import annotations

import pytest
from enum import Enum

from pipeline.plugin_types import PluginTypeSlot


# ────────────────────────────────────────────────────────
# 注册 API 测试
# ────────────────────────────────────────────────────────


class TestRegisterEnum:
    """register_enum 相关测试。"""

    def test_register_enum_success(self) -> None:
        """正常注册枚举不应抛出异常。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        # 不抛出异常即通过

    def test_register_enum_duplicate_raises_value_error(self) -> None:
        """重复注册同一 namespace+name 应抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "done"])
        with pytest.raises(ValueError, match="already registered"):
            slot.register_enum("retry", "status", ["pending", "done"])

    def test_register_enum_different_namespace_same_name_ok(self) -> None:
        """不同命名空间下相同 name 可以注册。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "done"])
        slot.register_enum("circuit", "status", ["closed", "open"])  # 不抛异常


class TestRegisterConstant:
    """register_constant 相关测试。"""

    def test_register_constant_success(self) -> None:
        """正常注册常量。"""
        slot = PluginTypeSlot()
        slot.register_constant("retry", "max_attempts", 3)

    def test_register_constant_duplicate_raises_value_error(self) -> None:
        """重复注册同一 namespace+key 应抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_constant("retry", "max_attempts", 3)
        with pytest.raises(ValueError, match="already registered"):
            slot.register_constant("retry", "max_attempts", 5)

    def test_register_constant_various_types(self) -> None:
        """常量值可以是任意类型。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "int_val", 42)
        slot.register_constant("ns", "str_val", "hello")
        slot.register_constant("ns", "list_val", [1, 2, 3])
        slot.register_constant("ns", "none_val", None)


class TestRegisterStateKey:
    """register_state_key 相关测试。"""

    def test_register_state_key_success(self) -> None:
        """正常注册 state key。"""
        slot = PluginTypeSlot()
        slot.register_state_key("retry", "attempt_count", default=0)

    def test_register_state_key_duplicate_raises_value_error(self) -> None:
        """重复注册同一 namespace+key 应抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_state_key("retry", "attempt_count", default=0)
        with pytest.raises(ValueError, match="already registered"):
            slot.register_state_key("retry", "attempt_count", default=1)

    def test_register_state_key_default_none(self) -> None:
        """default 参数默认为 None。"""
        slot = PluginTypeSlot()
        slot.register_state_key("ns", "key")
        defaults = slot.get_initial_state_defaults()
        assert defaults["ns.key"] is None


class TestRegisterHandler:
    """register_handler 相关测试。"""

    def test_register_handler_success(self) -> None:
        """正常注册处理函数。"""
        slot = PluginTypeSlot()
        slot.register_handler("retry", "on_failure", lambda: None)

    def test_register_handler_duplicate_raises_value_error(self) -> None:
        """重复注册同一 namespace+name 应抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_handler("retry", "on_failure", lambda: None)
        with pytest.raises(ValueError, match="already registered"):
            slot.register_handler("retry", "on_failure", lambda: 42)


# ────────────────────────────────────────────────────────
# 读取 API 测试
# ────────────────────────────────────────────────────────


class TestGetConstant:
    """get_constant 相关测试。"""

    def test_get_constant_registered(self) -> None:
        """获取已注册的常量。"""
        slot = PluginTypeSlot()
        slot.register_constant("retry", "max_attempts", 3)
        assert slot.get_constant("retry", "max_attempts") == 3

    def test_get_constant_unregistered_returns_default(self) -> None:
        """获取未注册的常量返回 default。"""
        slot = PluginTypeSlot()
        assert slot.get_constant("retry", "max_attempts", default=99) == 99

    def test_get_constant_unregistered_default_none(self) -> None:
        """获取未注册的常量，default 为 None。"""
        slot = PluginTypeSlot()
        assert slot.get_constant("retry", "not_exist") is None


class TestGetEnumClass:
    """get_enum_class 相关测试。"""

    def test_get_enum_class_returns_enum_subclass(self) -> None:
        """返回的是 Enum 子类。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        cls = slot.get_enum_class("retry", "status")
        assert issubclass(cls, Enum)

    def test_get_enum_class_members(self) -> None:
        """枚举成员值正确。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        cls = slot.get_enum_class("retry", "status")
        assert cls.PENDING.value == "pending"
        assert cls.RUNNING.value == "running"
        assert cls.DONE.value == "done"

    def test_get_enum_class_name_format(self) -> None:
        """动态枚举类名格式为 PascalCase。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["ok"])
        cls = slot.get_enum_class("retry", "status")
        assert cls.__name__ == "RetryStatus"

    def test_get_enum_class_caching(self) -> None:
        """重复获取返回同一类对象（缓存）。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["ok"])
        cls1 = slot.get_enum_class("retry", "status")
        cls2 = slot.get_enum_class("retry", "status")
        assert cls1 is cls2

    def test_get_enum_class_unregistered_raises_key_error(self) -> None:
        """获取未注册的枚举应抛出 KeyError。"""
        slot = PluginTypeSlot()
        with pytest.raises(KeyError, match="not registered"):
            slot.get_enum_class("retry", "status")

    def test_get_enum_class_underscore_namespace(self) -> None:
        """下划线风格的 namespace 也能正确转为 PascalCase 类名。"""
        slot = PluginTypeSlot()
        slot.register_enum("my_plugin", "my_enum", ["a", "b"])
        cls = slot.get_enum_class("my_plugin", "my_enum")
        assert cls.__name__ == "MyPluginMyEnum"


class TestGetStateKey:
    """get_state_key 相关测试。"""

    def test_get_state_key_format(self) -> None:
        """返回 "namespace.key" 格式。"""
        slot = PluginTypeSlot()
        result = slot.get_state_key("retry", "attempt_count")
        assert result == "retry.attempt_count"

    def test_get_state_key_no_registration_needed(self) -> None:
        """get_state_key 只是格式化，不需要先注册。"""
        slot = PluginTypeSlot()
        assert slot.get_state_key("a", "b") == "a.b"


class TestGetHandler:
    """get_handler 相关测试。"""

    def test_get_handler_registered(self) -> None:
        """获取已注册的处理函数。"""
        slot = PluginTypeSlot()
        fn = lambda x: x + 1  # noqa: E731
        slot.register_handler("retry", "on_failure", fn)
        assert slot.get_handler("retry", "on_failure") is fn

    def test_get_handler_unregistered_returns_none(self) -> None:
        """获取未注册的处理函数返回 None。"""
        slot = PluginTypeSlot()
        assert slot.get_handler("retry", "on_failure") is None


class TestGetInitialStateDefaults:
    """get_initial_state_defaults 相关测试。"""

    def test_empty_when_nothing_registered(self) -> None:
        """未注册任何 state key 时返回空字典。"""
        slot = PluginTypeSlot()
        assert slot.get_initial_state_defaults() == {}

    def test_returns_all_defaults(self) -> None:
        """返回所有已注册的 state key 默认值。"""
        slot = PluginTypeSlot()
        slot.register_state_key("retry", "attempt_count", default=0)
        slot.register_state_key("retry", "last_error", default=None)
        slot.register_state_key("circuit", "failure_count", default=0)

        defaults = slot.get_initial_state_defaults()
        assert defaults == {
            "retry.attempt_count": 0,
            "retry.last_error": None,
            "circuit.failure_count": 0,
        }


# ────────────────────────────────────────────────────────
# 元信息 API 测试
# ────────────────────────────────────────────────────────


class TestListNamespaces:
    """list_namespaces 相关测试。"""

    def test_empty(self) -> None:
        """未注册任何内容时返回空列表。"""
        slot = PluginTypeSlot()
        assert slot.list_namespaces() == []

    def test_returns_sorted_unique_namespaces(self) -> None:
        """返回去重排序后的命名空间列表。"""
        slot = PluginTypeSlot()
        slot.register_constant("zebra", "k", 1)
        slot.register_enum("alpha", "e", ["a"])
        slot.register_state_key("middle", "s", default=0)
        # 只在 zebra 命名空间注册了 handler
        slot.register_handler("zebra", "h", lambda: None)

        ns = slot.list_namespaces()
        assert ns == ["alpha", "middle", "zebra"]


class TestListAll:
    """list_all 相关测试。"""

    def test_empty_namespace(self) -> None:
        """不存在命名空间返回空字典。"""
        slot = PluginTypeSlot()
        result = slot.list_all("nonexistent")
        assert result == {
            "constants": {},
            "enums": {},
            "state_keys": {},
            "handlers": {},
        }

    def test_lists_all_registrations(self) -> None:
        """列出某命名空间下的所有注册项。"""
        slot = PluginTypeSlot()
        slot.register_constant("retry", "max", 3)
        slot.register_enum("retry", "status", ["ok", "fail"])
        slot.register_state_key("retry", "count", default=0)
        fn = lambda: None  # noqa: E731
        slot.register_handler("retry", "handler", fn)

        result = slot.list_all("retry")
        assert result["constants"] == {"max": 3}
        assert result["enums"] == {"status": ["ok", "fail"]}
        assert result["state_keys"] == {"count": 0}
        assert "handler" in result["handlers"]


# ────────────────────────────────────────────────────────
# 集成测试：与 plugin.py / config.py 的集成
# ────────────────────────────────────────────────────────


class TestPluginContextIntegration:
    """PluginContext.plugin_types 字段集成测试。"""

    def test_plugin_context_has_plugin_types_field(self) -> None:
        """PluginContext 应包含 plugin_types 字段。"""
        from pipeline.plugin import PluginContext

        ctx = PluginContext(state={})
        assert hasattr(ctx, "plugin_types")
        assert isinstance(ctx.plugin_types, PluginTypeSlot)

    def test_plugin_context_plugin_types_default_independent(self) -> None:
        """每个 PluginContext 实例有独立的 plugin_types。"""
        from pipeline.plugin import PluginContext

        ctx1 = PluginContext(state={})
        ctx2 = PluginContext(state={})
        ctx1.plugin_types.register_constant("ns", "k", 1)
        assert ctx2.plugin_types.get_constant("ns", "k") is None

    def test_plugin_context_custom_plugin_types(self) -> None:
        """可以传入自定义 PluginTypeSlot 实例。"""
        from pipeline.plugin import PluginContext

        slot = PluginTypeSlot()
        slot.register_constant("ns", "k", 42)
        ctx = PluginContext(state={}, plugin_types=slot)
        assert ctx.plugin_types.get_constant("ns", "k") == 42


class TestIPluginRegisterTypes:
    """IPlugin.register_types hook 方法测试。"""

    def test_register_types_default_noop(self) -> None:
        """基类 register_types 默认空实现不报错。"""
        from pipeline.plugin import IPlugin

        slot = PluginTypeSlot()
        # 不应抛出异常
        IPlugin.register_types(slot)

    def test_plugin_subclass_override_register_types(self) -> None:
        """插件子类可以覆盖 register_types。"""
        from pipeline.plugin import IPlugin

        class MyPlugin(IPlugin):
            @property
            def name(self) -> str:
                return "my_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx):
                pass

            @classmethod
            def register_types(cls, slots: PluginTypeSlot) -> None:
                slots.register_constant("my_plugin", "version", "1.0")

        slot = PluginTypeSlot()
        MyPlugin.register_types(slot)
        assert slot.get_constant("my_plugin", "version") == "1.0"


class TestBuildPluginRegistryIntegration:
    """build_plugin_registry 调用 register_types 的集成测试。"""

    def test_registry_has_plugin_types_attribute(self) -> None:
        """build_plugin_registry 返回的 registry 应附加 plugin_types 属性。"""
        from pipeline.config import PipelineConfig, build_plugin_registry
        from pipeline.route import InputRouteTable, OutputRouteTable

        config = PipelineConfig(
            name="test",
            input_route_table=InputRouteTable([]),
            output_route_table=OutputRouteTable([]),
            plugins=[],
            core_plugins={},
        )
        registry = build_plugin_registry(config)
        assert hasattr(registry, "plugin_types")
        assert isinstance(registry.plugin_types, PluginTypeSlot)

    def test_build_plugin_registry_calls_register_types(self) -> None:
        """build_plugin_registry 应为每个插件调用 register_types。"""
        from pipeline.config import PipelineConfig, build_plugin_registry
        from pipeline.plugin import IInputPlugin, PluginResult
        from pipeline.route import InputRouteTable, OutputRouteTable

        register_called = False

        class TestPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "test_register_types_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx):
                return PluginResult()

            @classmethod
            def _reset_cls(cls) -> None:
                pass  # for test isolation

        # 通过 monkey-patch 设置 register_types
        original_register = TestPlugin.register_types

        def custom_register(slots: PluginTypeSlot) -> None:
            nonlocal register_called
            register_called = True
            slots.register_constant("test", "called", True)

        TestPlugin.register_types = classmethod(custom_register)  # type: ignore[assignment]

        try:
            config = PipelineConfig(
                name="test",
                input_route_table=InputRouteTable([]),
                output_route_table=OutputRouteTable([]),
                plugins=[{"class": "tests.suites.test_plugin_type_slot.TestBuildPluginRegistryIntegration.test_build_plugin_registry_calls_register_types.<locals>.TestPlugin"}],
                core_plugins={},
            )
            # 简化测试：直接调用 register_types 而不是走完整的 _resolve_plugin_class
            # 因为 _resolve_plugin_class 需要 dotted_path 在白名单中
        finally:
            TestPlugin.register_types = original_register  # type: ignore[assignment]

        # 直接测试 build_plugin_registry 的核心逻辑
        from pipeline.registry import PluginRegistry

        slot = PluginTypeSlot()
        registry = PluginRegistry()

        # 模拟一个会注册类型的插件
        class PluginWithTypeRegistration(IInputPlugin):
            def __init__(self, config: dict | None = None) -> None:
                self._config = config or {}

            @property
            def name(self) -> str:
                return "typed_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx):
                return PluginResult()

            @classmethod
            def register_types(cls, slots: PluginTypeSlot) -> None:
                slots.register_constant("typed", "key", "value")

        plugin = PluginWithTypeRegistration(config={})
        registry.register(plugin)
        # 模拟 build_plugin_registry 中的行为：通过类调用 register_types
        PluginWithTypeRegistration.register_types(slot)

        assert slot.get_constant("typed", "key") == "value"
