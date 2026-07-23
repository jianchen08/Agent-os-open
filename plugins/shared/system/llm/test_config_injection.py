"""LLM 插件配置注入链路测试（task_11 P0）。

验证两条 P0 修复：
- P0-2：`_ModelLoaderShim._load_llm_data` 能从内核实际注入的配置结构
  `{"models": {"llm": {<llm.yaml 内容>}, "embedding": {...}, ...}}` 正确取出
  llm.yaml 的内容（含 providers/models/defaults 顶层键）。
- P0-3：`_config_models` 模块存在且 `set_config` / `get_model_config_loader`
  可正常工作（不再 ModuleNotFoundError）。

测试用真实数据结构（取自 config/models/llm.yaml 的真实顶层键 + config_refs=["models"]
经 filter_config_by_refs 后的形态），不使用 Mock。

[来源: docs/tasks/task_11_plugin_capability_unification.md P0-2/P0-3]
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# 确保插件目录在 sys.path 前面（与 server.py 启动时一致）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


# ────────────────────────────────────────────────────────────
# 真实配置夹具：模拟内核注入给 llm 插件的配置形态。
#
# 链路：config/models/llm.yaml → plugin-loader collect_yaml_configs 递归扫描
#   → {"models": {"llm": <llm.yaml 内容>, "embedding": <embedding.yaml 内容>}}
#   → invoker filter_config_by_refs(config_refs=["models"])
#   → 插件收到 {"models": {"llm": {...}, "embedding": {...}}}
# ────────────────────────────────────────────────────────────

_LLM_YAML_CONTENT = {
    "models": {
        "glm-5.2": {
            "provider": "zhipu_coding",
            "model_name": "glm-5.2",
            "api_base": "https://open.bigmodel.cn/api/coding/paas/v4/",
        },
        "deepseek-v4-flash": {
            "provider": "deepseek",
            "model_name": "deepseek-v4-flash",
        },
    },
    "defaults": {
        "chat": "glm-5.2",
        "tiers": {"fallback_chain": {"chat": ["deepseek-v4-flash"]}},
    },
    "providers": {
        "zhipu_coding": {
            "type": "openai",
            "api_base": "https://open.bigmodel.cn/api/coding/paas/v4/",
            "keys": [{"id": "zhipu_main", "api_key": "sk-PLACEHOLDER"}],
        },
        "deepseek": {
            "type": "openai",
            "api_base": "https://api.deepseek.com",
            "api_key": "sk-ds-PLACEHOLDER",
        },
    },
    "concurrency": {"default_max_concurrent": 2},
}

_EMBEDDING_YAML_CONTENT = {
    "models": {
        "embedding-3": {"provider": "zhipu_coding", "dimension": 2048},
    },
}

# 经 config_refs=["models"] 过滤后，插件实际收到的配置
_INJECTED_CONFIG: dict[str, Any] = {
    "models": {
        "llm": _LLM_YAML_CONTENT,
        "embedding": _EMBEDDING_YAML_CONTENT,
    },
}


class TestModelLoaderShimConfigExtraction:
    """P0-2：_ModelLoaderShim._load_llm_data 必须从 config["models"]["llm"] 取值。"""

    def test_load_llm_data_returns_llm_yaml_content(self) -> None:
        """_load_llm_data 返回 llm.yaml 的完整内容（含 providers/models/defaults）。"""
        from server import _ModelLoaderShim  # noqa: PLC0415

        shim = _ModelLoaderShim(_INJECTED_CONFIG)
        llm_data = shim._load_llm_data()

        # 必须是 llm.yaml 内容，而不是整个注入配置
        assert "providers" in llm_data, "应返回 llm.yaml 内容（含 providers 键）"
        assert "models" in llm_data, "应返回 llm.yaml 内容（含 models 键）"
        assert "glm-5.2" in llm_data["models"], "应含 llm.yaml 中的具体模型"

    def test_load_llm_data_providers_non_empty(self) -> None:
        """P0-2 验收：providers 非空（router 构建成功的前提）。"""
        from server import _ModelLoaderShim  # noqa: PLC0415

        shim = _ModelLoaderShim(_INJECTED_CONFIG)
        llm_data = shim._load_llm_data()

        providers = llm_data.get("providers", {})
        assert providers, "providers 必须非空，否则 router 构建无 deployment"
        assert "zhipu_coding" in providers

    def test_load_llm_data_not_returns_whole_config(self) -> None:
        """回归：不能退回旧行为（取不到 llm 键就返回整个 config）。

        旧实现 config.get("llm", config) 会因为顶层无 "llm" 键而返回整个注入配置，
        导致 llm_data 拿到 {"models": {...}, }，providers 取不到。
        新实现必须精确取 config["models"]["llm"]。
        """
        from server import _ModelLoaderShim  # noqa: PLC0415

        shim = _ModelLoaderShim(_INJECTED_CONFIG)
        llm_data = shim._load_llm_data()

        # 整个注入配置的顶层是 {"models": {...}}，
        # 而 llm.yaml 内容顶层是 {"models": {...}, "providers": {...}, "defaults": {...}}
        # 通过 "providers" 是否在顶层区分二者
        assert "providers" in llm_data, "不能返回整个注入配置（顶层无 providers）"

    def test_load_llm_data_empty_config_returns_empty(self) -> None:
        """空配置优雅降级：返回 {}，不抛异常。"""
        from server import _ModelLoaderShim  # noqa: PLC0415

        shim = _ModelLoaderShim({})
        llm_data = shim._load_llm_data()
        assert llm_data == {}

    def test_load_llm_data_missing_models_key_returns_empty(self) -> None:
        """config 没有 models 键时返回空（不抛 KeyError）。"""
        from server import _ModelLoaderShim  # noqa: PLC0415

        shim = _ModelLoaderShim({"system": {"foo": "bar"}})
        assert shim._load_llm_data() == {}

    def test_load_llm_data_missing_llm_under_models_returns_empty(self) -> None:
        """config["models"] 下没有 llm 子键时返回空。"""
        from server import _ModelLoaderShim  # noqa: PLC0415

        shim = _ModelLoaderShim({"models": {"embedding": _EMBEDDING_YAML_CONTENT}})
        assert shim._load_llm_data() == {}


class TestConfigModelsModule:
    """P0-3：_config_models 模块存在且 set_config/get_model_config_loader 可用。

    server.py:52、router_factory.py:88、adapter.py:1204 都 import 这个模块，
    文件缺失会导致 on_load / 懒加载路径静默 ModuleNotFoundError。
    """

    def test_module_importable(self) -> None:
        """_config_models 模块可导入（不再 ModuleNotFoundError）。"""
        import _config_models  # noqa: F401, PLC0415

    def test_set_config_then_get_model_config_loader(self) -> None:
        """set_config 注入配置后，get_model_config_loader 返回的 loader
        能通过 _load_llm_data 取到 llm.yaml 内容。

        这条链路被 adapter._route_call / router_factory._ensure_provider_type_map_loaded
        使用，必须与 _ModelLoaderShim 行为一致。
        """
        import _config_models  # noqa: PLC0415

        _config_models.set_config(_INJECTED_CONFIG)
        loader = _config_models.get_model_config_loader()
        assert loader is not None

        llm_data = loader._load_llm_data()
        assert "providers" in llm_data, "get_model_config_loader 的 loader 也要能取到 llm.yaml 内容"
        assert llm_data.get("providers"), "providers 非空"

    def test_get_model_config_loader_without_set_returns_empty_data(self) -> None:
        """未 set_config 时 get_model_config_loader 也返回 loader，
        _load_llm_data 返回空（不抛异常）。

        用独立模块实例避免污染其他测试：通过重新加载模块模拟"未 set_config"状态。
        """
        import importlib

        import _config_models  # noqa: PLC0415

        # 重新加载模块，清空模块级状态
        fresh = importlib.reload(_config_models)
        try:
            loader = fresh.get_model_config_loader()
            assert loader is not None
            assert loader._load_llm_data() == {}
        finally:
            # 恢复其他测试依赖的配置
            importlib.reload(_config_models)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
