# @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: python-coverage
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

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# 确保插件目录在 sys.path 前面（与 server.py 启动时一致）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_llm_server() -> Any:
    """按显式路径加载 llm 插件 server 模块。

    不能用裸 `from server import ...`：同一 pytest 进程里其它插件的
    server.py 也会把自身目录插入 sys.path[0]，裸名 `server` 会被劫持到
    错误的插件（如 monitoring/server.py）。显式路径 + 唯一模块名可隔离。
    """
    mod_name = "llm_plugin_server_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None and spec.loader is not None, "cannot load llm plugin server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


# ────────────────────────────────────────────────────────────
# 真实配置夹具：模拟内核注入给 llm 插件的配置形态。
#
# P1 链路（config_files 映射，ADR §4.3 B3 命名空间）：
#   config/models/llm.yaml → config_files[].id="llm" 映射
#   config/models/embedding.yaml → config_files[].id="embedding" 映射
#   → invoker build_injected_config 按 id 命名空间合并
#   → 插件收到 {"llm": <llm.yaml 全文>, "embedding": <embedding.yaml 全文>}
#
# 旧 config_refs=["models"] 路径（{models:{llm,embedding}}）已废弃（llm 试点改用 config_files）。
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

# 经 config_files 映射合并后，插件实际收到的配置（key = config_files[].id）
_INJECTED_CONFIG: dict[str, Any] = {
    "llm": _LLM_YAML_CONTENT,
    "embedding": _EMBEDDING_YAML_CONTENT,
}


class TestModelLoaderShimConfigExtraction:
    """P1：_ModelLoaderShim._load_llm_data 必须从 config["llm"] 取值（config_files 命名空间）。"""

    def test_load_llm_data_returns_llm_yaml_content(self) -> None:
        """_load_llm_data 返回 llm.yaml 的完整内容（含 providers/models/defaults）。"""
        _ModelLoaderShim = _load_llm_server()._ModelLoaderShim

        shim = _ModelLoaderShim(_INJECTED_CONFIG)
        llm_data = shim._load_llm_data()

        # 必须是 llm.yaml 内容，而不是整个注入配置
        assert "providers" in llm_data, "应返回 llm.yaml 内容（含 providers 键）"
        assert "models" in llm_data, "应返回 llm.yaml 内容（含 models 键）"
        assert "glm-5.2" in llm_data["models"], "应含 llm.yaml 中的具体模型"

    def test_load_llm_data_providers_non_empty(self) -> None:
        """P0-2 验收：providers 非空（router 构建成功的前提）。"""
        _ModelLoaderShim = _load_llm_server()._ModelLoaderShim

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
        _ModelLoaderShim = _load_llm_server()._ModelLoaderShim

        shim = _ModelLoaderShim(_INJECTED_CONFIG)
        llm_data = shim._load_llm_data()

        # 整个注入配置的顶层是 {"models": {...}}，
        # 而 llm.yaml 内容顶层是 {"models": {...}, "providers": {...}, "defaults": {...}}
        # 通过 "providers" 是否在顶层区分二者
        assert "providers" in llm_data, "不能返回整个注入配置（顶层无 providers）"

    def test_load_llm_data_empty_config_returns_empty(self) -> None:
        """空配置优雅降级：返回 {}，不抛异常。"""
        _ModelLoaderShim = _load_llm_server()._ModelLoaderShim

        shim = _ModelLoaderShim({})
        llm_data = shim._load_llm_data()
        assert llm_data == {}

    def test_load_llm_data_missing_llm_key_returns_empty(self) -> None:
        """config 没有 llm 键时返回空（不抛 KeyError）。"""
        _ModelLoaderShim = _load_llm_server()._ModelLoaderShim

        shim = _ModelLoaderShim({"system": {"foo": "bar"}})
        assert shim._load_llm_data() == {}

    def test_load_llm_data_llm_not_dict_returns_empty(self) -> None:
        """config["llm"] 不是 dict 时返回空。"""
        _ModelLoaderShim = _load_llm_server()._ModelLoaderShim

        shim = _ModelLoaderShim({"llm": "not-a-dict"})
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


class TestEnsureAdapterUsesResolvedConfig:
    """回归（08-27 回显根因）：_ensure_adapter 必须从已解析副本构建 adapter。

    内核下发的 llm.yaml 含 ``${VAR}`` 占位符，解析只发生在 set_config
    （进程环境 → .env 兜底）。旧实现直接用 ``plugin.get_config()`` 原文
    构建 router/key 池，KeySlot 持字面量 ``${DEEPSEEK_API_KEY}``，上游恒
    401 → LLM 轮次无产出 → 内核把用户消息原文当回复回发（前端回显）。
    """

    def test_ensure_adapter_builds_from_resolved_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """key 占位符在进入 model_loader 前必须已被解析为真实值。"""
        server_mod = _load_llm_server()
        monkeypatch.setenv("AGENTOS_TEST_LLM_KEY", "sk-resolved-real-key")

        raw_config = {
            "llm": {
                "providers": {
                    "deepseek": {
                        "type": "openai",
                        "api_base": "https://api.deepseek.com/v1",
                        "keys": [{"id": "deepseek_main", "api_key": "${AGENTOS_TEST_LLM_KEY}"}],
                    }
                },
                "models": {},
                "defaults": {},
            }
        }
        monkeypatch.setattr(server_mod.plugin, "get_config", lambda: raw_config)
        server_mod._adapter = None

        import router_factory as rf

        captured: dict[str, Any] = {}

        def fake_build_adapter(model_loader: Any) -> str:
            captured["llm_data"] = model_loader._load_llm_data()
            return "adapter-sentinel"

        monkeypatch.setattr(rf, "build_adapter", fake_build_adapter)

        adapter = server_mod._ensure_adapter()

        assert adapter == "adapter-sentinel"
        api_key = captured["llm_data"]["providers"]["deepseek"]["keys"][0]["api_key"]
        assert api_key == "sk-resolved-real-key", (
            "key 未解析即进入 router/key 池（字面量 ${VAR} 会被上游 401）"
        )
        server_mod._adapter = None

    def test_ensure_adapter_mixed_keys_partial_resolution(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第二组有区分度输入：多 key 混合（env 可解 + 示例值不可解）。

        可解 key 解析为真实值；``your-`` 开头的 .env.example 示例值视为
        未配置、保留占位符——两者都必须发生在进入 model_loader 之前。
        """
        server_mod = _load_llm_server()
        monkeypatch.setenv("AGENTOS_TEST_LLM_KEY", "sk-resolved-real-key")

        raw_config = {
            "llm": {
                "providers": {
                    "deepseek": {
                        "type": "openai",
                        "api_base": "https://api.deepseek.com/v1",
                        "keys": [
                            {"id": "deepseek_main", "api_key": "${AGENTOS_TEST_LLM_KEY}"},
                            {"id": "deepseek_demo", "api_key": "your-example-key"},
                        ],
                    }
                },
                "models": {},
                "defaults": {},
            }
        }
        monkeypatch.setattr(server_mod.plugin, "get_config", lambda: raw_config)
        server_mod._adapter = None

        import router_factory as rf

        captured: dict[str, Any] = {}

        def fake_build_adapter(model_loader: Any) -> str:
            captured["llm_data"] = model_loader._load_llm_data()
            return "adapter-sentinel"

        monkeypatch.setattr(rf, "build_adapter", fake_build_adapter)

        server_mod._ensure_adapter()

        keys = captured["llm_data"]["providers"]["deepseek"]["keys"]
        by_id = {k["id"]: k["api_key"] for k in keys}
        assert by_id["deepseek_main"] == "sk-resolved-real-key"
        assert by_id["deepseek_demo"] == "your-example-key", "示例值不经占位符路径，原样保留"
        server_mod._adapter = None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
