# @ci: python-coverage
"""router_factory 构建链契约测试：key 解析 → model_list/fallbacks → Router 单例。

覆盖契约：
- _parse_provider_keys：keys 列表（多 slot，含 per-key api_base/id/限额覆盖）
  与单 api_key 两形态；非 dict 噪声条目跳过；
- build_model_list：embedding 模型跳过；多 key 每 slot 一个 deployment；
  api_base 优先级 = 模型级 > slot 级 > 缺省删除；单 key 分支凭证来源；
- build_fallbacks：fallback_chain 按 defaults.<type> 主键合并去重；
- build_router：代理环境清理、provider→前缀映射、model→provider/name 映射、
  KeyPool 统计池、Router kwargs（timeout/fallbacks）；
- 单例生命周期：get_or_create_router 复用 + reset_router 全量清空；
- get_litellm_prefix 懒加载：缺失重建一次，仍未命中 fail-closed 抛错。

litellm.Router 与配置加载器是外部依赖，测试以替身注入，不打真实上游。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_router_factory() -> Any:
    """按唯一模块名加载 router_factory（避免与其它插件同名模块互劫持）。

    先弹 key_pool/exceptions 裸名缓存：平铺布局下其他插件目录可能已把同名
    模块装进 sys.modules，顶层 `from key_pool import ...` 须命中本目录版本。
    """
    for _m in ("key_pool", "exceptions"):
        sys.modules.pop(_m, None)
    mod_name = "router_factory_build_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "router_factory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rf() -> Any:
    module = _load_router_factory()
    module.reset_router()
    yield module
    module.reset_router()


class FakeLoader:
    """最小配置加载器替身：_load_llm_data 返回注入的配置。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.calls = 0

    def _load_llm_data(self) -> dict[str, Any]:
        self.calls += 1
        return self._data


def _base_llm_data() -> dict[str, Any]:
    return {
        "defaults": {"call_timeout": 123, "main": "m-main"},
        "providers": {
            "prov_a": {
                "type": "openai",
                "api_base": "https://a.example",
                "keys": [
                    {"id": "a1", "api_key": "sk-a1", "max_concurrent": 3, "rpm": 60, "token_quota": 1000},
                    {"api_key": "sk-a2", "api_base": "https://a2.example"},
                ],
            },
            "prov_b": {"type": "openai", "api_key": "sk-b-default", "api_base": "https://b.example"},
            "prov_empty": {"type": "openai"},
            "bad_shape": "not-a-dict",
        },
        "models": {
            "m-main": {"provider": "prov_a", "model_name": "real-main"},
            "m-b": {"provider": "prov_b", "model_name": "real-b"},
            "m-solo": {"provider": "prov_empty", "model_name": "real-solo"},
            "m-embed": {"provider": "prov_a", "model_name": "real-embed", "dimension": 1024},
            "m-embedding2": {"provider": "prov_a", "model_name": "x-embedding-model"},
        },
    }


@pytest.fixture
def stub_router(rf: Any, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """替身 litellm.Router：捕获 kwargs，构造零成本。"""
    captured: list[dict[str, Any]] = []

    class FakeRouter:
        def __init__(self, **kwargs: Any) -> None:
            captured.append(kwargs)

    monkeypatch.setattr(rf.litellm, "Router", FakeRouter)
    return captured  # type: ignore[return-value]


# ──────────────────────────────────────────────
# _format_key_fingerprint
# ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", "EMPTY"),
        (None, "EMPTY"),
        (123, "EMPTY"),
        ("${ENV_KEY}", "UNRESOLVED:${ENV_KEY}"),
        ("sk-abc1234567890", "sk-abc...(len=16)"),
    ],
)
def test_format_key_fingerprint(rf: Any, raw: Any, expected: str) -> None:
    """指纹契约：空/非串 EMPTY、未展开占位符 UNRESOLVED、正常只露前 6 位。"""
    assert rf._format_key_fingerprint(raw) == expected


# ──────────────────────────────────────────────
# _parse_provider_keys
# ──────────────────────────────────────────────


class TestParseProviderKeys:
    def test_multi_key_slots_with_per_key_overrides(self, rf: Any) -> None:
        slots = rf._parse_provider_keys(_base_llm_data())["prov_a"]
        assert len(slots) == 2
        first, second = slots
        assert (first.key_id, first.api_key) == ("a1", "sk-a1")
        assert first.api_base == "https://a.example"  # 继承 provider 级
        assert first.max_concurrent == 3
        assert first.rpm_limit == 60
        assert first.token_quota == 1000
        assert second.key_id == "prov_a_1"  # 未声明 id → 位置默认名
        assert second.api_base == "https://a2.example"  # per-key 覆盖优先
        assert second.max_concurrent == 2  # 默认并发
        assert second.rpm_limit == 0

    def test_single_api_key_branch(self, rf: Any) -> None:
        slots = rf._parse_provider_keys(_base_llm_data())["prov_b"]
        assert len(slots) == 1
        assert slots[0].key_id == "prov_b_default"
        assert slots[0].api_key == "sk-b-default"
        assert slots[0].api_base == "https://b.example"

    def test_provider_without_credentials_absent(self, rf: Any) -> None:
        parsed = rf._parse_provider_keys(_base_llm_data())
        assert "prov_empty" not in parsed
        assert "bad_shape" not in parsed

    def test_non_list_keys_conf_falls_back_to_api_key(self, rf: Any) -> None:
        data = {"providers": {"p": {"type": "openai", "api_key": "sk-x", "keys": "oops"}}}
        slots = rf._parse_provider_keys(data)["p"]
        assert len(slots) == 1 and slots[0].api_key == "sk-x"

    def test_non_dict_key_entries_skipped(self, rf: Any) -> None:
        data = {"providers": {"p": {"keys": ["junk", {"api_key": "sk-ok"}]}}}
        slots = rf._parse_provider_keys(data)["p"]
        assert len(slots) == 1 and slots[0].api_key == "sk-ok"


# ──────────────────────────────────────────────
# build_model_list
# ──────────────────────────────────────────────


def _pin_prefix_map(rf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """钉死前缀映射并短路懒加载：直接调 build_model_list 的测试不触碰真实 llm.yaml。"""
    monkeypatch.setattr(
        rf,
        "_provider_type_map",
        {"prov_a": "openai", "prov_b": "openai", "prov_empty": "openai", "p2": "openai"},
    )
    monkeypatch.setattr(rf, "_ensure_provider_type_map_loaded", lambda: None)


class TestBuildModelList:
    def test_embedding_models_skipped(self, rf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _pin_prefix_map(rf, monkeypatch)
        model_list = rf.build_model_list(FakeLoader(_base_llm_data()), {})
        ids = [m["model_name"] for m in model_list]
        assert "m-embed" not in ids and "m-embedding2" not in ids
        assert set(ids) == {"m-main", "m-b", "m-solo"}

    def test_multi_key_expands_to_per_slot_deployments(
        self, rf: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_prefix_map(rf, monkeypatch)
        slots = rf._parse_provider_keys(_base_llm_data())["prov_a"]
        model_list = rf.build_model_list(
            FakeLoader({"models": {"m-main": {"provider": "prov_a", "model_name": "real-main"}}}),
            {"prov_a": slots},
        )
        assert len(model_list) == 2  # 每个 slot 一个 deployment
        assert all(m["model_name"] == "m-main" for m in model_list)
        lps = [m["litellm_params"] for m in model_list]
        assert [lp["api_key"] for lp in lps] == ["sk-a1", "sk-a2"]
        assert lps[0]["api_base"] == "https://a.example"  # slot 继承 provider 级
        assert lps[1]["api_base"] == "https://a2.example"  # slot 级覆盖
        assert all(lp["model"] == "openai/real-main" for lp in lps)

    def test_model_level_key_forces_single_deployment(
        self, rf: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """模型级 api_key 存在：不再按 slot 展开，单一 deployment 用模型级凭证。"""
        _pin_prefix_map(rf, monkeypatch)
        slots = rf._parse_provider_keys(_base_llm_data())["prov_a"]
        model_list = rf.build_model_list(
            FakeLoader(
                {
                    "models": {
                        "m-main": {
                            "provider": "prov_a",
                            "model_name": "real-main",
                            "api_key": "sk-model-level",
                            "api_base": "https://model.example",
                        }
                    }
                }
            ),
            {"prov_a": slots},
        )
        assert len(model_list) == 1
        lp = model_list[0]["litellm_params"]
        assert lp["api_key"] == "sk-model-level"
        assert lp["api_base"] == "https://model.example"

    def test_single_slot_fallback_api_base_and_no_del(
        self, rf: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无模型级凭证：单 deployment 回退 slots[0].api_base；无 api_base 时键删除。"""
        _pin_prefix_map(rf, monkeypatch)
        slots = rf._parse_provider_keys(_base_llm_data())["prov_a"][:1]
        model_list = rf.build_model_list(
            FakeLoader({"models": {"m-main": {"provider": "prov_a", "model_name": "real-main"}}}),
            {"prov_a": slots},
        )
        lp = model_list[0]["litellm_params"]
        assert lp["api_base"] == "https://a.example"

        slots_no_base = [rf.KeySlot(key_id="nb", api_key="sk-nb", api_base="")]
        model_list2 = rf.build_model_list(
            FakeLoader({"models": {"m-x": {"provider": "p2", "model_name": "n"}}}),
            {"p2": slots_no_base},
        )
        assert "api_base" not in model_list2[0]["litellm_params"]

    def test_no_slots_no_model_key_yields_bare_params(
        self, rf: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _pin_prefix_map(rf, monkeypatch)
        model_list = rf.build_model_list(
            FakeLoader({"models": {"m-solo": {"provider": "prov_empty", "model_name": "real-solo"}}}),
            {},
        )
        assert model_list[0]["litellm_params"] == {"model": "openai/real-solo"}


# ──────────────────────────────────────────────
# build_fallbacks
# ──────────────────────────────────────────────


class TestBuildFallbacks:
    def test_chain_merged_by_primary_and_deduped(self, rf: Any) -> None:
        data = {
            "defaults": {
                "main": "m-main",
                "fast": "m-b",
                "tiers": {"fallback_chain": {"main": ["m-b", "m-solo", "m-b"], "fast": ["m-solo"]}},
            }
        }
        fallbacks = rf.build_fallbacks(FakeLoader(data))
        assert fallbacks == [{"m-main": ["m-b", "m-solo"]}, {"m-b": ["m-solo"]}]

    def test_entries_without_primary_or_chain_skipped(self, rf: Any) -> None:
        data = {
            "defaults": {
                "main": "",
                "tiers": {"fallback_chain": {"main": ["m-b"], "fast": []}},
            }
        }
        assert rf.build_fallbacks(FakeLoader(data)) == []

    def test_empty_defaults_yield_empty(self, rf: Any) -> None:
        assert rf.build_fallbacks(FakeLoader({})) == []


# ──────────────────────────────────────────────
# build_router 全链 + 单例
# ──────────────────────────────────────────────


class TestBuildRouter:
    def test_build_router_wires_everything(
        self, rf: Any, monkeypatch: pytest.MonkeyPatch, stub_router: list[dict[str, Any]]
    ) -> None:
        for var in ("HTTP_PROXY", "HTTPS_PROXY"):
            monkeypatch.setenv(var, "http://127.0.0.1:9")
        data = _base_llm_data()
        data["defaults"]["tiers"] = {"fallback_chain": {"main": ["m-b"]}}
        loader = FakeLoader(data)

        rf.build_router(loader)

        # 代理环境已清 + trust_env 关闭
        assert "HTTP_PROXY" not in os.environ
        assert "HTTPS_PROXY" not in os.environ
        assert rf.litellm.aiohttp_trust_env is False
        assert rf.litellm.disable_aiohttp_trust_env is True

        # provider→前缀映射
        assert rf._provider_type_map == {
            "prov_a": "openai",
            "prov_b": "openai",
            "prov_empty": "openai",
        }
        # model→provider / model→name 映射
        assert rf.get_provider_for_model("m-main") == "prov_a"
        assert rf.get_provider_for_model("m-unknown") == ""
        assert rf.get_model_name_for_id("m-main") == "real-main"
        assert rf.get_model_name_for_id("m-unknown") == "m-unknown"  # 回退自身
        # KeyPool 统计池（仅声明了 key 的 provider）
        assert set(rf._key_pools) == {"prov_a", "prov_b"}
        assert rf.get_key_pool("prov_a") is not None
        assert rf.get_key_pool("prov_empty") is None

        # Router kwargs：embeddings 被剔除（3 个文本模型 × 各自展开数）
        kwargs = stub_router[0]
        assert kwargs["timeout"] == 123 and kwargs["stream_timeout"] == 123
        assert kwargs["fallbacks"] == [{"m-main": ["m-b"]}]
        names = [m["model_name"] for m in kwargs["model_list"]]
        assert names.count("m-main") == 2  # prov_a 两 slot 展开
        assert names.count("m-b") == 1
        assert names.count("m-solo") == 1
        assert not any("embedding" in n for n in names)

    def test_no_fallbacks_no_kwargs_key(self, rf: Any, stub_router: list[dict[str, Any]]) -> None:
        rf.build_router(FakeLoader({"providers": {}, "models": {}}))
        assert "fallbacks" not in stub_router[0]

    def test_get_or_create_is_singleton_and_reset_clears(
        self, rf: Any, stub_router: list[dict[str, Any]]
    ) -> None:
        loader = FakeLoader(_base_llm_data())
        r1 = rf.get_or_create_router(loader)
        r2 = rf.get_or_create_router(loader)
        assert r1 is r2
        assert loader.calls == 3  # 一次构建读 3 次配置（build_router/model_list/fallbacks），单例命中零重建
        rf.reset_router()
        assert rf._key_pools == {} and rf._provider_type_map == {}
        assert rf._router_instance is None and rf._adapter_instance is None
        r3 = rf.get_or_create_router(loader)
        assert r3 is not r1  # 重置后重建新实例
        assert loader.calls == 6


# ──────────────────────────────────────────────
# build_adapter（KeyPoolAdapter 装配）
# ──────────────────────────────────────────────


class TestBuildAdapter:
    def test_build_adapter_reads_concurrency_and_uses_router_singleton(
        self, rf: Any, stub_router: list[dict[str, Any]]
    ) -> None:
        from adapter import KeyPoolAdapter  # noqa: PLC0415

        data = _base_llm_data()
        data["concurrency"] = {"default_max_concurrent": 7}
        loader = FakeLoader(data)
        adapter = rf.build_adapter(loader)
        assert isinstance(adapter, KeyPoolAdapter)
        assert adapter._default_max_concurrent == 7
        assert adapter._router is rf._router_instance  # 复用 Router 单例
        assert loader.calls >= 2  # 配置至少读两次（router 构建 + 并发段）
        rf.reset_router()
        adapter2 = rf.build_adapter(FakeLoader(data))
        assert isinstance(adapter2, KeyPoolAdapter)


# ──────────────────────────────────────────────
# get_litellm_prefix 懒加载重建
# ──────────────────────────────────────────────


class TestPrefixLazyLoad:
    def test_lazy_load_rebuilds_map_from_config(self, rf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_cfg = types.ModuleType("_config_models")

        class FakeLoader2:
            def _load_llm_data(self) -> dict[str, Any]:
                return {"providers": {"prov_lazy": {"type": "zhipu"}}}

        fake_cfg.get_model_config_loader = lambda: FakeLoader2()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "_config_models", fake_cfg)
        rf._provider_type_map.clear()
        assert rf.get_litellm_prefix("prov_lazy") == "zhipu"

    def test_lazy_load_failure_then_fail_closed(self, rf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_cfg = types.ModuleType("_config_models")

        def boom() -> Any:
            raise RuntimeError("cfg broken")

        fake_cfg.get_model_config_loader = boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "_config_models", fake_cfg)
        rf._provider_type_map.clear()
        with pytest.raises(ValueError, match="前缀映射缺失"):
            rf.get_litellm_prefix("prov_missing")
