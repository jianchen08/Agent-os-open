"""apply_agent_model_override 函数单元测试。

验证 Router 模式下切换模型时 _provider / _api_base / _context_window
是否正确同步更新，以及直连模式行为不受影响。

Bug 背景:
    Router 模式下切换模型时，原来只更新了 _model 和 _context_window，
    没有更新 _provider 和 _api_base，导致后续 normalize_messages_for_provider
    使用了错误的 provider 参数。修复后通过 get_llm_core_config 获取完整配置
    并同步更新所有字段。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pipeline.plugin_resolver import apply_agent_model_override


# ---------------------------------------------------------------------------
# 辅助工具：构建 mock 对象
# ---------------------------------------------------------------------------


def _make_llm_call(
    *,
    use_router: bool = True,
    model: str = "glm-5.1",
    provider: str = "zhipu_coding",
    api_base: str = "https://api.zhipu.cn/v1",
    context_window: int = 128000,
) -> MagicMock:
    """构建一个模拟的 LLMCore 插件实例。

    Args:
        use_router: 是否使用 Router 模式
        model: 当前模型标识
        provider: 当前提供商
        api_base: 当前 API 基础地址
        context_window: 当前上下文窗口大小

    Returns:
        配置好属性的 MagicMock 对象
    """
    llm = MagicMock(spec=[])
    llm._use_router = use_router
    llm._model = model
    llm._provider = provider
    llm._api_base = api_base
    llm._context_window = context_window
    return llm


def _make_plugin_registry(llm_call: MagicMock) -> MagicMock:
    """构建一个模拟的 PluginRegistry，get_core("llm_call") 返回指定实例。

    Args:
        llm_call: 要注册为 llm_call 核心插件的 mock 实例

    Returns:
        配置好 get_core 方法的 MagicMock 对象
    """
    registry = MagicMock(spec=[])
    registry.get_core = MagicMock(return_value=llm_call)
    return registry


def _make_agent_config(model_name: str) -> SimpleNamespace:
    """构建一个模拟的 Agent 配置对象。

    Args:
        model_name: 要切换到的目标模型标识

    Returns:
        包含 model_name 属性的 SimpleNamespace 对象
    """
    return SimpleNamespace(model_name=model_name)


def _make_model_loader(configs: dict[str, dict[str, Any]]) -> MagicMock:
    """构建一个模拟的 ModelConfigLoader。

    Args:
        configs: 模型标识到配置字典的映射，例如：
            {
                "minimax-m2.7": {
                    "provider": "minimax",
                    "api_base": "https://api.minimax.chat/v1",
                    "context_window": 256000,
                }
            }

    Returns:
        get_llm_core_config 根据模型标识返回对应配置的 MagicMock
    """
    loader = MagicMock(spec=[])
    loader.get_llm_core_config = MagicMock(
        side_effect=lambda model_id: configs.get(model_id)
    )
    return loader


# ---------------------------------------------------------------------------
# 测试类
# ---------------------------------------------------------------------------


class TestRouterModeProviderUpdate:
    """Router 模式 - provider 正确更新。"""

    def test_provider_updates_on_model_switch(self) -> None:
        """从 glm-5.1(zhipu_coding) 切换到 minimax-m2.7(minimax) 时，
        _provider 应从 zhipu_coding 变为 minimax。"""
        llm = _make_llm_call(
            model="glm-5.1",
            provider="zhipu_coding",
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        assert llm._provider == "minimax"


class TestRouterModeApiBaseUpdate:
    """Router 模式 - api_base 正确更新。"""

    def test_api_base_updates_on_model_switch(self) -> None:
        """切换模型后 _api_base 应更新为新模型的 api_base。"""
        llm = _make_llm_call(
            model="glm-5.1",
            provider="zhipu_coding",
            api_base="https://api.zhipu.cn/v1",
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        assert llm._api_base == "https://api.minimax.chat/v1"


class TestRouterModeContextWindowUpdate:
    """Router 模式 - context_window 正确更新。"""

    def test_context_window_updates_on_model_switch(self) -> None:
        """切换后 _context_window 应更新为新模型的值。"""
        llm = _make_llm_call(
            model="glm-5.1",
            context_window=128000,
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        assert llm._context_window == 256000


class TestRouterModeFallbackWithoutLoader:
    """Router 模式 - model_loader 不可用时的降级处理。"""

    @patch("pipeline.plugin_resolver.apply_agent_model_override", wraps=apply_agent_model_override)
    def test_provider_keeps_original_when_no_loader(self, _mock_wrap: MagicMock) -> None:
        """services 中没有 model_loader 且 import 也失败时，
        _provider / _api_base 应保持原值不变（降级处理）。

        通过 patch config.models.get_model_config_loader 使其抛异常，
        模拟 import 失败场景。
        """
        llm = _make_llm_call(
            model="glm-5.1",
            provider="zhipu_coding",
            api_base="https://api.zhipu.cn/v1",
            context_window=128000,
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("minimax-m2.7")
        # services 中没有 model_loader，且 import 也会失败
        services: dict[str, Any] = {}

        with patch.dict("sys.modules", {"config.models": None}):
            # 让 from config.models import get_model_config_loader 失败
            with patch("builtins.__import__", side_effect=self._block_config_models_import):
                apply_agent_model_override(registry, agent_config, services)

        # 降级：_provider 和 _api_base 保持原值
        assert llm._provider == "zhipu_coding"
        assert llm._api_base == "https://api.zhipu.cn/v1"
        # _model 仍然会被更新（不受 loader 影响）
        assert llm._model == "minimax-m2.7"

    @staticmethod
    def _block_config_models_import(
        name: str, *args: Any, **kwargs: Any
    ) -> Any:
        """自定义 __import__，让 config.models 导入失败。

        Args:
            name: 模块名
            args: 其他位置参数
            kwargs: 其他关键字参数

        Returns:
            正常导入结果

        Raises:
            ImportError: 当模块名为 config.models 时
        """
        if name == "config.models":
            raise ImportError("config.models not available")
        return __import__(name, *args, **kwargs)


class TestDirectModeUnaffected:
    """直连模式 - 行为不受影响。"""

    def test_direct_mode_does_not_modify_provider_directly(self) -> None:
        """直连模式（_use_router=False）不会直接修改 llm_call 的 _provider
        等属性，而是通过重建插件的方式处理。"""
        llm = _make_llm_call(
            use_router=False,
            model="glm-5.1",
            provider="zhipu_coding",
            api_base="https://api.zhipu.cn/v1",
            context_window=128000,
        )
        # 直连模式需要 _config 和 __class__ 来支持重建
        llm._config = {
            "provider": "zhipu_coding",
            "model_name": "glm-5.1",
            "api_base": "https://api.zhipu.cn/v1",
        }
        llm.name = "LLMCorePlugin"
        # 模拟 __class__ 的调用（重建插件）
        new_llm = MagicMock()
        new_llm.name = "LLMCorePlugin"
        type(llm).__call__ = lambda *a, **kw: new_llm

        registry = _make_plugin_registry(llm)
        # 直连模式会修改 _core_plugins 和 _plugins
        registry._core_plugins = {"llm_call": llm}
        registry._plugins = {"LLMCorePlugin": llm}

        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
                "model_name": "MiniMax-M2.7",
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        # 直连模式下原 llm_call 的属性不会被直接修改
        assert llm._provider == "zhipu_coding"
        assert llm._api_base == "https://api.zhipu.cn/v1"
        # 直连模式不修改 _model（Router 模式才会）
        assert llm._model == "glm-5.1"

    def test_direct_mode_rebuilds_plugin(self) -> None:
        """直连模式应通过重建插件（type(llm_call)(config=...)）来切换模型，
        而不是直接修改属性。验证 registry._plugins 被更新。"""

        # 使用可控行为的真实类：每次 FakeLLMCore() 都返回同一实例
        # 这样 type(llm_call)(config=...) 也能返回预设的 rebuilt_llm
        class FakeLLMCore:
            """模拟 LLMCore 类，_return_on_create 控制构造行为。"""
            _return_on_create: Any = None

            def __new__(cls, *args: Any, **kwargs: Any) -> Any:
                """如果有预设实例则返回，否则创建新实例。"""
                if cls._return_on_create is not None:
                    return cls._return_on_create
                return super().__new__(cls)

            def __init__(self, **kwargs: Any) -> None:
                """初始化（仅在真正创建新实例时调用）。"""
                pass

        # 先用 object.__new__ 创建 rebuilt_llm（不触发 __new__ 逻辑）
        rebuilt_llm = object.__new__(FakeLLMCore)
        rebuilt_llm._use_router = False  # type: ignore[attr-defined]
        rebuilt_llm._model = "MiniMax-M2.7"  # type: ignore[attr-defined]
        rebuilt_llm._provider = "minimax"  # type: ignore[attr-defined]
        rebuilt_llm._api_base = "https://api.minimax.chat/v1"  # type: ignore[attr-defined]
        rebuilt_llm._context_window = 256000  # type: ignore[attr-defined]
        rebuilt_llm.name = "LLMCorePlugin"  # type: ignore[attr-defined]

        # 让 FakeLLMCore() 返回 rebuilt_llm
        FakeLLMCore._return_on_create = rebuilt_llm
        original_llm = FakeLLMCore()
        assert original_llm is rebuilt_llm  # 确认同一对象

        # 设置 original_llm 的初始属性（实际也在 rebuilt_llm 上）
        original_llm._use_router = False  # type: ignore[attr-defined]
        original_llm._model = "glm-5.1"  # type: ignore[attr-defined]
        original_llm._provider = "zhipu_coding"  # type: ignore[attr-defined]
        original_llm._api_base = "https://api.zhipu.cn/v1"  # type: ignore[attr-defined]
        original_llm._context_window = 128000  # type: ignore[attr-defined]
        original_llm._config = {  # type: ignore[attr-defined]
            "provider": "zhipu_coding",
            "model_name": "glm-5.1",
            "api_base": "https://api.zhipu.cn/v1",
        }
        original_llm.name = "LLMCorePlugin"  # type: ignore[attr-defined]

        # 使用真实类替代 MagicMock，避免属性拦截问题
        class FakeRegistry:
            """模拟 PluginRegistry，使用真实字典存储。"""
            def __init__(self, core: Any) -> None:
                self._core_plugins: dict[str, Any] = {}
                self._plugins: dict[str, Any] = {}
                self._core = core

            def get_core(self, core_type: str) -> Any:
                """返回预设的核心插件。"""
                return self._core

        registry = FakeRegistry(original_llm)
        registry._core_plugins = {"llm_call": original_llm}
        registry._plugins = {"LLMCorePlugin": original_llm}

        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
                "model_name": "MiniMax-M2.7",
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        # 直连模式应通过 registry 更新插件引用
        # type(original_llm) 是 FakeLLMCore，FakeLLMCore(config=...) 返回 rebuilt_llm
        assert registry._core_plugins["llm_call"] is rebuilt_llm
        assert registry._plugins["LLMCorePlugin"] is rebuilt_llm


class TestRouterModeModelUpdate:
    """Router 模式 - _model 字段更新（补充验证）。"""

    def test_model_updates_on_switch(self) -> None:
        """Router 模式下 _model 应更新为目标模型标识。"""
        llm = _make_llm_call(model="glm-5.1")
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        assert llm._model == "minimax-m2.7"


class TestRouterModeAllFieldsUpdate:
    """Router 模式 - 所有字段同步更新（集成验证）。"""

    def test_all_fields_update_together(self) -> None:
        """切换模型时 _model / _provider / _api_base / _context_window
        应全部同步更新为新模型配置。"""
        llm = _make_llm_call(
            model="glm-5.1",
            provider="zhipu_coding",
            api_base="https://api.zhipu.cn/v1",
            context_window=128000,
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("minimax-m2.7")
        loader = _make_model_loader({
            "minimax-m2.7": {
                "provider": "minimax",
                "api_base": "https://api.minimax.chat/v1",
                "context_window": 256000,
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        assert llm._model == "minimax-m2.7"
        assert llm._provider == "minimax"
        assert llm._api_base == "https://api.minimax.chat/v1"
        assert llm._context_window == 256000


class TestRouterModeApiBaseFallback:
    """Router 模式 - api_base 为 None 时的回退逻辑。"""

    def test_api_base_keeps_original_when_new_is_none(self) -> None:
        """新模型配置中 api_base 为 None/空时，_api_base 应保持原值
        （通过 `or` 回退逻辑）。"""
        llm = _make_llm_call(
            model="glm-5.1",
            provider="zhipu_coding",
            api_base="https://api.zhipu.cn/v1",
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("some-model")
        loader = _make_model_loader({
            "some-model": {
                "provider": "new_provider",
                "api_base": "",  # 空字符串，触发 or 回退
                "context_window": 64000,
            },
        })
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        # api_base 为空字符串，通过 `or` 回退到原值
        assert llm._api_base == "https://api.zhipu.cn/v1"
        assert llm._provider == "new_provider"


class TestEdgeCases:
    """边界情况测试。"""

    def test_no_agent_config(self) -> None:
        """agent_config 为 None 时不应抛出异常。"""
        registry = MagicMock()
        apply_agent_model_override(registry, None, {})
        # 函数在第一个 if 就返回，get_core 不应被调用
        registry.get_core.assert_not_called()

    def test_no_model_name(self) -> None:
        """agent_config 没有 model_name 属性时不应抛出异常。"""
        registry = MagicMock()
        agent_config = SimpleNamespace()  # 没有 model_name
        apply_agent_model_override(registry, agent_config, {})
        registry.get_core.assert_not_called()

    def test_empty_model_name(self) -> None:
        """model_name 为空字符串时应提前返回。"""
        registry = MagicMock()
        agent_config = SimpleNamespace(model_name="")
        apply_agent_model_override(registry, agent_config, {})
        registry.get_core.assert_not_called()

    def test_no_llm_call_plugin(self) -> None:
        """registry.get_core("llm_call") 返回 None 时应提前返回。"""
        registry = MagicMock(spec=[])
        registry.get_core = MagicMock(return_value=None)
        agent_config = SimpleNamespace(model_name="minimax-m2.7")
        apply_agent_model_override(registry, agent_config, {})
        # 不应抛出异常

    def test_model_config_not_found(self) -> None:
        """get_llm_core_config 返回 None 时，_provider 等保持原值。"""
        llm = _make_llm_call(
            model="glm-5.1",
            provider="zhipu_coding",
            api_base="https://api.zhipu.cn/v1",
            context_window=128000,
        )
        registry = _make_plugin_registry(llm)
        agent_config = _make_agent_config("unknown-model")
        # loader 对 unknown-model 返回 None
        loader = _make_model_loader({})
        services: dict[str, Any] = {"model_loader": loader}

        apply_agent_model_override(registry, agent_config, services)

        # _model 已被更新（在 loader 查询之前）
        assert llm._model == "unknown-model"
        # _provider 等保持原值（因为 get_llm_core_config 返回 None）
        assert llm._provider == "zhipu_coding"
        assert llm._api_base == "https://api.zhipu.cn/v1"
        assert llm._context_window == 128000
