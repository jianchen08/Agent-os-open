# @feature: FP-T07 llm api | @vision: V3 可嵌入 | @ci: python-coverage
"""router_factory 前缀映射 fail-closed 契约测试（兜底反模式审查 P8）。

P8 契约：provider → litellm 前缀映射不静默回退——
- 未在 llm.yaml providers.<name>.type 配置的 provider 显式抛配置错误
  （provider 名本身不是合法 litellm 前缀，静默回退会把配置错误推迟成
  上游 API 的隐蔽 404/路由失败）；
- 懒加载失败要留痕（warning），随后仍 fail-closed 抛错；
- 空 provider 是调用方约定的"无前缀直连"，返回空串。
"""
from __future__ import annotations

import importlib.util
import logging
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

    先弹 key_pool 裸名缓存：平铺布局下其他插件目录可能已把同名模块
    装进 sys.modules，顶层 `from key_pool import ...` 须命中本目录版本。
    """
    for _m in ("key_pool",):
        sys.modules.pop(_m, None)
    mod_name = "router_factory_p8_test"
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
    return _load_router_factory()


def _with_map(rf: Any, monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """替换模块级前缀表（monkeypatch 负责恢复），并短路懒加载。"""
    monkeypatch.setattr(rf, "_provider_type_map", dict(mapping))
    monkeypatch.setattr(rf, "_ensure_provider_type_map_loaded", lambda: None)


@pytest.mark.parametrize(
    ("provider", "prefix"),
    [("apigo", "openai"), ("zhipu_coding", "zai")],
)
def test_hit_returns_mapping_without_warning(
    rf: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider: str, prefix: str
) -> None:
    """已配置 provider → 精确返回映射值，无回退告警。"""
    _with_map(rf, monkeypatch, {"apigo": "openai", "zhipu_coding": "zai"})
    with caplog.at_level(logging.WARNING):
        assert rf.get_litellm_prefix(provider) == prefix
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_every_map_member_resolves_without_raising(
    rf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """性质断言：映射表内任意成员查询恒等于其配置值（成员 ⇔ 不抛错）。"""
    mapping = {"apigo": "openai", "zhipu_coding": "zai", "deepseek": "deepseek"}
    _with_map(rf, monkeypatch, mapping)
    for provider, prefix in mapping.items():
        assert rf.get_litellm_prefix(provider) == prefix


@pytest.mark.parametrize("provider", ["apigo", "custom_proxy"])
def test_miss_raises_config_error(
    rf: Any, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """未配置 provider → ValueError 配置错误（不拿 provider 名充当前缀）。"""
    _with_map(rf, monkeypatch, {})
    with pytest.raises(ValueError, match="前缀映射缺失") as excinfo:
        rf.get_litellm_prefix(provider)
    assert provider in str(excinfo.value)


def test_empty_provider_returns_empty_prefix(rf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """空 provider 是调用方约定的"无前缀直连"，返回空串（无斜杠前缀）。"""
    _with_map(rf, monkeypatch, {})
    assert rf.get_litellm_prefix("") == ""


def test_loader_failure_warns_then_raises(
    rf: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """懒加载失败 → warning 留痕，随后仍 fail-closed 抛配置错误。"""
    monkeypatch.setattr(rf, "_provider_type_map", {})
    monkeypatch.setitem(sys.modules, "_config_models", None)  # from ... import → ImportError
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ValueError, match="前缀映射缺失"):
            rf.get_litellm_prefix("apigo")
    assert any("懒加载" in r.getMessage() for r in caplog.records)


def test_lazy_load_recovers_mapping(rf: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """懒加载成功 → 表被重建，未命中查询恢复命中。"""
    monkeypatch.setattr(rf, "_provider_type_map", {})

    class _FakeLoader:
        def _load_llm_data(self) -> dict[str, Any]:
            return {"providers": {"apigo": {"type": "openai"}}}

    fake_cfg = types.ModuleType("_config_models_fake")
    fake_cfg.get_model_config_loader = lambda: _FakeLoader()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_config_models", fake_cfg)

    assert rf.get_litellm_prefix("apigo") == "openai"
    assert rf._provider_type_map == {"apigo": "openai"}
