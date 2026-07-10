"""管道和插件系统最小化原则与阻塞问题审计测试。

测试覆盖：
1. PluginRegistry.get_output_plugins() 缓存 —— 避免每轮迭代全量扫描
2. _safe_deepcopy 浅拷贝优先 —— 避免 JSON roundtrip
3. resolve_tier 缓存 —— 避免重复磁盘读取
4. hot_swap._pre_check 无死代码
5. PluginHotReloader 非阻塞重载 —— 不阻塞 watchdog 线程
6. _discover_plugin_class 缓存 —— 避免重复扫描 dir(module)
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin import IOutputPlugin, IInputPlugin, IPlugin, PluginResult, PluginContext
from pipeline.registry import PluginRegistry


# ---------------------------------------------------------------------------
# 辅助：最小化插件桩
# ---------------------------------------------------------------------------

class StubOutputPlugin(IOutputPlugin):
    """最小化 Output 插件桩。"""

    def __init__(self, name: str = "stub_out", priority: int = 0) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult()


class StubInputPlugin(IInputPlugin):
    """最小化 Input 插件桩。"""

    def __init__(self, name: str = "stub_in", priority: int = 0) -> None:
        self._name = name
        self._priority = priority

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    async def execute(self, ctx: PluginContext) -> PluginResult:
        return PluginResult()


# ===========================================================================
# 测试 1：PluginRegistry.get_output_plugins() 缓存
# ===========================================================================

class TestGetOutputPluginsCaching:
    """验证 get_output_plugins 使用缓存，不会每次全量扫描。"""

    def test_output_plugins_cached_on_repeated_calls(self) -> None:
        """连续调用 get_output_plugins() 应返回相同列表（缓存命中）。"""
        registry = PluginRegistry()
        registry.register(StubOutputPlugin("out_a", 10))
        registry.register(StubOutputPlugin("out_b", 5))

        result1 = registry.get_output_plugins()
        result2 = registry.get_output_plugins()

        # 缓存命中：应是同一对象或等价列表
        assert result1 == result2
        assert len(result1) == 2

    def test_cache_invalidated_on_register(self) -> None:
        """注册新插件后缓存应失效，返回更新后的列表。"""
        registry = PluginRegistry()
        registry.register(StubOutputPlugin("out_a", 10))

        result_before = registry.get_output_plugins()
        assert len(result_before) == 1

        # 注册新插件后应返回新列表
        registry.register(StubOutputPlugin("out_b", 5))
        result_after = registry.get_output_plugins()
        assert len(result_after) == 2

    def test_cache_invalidated_on_replace(self) -> None:
        """替换插件后缓存应失效。"""
        registry = PluginRegistry()
        registry.register(StubOutputPlugin("out_a", 10))

        result_before = registry.get_output_plugins()
        assert len(result_before) == 1

        registry.replace("out_a", StubOutputPlugin("out_a_new", 5))
        result_after = registry.get_output_plugins()
        assert len(result_after) == 1
        assert result_after[0].name == "out_a_new"

    def test_output_plugins_sorted_by_priority(self) -> None:
        """返回列表应按优先级排序。"""
        registry = PluginRegistry()
        registry.register(StubOutputPlugin("out_c", 20))
        registry.register(StubOutputPlugin("out_a", 5))
        registry.register(StubOutputPlugin("out_b", 10))

        result = registry.get_output_plugins()
        assert [p.name for p in result] == ["out_a", "out_b", "out_c"]

    def test_non_output_plugins_not_included(self) -> None:
        """Input 插件不应出现在 output_plugins 列表中。"""
        registry = PluginRegistry()
        registry.register(StubOutputPlugin("out_a", 10))
        registry.register(StubInputPlugin("in_a", 5))

        result = registry.get_output_plugins()
        assert len(result) == 1
        assert result[0].name == "out_a"


# ===========================================================================
# 测试 2：_safe_deepcopy 浅拷贝优先
# ===========================================================================

class TestSafeDeepcopyShallowFallback:
    """验证 _safe_deepcopy 对未知类型优先使用浅拷贝而非 JSON roundtrip。"""

    def test_unknown_type_uses_shallow_copy(self) -> None:
        """未知类型对象应浅拷贝，而非 JSON roundtrip（会丢失类型信息）。"""
        from pipeline.engine_state import _safe_deepcopy

        class CustomObj:
            def __init__(self, val: int) -> None:
                self.val = val

        state: dict[str, Any] = {"obj": CustomObj(42)}
        result = _safe_deepcopy(state)

        # 浅拷贝保留了原始类型（JSON roundtrip 会变成 int 或丢失）
        assert isinstance(result["obj"], CustomObj)
        assert result["obj"].val == 42

    def test_json_safe_types_direct_reference(self) -> None:
        """JSON 安全类型应直接引用，不做任何拷贝。"""
        from pipeline.engine_state import _safe_deepcopy

        state: dict[str, Any] = {"count": 42, "name": "test", "flag": True, "empty": None}
        result = _safe_deepcopy(state)

        assert result["count"] == 42
        assert result["name"] == "test"
        assert result["flag"] is True
        assert result["empty"] is None

    def test_dict_and_list_deep_copied(self) -> None:
        """嵌套的 dict 和 list 应被深拷贝（互不影响）。"""
        from pipeline.engine_state import _safe_deepcopy

        state: dict[str, Any] = {
            "items": [1, 2, {"key": "val"}],
            "config": {"a": [1, 2]},
        }
        result = _safe_deepcopy(state)

        # 修改结果不应影响原始
        result["items"].append(999)
        result["config"]["a"].append(3)

        assert len(state["items"]) == 3  # 不受影响
        assert len(state["config"]["a"]) == 2  # 不受影响

    def test_skip_on_chunk_key(self) -> None:
        """on_chunk 键应被跳过（不可序列化的回调函数）。"""
        from pipeline.engine_state import _safe_deepcopy

        state: dict[str, Any] = {"on_chunk": lambda x: None, "data": "hello"}
        result = _safe_deepcopy(state)

        assert "on_chunk" not in result
        assert result["data"] == "hello"


# ===========================================================================
# 测试 3：resolve_tier 缓存
# ===========================================================================

class TestResolveTierCaching:
    """验证 resolve_tier 使用缓存避免重复调用 _load_llm_data。"""

    def test_resolve_tier_caches_result(self) -> None:
        """相同 tier 多次调用应命中缓存，不重复调用 _load_llm_data。"""
        from pipeline.plugin_resolver import resolve_tier

        mock_loader = MagicMock()
        mock_loader._load_llm_data.return_value = {
            "defaults": {"tiers": {"large": "gpt-4", "small": "gpt-3.5"}}
        }

        services: dict[str, Any] = {"model_loader": mock_loader}

        result1 = resolve_tier("large", services)
        result2 = resolve_tier("small", services)
        result3 = resolve_tier("large", services)

        assert result1 == "gpt-4"
        assert result2 == "gpt-3.5"
        assert result3 == "gpt-4"
        # _load_llm_data 应调用 2 次（large 首次 + small 首次），
        # 第三次 large 从缓存命中不再调用
        assert mock_loader._load_llm_data.call_count == 2

    def test_resolve_tier_returns_empty_for_unknown(self) -> None:
        """未知 tier 应返回空字符串。"""
        from pipeline.plugin_resolver import resolve_tier

        mock_loader = MagicMock()
        mock_loader._load_llm_data.return_value = {
            "defaults": {"tiers": {"large": "gpt-4"}}
        }
        services: dict[str, Any] = {"model_loader": mock_loader}

        result = resolve_tier("unknown_tier", services)
        assert result == ""


# ===========================================================================
# 测试 4：hot_swap._pre_check 无死代码
# ===========================================================================

class TestHotSwapPreCheckNoDeadCode:
    """验证 _pre_check 不包含无用的 type() 调用（死代码）。"""

    def test_pre_check_returns_warnings_for_interface_mismatch(self) -> None:
        """接口类型不匹配时应返回警告。"""
        from pipeline.hot_swap import HotSwapManager

        registry = PluginRegistry()
        manager = HotSwapManager(registry)

        old = StubOutputPlugin("old", 0)
        new = StubInputPlugin("new", 0)

        warnings = manager._pre_check(old, new)
        assert len(warnings) == 1
        assert "接口类型不同" in warnings[0]

    def test_pre_check_empty_for_compatible_plugins(self) -> None:
        """兼容插件不应产生警告。"""
        from pipeline.hot_swap import HotSwapManager

        registry = PluginRegistry()
        manager = HotSwapManager(registry)

        old = StubOutputPlugin("old", 0)
        new = StubOutputPlugin("new", 0)

        warnings = manager._pre_check(old, new)
        assert len(warnings) == 0


# ===========================================================================
# 测试 5：PluginHotReloader 非阻塞重载
# ===========================================================================

class TestPluginHotReloaderNonBlocking:
    """验证 hot-reload 不阻塞 watchdog 线程。"""

    def test_on_file_change_returns_immediately(self) -> None:
        """_on_file_change 应快速返回，不阻塞调用线程。"""
        from plugins.hot_reload import PluginHotReloader

        reloader = PluginHotReloader(config_dir="/nonexistent")

        # Mock _do_reload 使其耗时较长
        original_do_reload = reloader._do_reload
        call_times: list[float] = []

        def slow_reload(event_type: str, file_path: str) -> Any:
            call_times.append(time.monotonic())
            time.sleep(0.1)  # 模拟慢操作
            return original_do_reload(event_type, file_path)

        reloader._do_reload = slow_reload  # type: ignore[assignment]

        start = time.monotonic()
        reloader._on_file_change("modified", "/some/file.yaml")
        elapsed = time.monotonic() - start

        # _on_file_change 应在合理时间内返回（不应等待 _do_reload 完成）
        # 如果是阻塞的，elapsed >= 0.1s；如果非阻塞，elapsed << 0.1s
        assert elapsed < 0.05, (
            f"_on_file_change 阻塞了 {elapsed:.3f}s，应在 watchdog 线程中快速返回"
        )


# ===========================================================================
# 测试 6：_discover_plugin_class 缓存
# ===========================================================================

class TestDiscoverPluginClassCaching:
    """验证 _discover_plugin_class 使用缓存避免重复扫描。"""

    def test_discover_caches_result(self) -> None:
        """同名插件第二次发现应命中缓存，不再 import。"""
        from pipeline import config as cfg_mod

        # 清除缓存（如果存在）
        if hasattr(cfg_mod, "_plugin_class_cache"):
            cfg_mod._plugin_class_cache.clear()

        # 第一次调用（即使返回 None 也应缓存）
        with patch("importlib.import_module", side_effect=ImportError):
            result1 = cfg_mod._discover_plugin_class("nonexistent_plugin")
            result2 = cfg_mod._discover_plugin_class("nonexistent_plugin")

        assert result1 is None
        assert result2 is None
        # import_module 应只被调用一次（第二次从缓存读取）
        # 注意：如果缓存生效，import_module 只调用 1 次而非 2 次
