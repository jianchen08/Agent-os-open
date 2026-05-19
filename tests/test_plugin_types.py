"""PluginTypeSlot 插件类型插槽机制全面测试。

覆盖场景：
1. 注册/读取 API（enum、constant、state_key、handler）
2. 命名空间隔离（不同 namespace 同名 key 不冲突）
3. 冲突检测（重复注册抛出 ValueError）
4. 动态枚举创建与比较
5. 元信息 API（list_namespaces、list_all）
6. get_initial_state_defaults
7. 与 PluginContext 集成
8. 与 IPlugin.register_types hook 集成
9. 边界与异常场景
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import pytest

from pipeline.plugin_types import PluginTypeSlot


# ────────────────────────────────────────────────────────
# 1. 注册/读取 API 测试
# ────────────────────────────────────────────────────────


class TestRegisterAndReadEnum:
    """register_enum + get_enum_class 端到端测试。"""

    def test_register_then_get_enum_class(self) -> None:
        """注册枚举后能通过 get_enum_class 获取动态 Enum 子类。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        cls = slot.get_enum_class("retry", "status")

        assert issubclass(cls, Enum)
        assert cls.PENDING.value == "pending"
        assert cls.RUNNING.value == "running"
        assert cls.DONE.value == "done"

    def test_enum_members_are_correct(self) -> None:
        """动态枚举的成员名称和值一一对应。"""
        slot = PluginTypeSlot()
        slot.register_enum("task", "priority", ["low", "medium", "high"])
        cls = slot.get_enum_class("task", "priority")

        member_names = [m.name for m in cls]
        assert member_names == ["LOW", "MEDIUM", "HIGH"]

        member_values = [m.value for m in cls]
        assert member_values == ["low", "medium", "high"]

    def test_enum_can_be_compared(self) -> None:
        """动态枚举成员可以正常比较。"""
        slot = PluginTypeSlot()
        slot.register_enum("retry", "status", ["pending", "running", "done"])
        cls = slot.get_enum_class("retry", "status")

        # 同一成员相等
        assert cls.PENDING == cls.PENDING
        # 不同成员不等
        assert cls.PENDING != cls.DONE
        # is 比较
        assert cls.PENDING is cls.PENDING

    def test_enum_class_name_pascal_case(self) -> None:
        """动态枚举类名格式为 {NamespacePascalCase}{NamePascalCase}。"""
        slot = PluginTypeSlot()
        slot.register_enum("my_plugin", "my_enum", ["a", "b"])
        cls = slot.get_enum_class("my_plugin", "my_enum")
        assert cls.__name__ == "MyPluginMyEnum"

    def test_enum_caching(self) -> None:
        """重复 get_enum_class 返回同一类对象（缓存机制）。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "e", ["x"])
        cls1 = slot.get_enum_class("ns", "e")
        cls2 = slot.get_enum_class("ns", "e")
        assert cls1 is cls2

    def test_get_enum_unregistered_raises_key_error(self) -> None:
        """获取未注册的枚举应抛出 KeyError。"""
        slot = PluginTypeSlot()
        with pytest.raises(KeyError, match="not registered"):
            slot.get_enum_class("ns", "nonexistent")


class TestRegisterAndReadConstant:
    """register_constant + get_constant 端到端测试。"""

    def test_register_then_get_constant(self) -> None:
        """注册常量后能正确读取。"""
        slot = PluginTypeSlot()
        slot.register_constant("retry", "max_attempts", 3)
        assert slot.get_constant("retry", "max_attempts") == 3

    def test_constant_various_types(self) -> None:
        """常量值可以是任意类型。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "int_val", 42)
        slot.register_constant("ns", "str_val", "hello")
        slot.register_constant("ns", "list_val", [1, 2, 3])
        slot.register_constant("ns", "dict_val", {"a": 1})
        slot.register_constant("ns", "none_val", None)
        slot.register_constant("ns", "bool_val", True)

        assert slot.get_constant("ns", "int_val") == 42
        assert slot.get_constant("ns", "str_val") == "hello"
        assert slot.get_constant("ns", "list_val") == [1, 2, 3]
        assert slot.get_constant("ns", "dict_val") == {"a": 1}
        assert slot.get_constant("ns", "none_val") is None
        assert slot.get_constant("ns", "bool_val") is True

    def test_get_constant_nonexistent_namespace_returns_default(self) -> None:
        """namespace 不存在时返回 default。"""
        slot = PluginTypeSlot()
        assert slot.get_constant("no_such_ns", "key") is None
        assert slot.get_constant("no_such_ns", "key", default=99) == 99

    def test_get_constant_nonexistent_key_returns_default(self) -> None:
        """namespace 存在但 key 不存在时返回 default。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "existing_key", 1)
        assert slot.get_constant("ns", "missing_key") is None
        assert slot.get_constant("ns", "missing_key", default="fallback") == "fallback"


class TestRegisterAndReadStateKey:
    """register_state_key + get_state_key 端到端测试。"""

    def test_get_state_key_format(self) -> None:
        """注册后的 key 格式为 'namespace.key'。"""
        slot = PluginTypeSlot()
        slot.register_state_key("retry", "attempt_count", default=0)
        assert slot.get_state_key("retry", "attempt_count") == "retry.attempt_count"

    def test_get_state_key_no_registration_needed(self) -> None:
        """get_state_key 只是格式化字符串，不需要先注册。"""
        slot = PluginTypeSlot()
        assert slot.get_state_key("a", "b") == "a.b"
        assert slot.get_state_key("x", "y") == "x.y"

    def test_state_key_default_none(self) -> None:
        """default 参数默认为 None。"""
        slot = PluginTypeSlot()
        slot.register_state_key("ns", "key")
        defaults = slot.get_initial_state_defaults()
        assert defaults["ns.key"] is None


class TestRegisterAndReadHandler:
    """register_handler + get_handler 端到端测试。"""

    def test_register_then_get_handler(self) -> None:
        """注册处理函数后能正确获取。"""
        slot = PluginTypeSlot()
        fn = lambda x: x + 1  # noqa: E731
        slot.register_handler("retry", "on_failure", fn)
        assert slot.get_handler("retry", "on_failure") is fn

    def test_handler_is_callable(self) -> None:
        """获取的 handler 可以正确调用。"""
        slot = PluginTypeSlot()

        def my_handler(x: int, y: int) -> int:
            return x + y

        slot.register_handler("math", "add", my_handler)
        handler = slot.get_handler("math", "add")
        assert handler is not None
        assert handler(3, 4) == 7

    def test_get_handler_nonexistent_returns_none(self) -> None:
        """获取未注册的 handler 返回 None。"""
        slot = PluginTypeSlot()
        assert slot.get_handler("ns", "handler") is None

    def test_get_handler_nonexistent_namespace_returns_none(self) -> None:
        """namespace 不存在时返回 None。"""
        slot = PluginTypeSlot()
        assert slot.get_handler("no_such_ns", "h") is None


# ────────────────────────────────────────────────────────
# 2. 命名空间隔离测试
# ────────────────────────────────────────────────────────


class TestNamespaceIsolation:
    """不同 namespace 的同名 key 不冲突。"""

    def test_same_key_different_namespace_constants(self) -> None:
        """常量：不同 namespace 同名 key 独立。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns_a", "timeout", 30)
        slot.register_constant("ns_b", "timeout", 60)

        assert slot.get_constant("ns_a", "timeout") == 30
        assert slot.get_constant("ns_b", "timeout") == 60

    def test_same_key_different_namespace_enums(self) -> None:
        """枚举：不同 namespace 同名 name 独立。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns_a", "status", ["ok", "fail"])
        slot.register_enum("ns_b", "status", ["open", "closed"])

        cls_a = slot.get_enum_class("ns_a", "status")
        cls_b = slot.get_enum_class("ns_b", "status")

        assert cls_a.OK.value == "ok"
        assert cls_b.OPEN.value == "open"
        assert cls_a is not cls_b

    def test_same_key_different_namespace_state_keys(self) -> None:
        """State key：不同 namespace 同名 key 格式不同。"""
        slot = PluginTypeSlot()
        slot.register_state_key("ns_a", "count", default=0)
        slot.register_state_key("ns_b", "count", default=10)

        assert slot.get_state_key("ns_a", "count") == "ns_a.count"
        assert slot.get_state_key("ns_b", "count") == "ns_b.count"

        defaults = slot.get_initial_state_defaults()
        assert defaults["ns_a.count"] == 0
        assert defaults["ns_b.count"] == 10

    def test_same_key_different_namespace_handlers(self) -> None:
        """Handler：不同 namespace 同名 handler 独立。"""
        slot = PluginTypeSlot()
        fn_a = lambda: "a"  # noqa: E731
        fn_b = lambda: "b"  # noqa: E731
        slot.register_handler("ns_a", "process", fn_a)
        slot.register_handler("ns_b", "process", fn_b)

        assert slot.get_handler("ns_a", "process") is fn_a
        assert slot.get_handler("ns_b", "process") is fn_b


# ────────────────────────────────────────────────────────
# 3. 冲突检测测试
# ────────────────────────────────────────────────────────


class TestConflictDetection:
    """重复注册同 namespace + key 抛出 ValueError。"""

    def test_duplicate_constant_raises(self) -> None:
        """重复注册常量抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "key", 1)
        with pytest.raises(ValueError, match="Constant 'ns.key' already registered"):
            slot.register_constant("ns", "key", 2)

    def test_duplicate_enum_raises(self) -> None:
        """重复注册枚举抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "status", ["a"])
        with pytest.raises(ValueError, match="Enum 'ns.status' already registered"):
            slot.register_enum("ns", "status", ["b"])

    def test_duplicate_state_key_raises(self) -> None:
        """重复注册 state key 抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_state_key("ns", "count", default=0)
        with pytest.raises(ValueError, match="State key 'ns.count' already registered"):
            slot.register_state_key("ns", "count", default=1)

    def test_duplicate_handler_raises(self) -> None:
        """重复注册 handler 抛出 ValueError。"""
        slot = PluginTypeSlot()
        slot.register_handler("ns", "fn", lambda: None)
        with pytest.raises(ValueError, match="Handler 'ns.fn' already registered"):
            slot.register_handler("ns", "fn", lambda: 42)

    def test_same_key_different_type_no_conflict(self) -> None:
        """不同注册类型的同名 key 不冲突。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "name", "value")
        slot.register_state_key("ns", "name", default=0)
        slot.register_handler("ns", "name", lambda: None)
        # 三种类型各注册了一个 "ns.name"，互不冲突


# ────────────────────────────────────────────────────────
# 4. 动态枚举详细测试
# ────────────────────────────────────────────────────────


class TestDynamicEnumDetails:
    """动态枚举的详细行为测试。"""

    def test_enum_member_iteration(self) -> None:
        """可以遍历枚举成员。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "color", ["red", "green", "blue"])
        cls = slot.get_enum_class("ns", "color")

        members = list(cls)
        assert len(members) == 3
        assert members[0].name == "RED"
        assert members[0].value == "red"

    def test_enum_member_access_by_name(self) -> None:
        """可以通过成员名访问枚举值。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "level", ["low", "mid", "high"])
        cls = slot.get_enum_class("ns", "level")

        assert cls["LOW"].value == "low"
        assert cls["HIGH"].value == "high"

    def test_enum_single_value(self) -> None:
        """只有一个值的枚举也能正常工作。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "flag", ["on"])
        cls = slot.get_enum_class("ns", "flag")

        assert len(list(cls)) == 1
        assert cls.ON.value == "on"

    def test_multiple_enums_in_same_namespace(self) -> None:
        """同一 namespace 下可以注册多个不同名的枚举。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "status", ["ok", "fail"])
        slot.register_enum("ns", "priority", ["low", "high"])

        status_cls = slot.get_enum_class("ns", "status")
        priority_cls = slot.get_enum_class("ns", "priority")

        assert status_cls.OK.value == "ok"
        assert priority_cls.LOW.value == "low"
        assert status_cls is not priority_cls


# ────────────────────────────────────────────────────────
# 5. 元信息 API 测试
# ────────────────────────────────────────────────────────


class TestListNamespaces:
    """list_namespaces 测试。"""

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
        slot.register_handler("zebra", "h", lambda: None)

        ns = slot.list_namespaces()
        assert ns == ["alpha", "middle", "zebra"]

    def test_deduplication(self) -> None:
        """同一 namespace 注册多种类型，只出现一次。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "c", 1)
        slot.register_enum("ns", "e", ["a"])
        slot.register_state_key("ns", "s", default=0)
        slot.register_handler("ns", "h", lambda: None)

        assert slot.list_namespaces() == ["ns"]


class TestListAll:
    """list_all 测试。"""

    def test_empty_namespace(self) -> None:
        """不存在的命名空间返回空字典。"""
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

    def test_list_all_partial_registrations(self) -> None:
        """只注册了部分类型的命名空间。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "only_const", 42)

        result = slot.list_all("ns")
        assert result["constants"] == {"only_const": 42}
        assert result["enums"] == {}
        assert result["state_keys"] == {}
        assert result["handlers"] == {}


# ────────────────────────────────────────────────────────
# 6. get_initial_state_defaults 测试
# ────────────────────────────────────────────────────────


class TestGetInitialStateDefaults:
    """get_initial_state_defaults 测试。"""

    def test_empty_when_nothing_registered(self) -> None:
        """未注册任何 state key 时返回空字典。"""
        slot = PluginTypeSlot()
        assert slot.get_initial_state_defaults() == {}

    def test_returns_all_defaults_single_namespace(self) -> None:
        """返回单命名空间下所有 state key 的默认值。"""
        slot = PluginTypeSlot()
        slot.register_state_key("retry", "attempt_count", default=0)
        slot.register_state_key("retry", "last_error", default=None)

        defaults = slot.get_initial_state_defaults()
        assert defaults == {
            "retry.attempt_count": 0,
            "retry.last_error": None,
        }

    def test_returns_all_defaults_multiple_namespaces(self) -> None:
        """返回多命名空间下所有 state key 的默认值。"""
        slot = PluginTypeSlot()
        slot.register_state_key("retry", "attempt_count", default=0)
        slot.register_state_key("circuit", "failure_count", default=0)
        slot.register_state_key("timeout", "elapsed", default=0.0)

        defaults = slot.get_initial_state_defaults()
        assert defaults == {
            "retry.attempt_count": 0,
            "circuit.failure_count": 0,
            "timeout.elapsed": 0.0,
        }

    def test_defaults_with_complex_values(self) -> None:
        """默认值可以是复杂类型。"""
        slot = PluginTypeSlot()
        slot.register_state_key("ns", "list_val", default=[])
        slot.register_state_key("ns", "dict_val", default={})

        defaults = slot.get_initial_state_defaults()
        assert defaults["ns.list_val"] == []
        assert defaults["ns.dict_val"] == {}

    def test_not_affected_by_other_registrations(self) -> None:
        """constants/enums/handlers 注册不影响 get_initial_state_defaults。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "c", 1)
        slot.register_enum("ns", "e", ["a"])
        slot.register_handler("ns", "h", lambda: None)

        assert slot.get_initial_state_defaults() == {}


# ────────────────────────────────────────────────────────
# 7. 与 PluginContext 集成测试
# ────────────────────────────────────────────────────────


class TestPluginContextIntegration:
    """PluginContext.plugin_types 字段集成测试。"""

    def test_plugin_context_default_has_plugin_types(self) -> None:
        """PluginContext 默认包含 plugin_types 字段。"""
        from pipeline.plugin import PluginContext

        ctx = PluginContext(state={})
        assert hasattr(ctx, "plugin_types")
        assert isinstance(ctx.plugin_types, PluginTypeSlot)

    def test_plugin_context_register_and_read_via_ctx(self) -> None:
        """通过 ctx.plugin_types 可以注册和读取类型。"""
        from pipeline.plugin import PluginContext

        ctx = PluginContext(state={})
        ctx.plugin_types.register_constant("ns", "key", 42)
        ctx.plugin_types.register_enum("ns", "status", ["ok", "fail"])

        assert ctx.plugin_types.get_constant("ns", "key") == 42
        cls = ctx.plugin_types.get_enum_class("ns", "status")
        assert cls.OK.value == "ok"

    def test_plugin_context_independent_instances(self) -> None:
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

        assert ctx.plugin_types is slot
        assert ctx.plugin_types.get_constant("ns", "k") == 42


# ────────────────────────────────────────────────────────
# 8. 与 IPlugin.register_types hook 集成测试
# ────────────────────────────────────────────────────────


class TestIPluginRegisterTypesHook:
    """IPlugin.register_types hook 方法测试。"""

    def test_base_class_default_noop(self) -> None:
        """基类 register_types 默认空实现不报错。"""
        from pipeline.plugin import IPlugin

        slot = PluginTypeSlot()
        IPlugin.register_types(slot)  # 不抛异常

    def test_subclass_override_register_types(self) -> None:
        """子类可以覆盖 register_types 注册自定义类型。"""
        from pipeline.plugin import IPlugin

        class MyPlugin(IPlugin):
            @property
            def name(self) -> str:
                return "my_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx: Any) -> Any:
                pass

            @classmethod
            def register_types(cls, slots: PluginTypeSlot) -> None:
                slots.register_constant("my_plugin", "version", "1.0")
                slots.register_enum("my_plugin", "mode", ["fast", "slow"])
                slots.register_state_key("my_plugin", "counter", default=0)
                slots.register_handler("my_plugin", "on_init", lambda: None)

        slot = PluginTypeSlot()
        MyPlugin.register_types(slot)

        assert slot.get_constant("my_plugin", "version") == "1.0"
        assert slot.get_enum_class("my_plugin", "mode").FAST.value == "fast"
        assert slot.get_state_key("my_plugin", "counter") == "my_plugin.counter"
        assert slot.get_handler("my_plugin", "on_init") is not None

    def test_input_plugin_subclass_register_types(self) -> None:
        """IInputPlugin 子类也能使用 register_types。"""
        from pipeline.plugin import IInputPlugin, PluginResult

        class TestInputPlugin(IInputPlugin):
            @property
            def name(self) -> str:
                return "test_input"

            @property
            def priority(self) -> int:
                return 5

            async def execute(self, ctx: Any) -> PluginResult:
                return PluginResult()

            @classmethod
            def register_types(cls, slots: PluginTypeSlot) -> None:
                slots.register_constant("test_input", "enabled", True)

        slot = PluginTypeSlot()
        TestInputPlugin.register_types(slot)
        assert slot.get_constant("test_input", "enabled") is True

    def test_output_plugin_subclass_register_types(self) -> None:
        """IOutputPlugin 子类也能使用 register_types。"""
        from pipeline.plugin import IOutputPlugin, OutputResult

        class TestOutputPlugin(IOutputPlugin):
            @property
            def name(self) -> str:
                return "test_output"

            @property
            def priority(self) -> int:
                return 100

            async def execute(self, ctx: Any) -> OutputResult:
                return OutputResult()

            @classmethod
            def register_types(cls, slots: PluginTypeSlot) -> None:
                slots.register_handler("test_output", "format", lambda x: str(x))

        slot = PluginTypeSlot()
        TestOutputPlugin.register_types(slot)
        handler = slot.get_handler("test_output", "format")
        assert handler is not None
        assert handler(42) == "42"


# ────────────────────────────────────────────────────────
# 9. 与 build_plugin_registry 集成测试
# ────────────────────────────────────────────────────────


class TestBuildPluginRegistryIntegration:
    """build_plugin_registry 与 register_types 的集成测试。"""

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

    def test_empty_config_registry_has_empty_namespaces(self) -> None:
        """空配置下 registry.plugin_types 没有命名空间。"""
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
        assert registry.plugin_types.list_namespaces() == []

    def test_register_types_via_registry_integration(self) -> None:
        """通过模拟插件注册流程，验证 register_types 被正确调用。"""
        from pipeline.plugin import IInputPlugin, PluginResult
        from pipeline.registry import PluginRegistry

        # 模拟一个带 register_types 的插件
        class TypedPlugin(IInputPlugin):
            def __init__(self, config: dict | None = None) -> None:
                self._config = config or {}

            @property
            def name(self) -> str:
                return "typed_plugin"

            @property
            def priority(self) -> int:
                return 10

            async def execute(self, ctx: Any) -> PluginResult:
                return PluginResult()

            @classmethod
            def register_types(cls, slots: PluginTypeSlot) -> None:
                slots.register_constant("typed", "key", "value")
                slots.register_enum("typed", "mode", ["a", "b"])

        # 模拟 build_plugin_registry 的核心流程
        slot = PluginTypeSlot()
        registry = PluginRegistry()
        plugin = TypedPlugin(config={})
        registry.register(plugin)
        TypedPlugin.register_types(slot)

        assert slot.get_constant("typed", "key") == "value"
        assert slot.get_enum_class("typed", "mode").A.value == "a"


# ────────────────────────────────────────────────────────
# 10. 边界与异常场景
# ────────────────────────────────────────────────────────


class TestEdgeCases:
    """边界场景测试。"""

    def test_empty_enum_values(self) -> None:
        """注册空值列表的枚举不报错。"""
        slot = PluginTypeSlot()
        slot.register_enum("ns", "empty", [])
        cls = slot.get_enum_class("ns", "empty")
        assert len(list(cls)) == 0

    def test_constant_with_zero_value(self) -> None:
        """常量值为 0 不是 falsy 误判。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "zero", 0)
        assert slot.get_constant("ns", "zero") == 0
        # 确保不是 default
        assert slot.get_constant("ns", "zero", default=99) == 0

    def test_constant_with_empty_string(self) -> None:
        """常量值为空字符串不是 falsy 误判。"""
        slot = PluginTypeSlot()
        slot.register_constant("ns", "empty_str", "")
        assert slot.get_constant("ns", "empty_str") == ""

    def test_handler_with_none_value(self) -> None:
        """注册 None 作为 handler 值后能获取到。"""
        slot = PluginTypeSlot()
        slot.register_handler("ns", "null_handler", None)  # type: ignore[arg-type]
        assert slot.get_handler("ns", "null_handler") is None
        # 注意：None 和 "不存在" 都是 None，需要通过 list_all 区分
        result = slot.list_all("ns")
        assert "null_handler" in result["handlers"]

    def test_state_key_with_special_characters(self) -> None:
        """state key 可以包含下划线等特殊字符。"""
        slot = PluginTypeSlot()
        slot.register_state_key("my_ns", "my_key_v2", default="test")
        assert slot.get_state_key("my_ns", "my_key_v2") == "my_ns.my_key_v2"

    def test_multiple_registrations_across_all_types(self) -> None:
        """跨所有类型的大量注册。"""
        slot = PluginTypeSlot()

        for i in range(10):
            slot.register_constant("ns", f"c_{i}", i)
            slot.register_enum("ns", f"e_{i}", [f"val_{j}" for j in range(3)])
            slot.register_state_key("ns", f"s_{i}", default=i)
            slot.register_handler("ns", f"h_{i}", lambda x=i: x)

        # 验证所有注册项
        for i in range(10):
            assert slot.get_constant("ns", f"c_{i}") == i
            cls = slot.get_enum_class("ns", f"e_{i}")
            assert cls.VAL_0.value == "val_0"
            assert slot.get_state_key("ns", f"s_{i}") == f"ns.s_{i}"
            handler = slot.get_handler("ns", f"h_{i}")
            assert handler is not None

        # 元信息验证
        assert "ns" in slot.list_namespaces()
        all_items = slot.list_all("ns")
        assert len(all_items["constants"]) == 10
        assert len(all_items["enums"]) == 10
        assert len(all_items["state_keys"]) == 10
        assert len(all_items["handlers"]) == 10

    def test_fresh_slot_is_empty(self) -> None:
        """新创建的 PluginTypeSlot 应该完全为空。"""
        slot = PluginTypeSlot()
        assert slot.list_namespaces() == []
        assert slot.get_initial_state_defaults() == {}
        assert slot.get_constant("any", "key") is None
        assert slot.get_handler("any", "key") is None
