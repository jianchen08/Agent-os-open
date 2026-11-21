# @feature: FP-T07 llm api | @ci: python-coverage
"""方言路由键正规化回归测试（2026-11-18 两类收敛批 1）。

契约（主方案 §7.1 批 1 / ADR 2026-11-18-llm-two-tier-plugin-convergence：
providers.type = 方言键升格，_plugin_provider_routes 键位与方言表对齐）：
- via=plugin 路由键取 providers.<name>.type 方言键（方言表同源）；
  provider 名不再是合法路由键（缺方言键 fail-closed 报配置错误，
  与 get_provider_prefix 同一纪律，不静默回退 provider 名）
- provider_type 遗留字段在场时优先兼容保留（既有期望行为不删）
- 配置形态行为零变化：无 via=plugin 的配置（37 条 llm.yaml 现状形态）
  路由表逐代确定性重建、直连面表零扰动
- 构建期 fail-closed 异常在整体替换引用之前抛出：上一代完整表原样可读
  （读侧无空窗，审计 2026-11-12 P2-12 同款性质）

≥2 组有区分度输入做性质断言（方言键空间封闭性），非硬编码期望值刷绿。
外部依赖全桩（model_loader），绝不触网。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

# 注意：裸名 sys.modules.pop 在此禁用（与 test_timeout_disposition 同一约定）
# ——router_factory 是 llm 独有名（不在 plugins/conftest 逐出名单），收集期
# pop+重导入会以第二实例顶掉共享槽位，先收集文件（test_plugin_provider_route）
# 的 monkeypatch 目标与 adapter 消费目标分叉（全量车道串扰实证）。本文件直接
# 复用共享实例：build_router 以注入 loader 整表重建，单例状态自含无需新模块。
import router_factory as _rf  # noqa: E402


class _FakeLoader:
    """llm.yaml loader 桩：只回放构造期注入的数据。"""

    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def load_llm_data(self) -> dict[str, Any]:
        return self._d


def _cfg(
    providers: dict[str, dict[str, Any]],
    models: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "providers": providers,
        "models": models
        or {f"m-{name}": {"provider": name, "model_name": f"up-{name}"} for name in providers},
    }


def _build(data: dict[str, Any]) -> None:
    _rf.build_router(model_loader=_FakeLoader(data))


# ─────────────────────────── 键位对齐 ───────────────────────────


def test_route_key_from_dialect_type_when_legacy_field_absent() -> None:
    """via=plugin 且只声明方言键（type）→ 路由键 = type 方言键。

    区分度设计：provider 名与方言键取不同词（corp_gateway vs dsh），
    路由键断言既等于方言键、又显式不等于 provider 名——provider 名回退
    退役的对偶证明（旧实现此形态回退 provider 名，必红）。
    """
    data = _cfg(
        {
            "corp_gateway": {"type": "dsh", "via": "plugin"},
            "plain": {"type": "openai"},
        }
    )
    try:
        _build(data)
        route = _rf.get_plugin_provider_route("m-corp_gateway")
        assert route is not None
        assert route["provider_type"] == "dsh"
        assert route["provider_type"] != "corp_gateway"
        # 非 via 模型不进路由表（直连路径零迁移）
        assert _rf.get_plugin_provider_route("m-plain") is None
    finally:
        _rf.reset_router()


def test_route_key_prefers_legacy_provider_type_field() -> None:
    """via=plugin 且 provider_type 遗留字段在场 → 遗留字段优先（兼容保留）。

    与 test_plugin_provider_route.py::test_plugin_provider_route_from_provider_via
    同形状：type 与 provider_type 并存时期望 provider_type 胜出——既有期望
    行为不删（批 1 键位对齐只改「双缺时回退 provider 名」的尾段）。
    """
    data = _cfg(
        {
            "prov-x": {"type": "openai", "via": "plugin", "provider_type": "sample"},
        }
    )
    try:
        _build(data)
        assert _rf.get_plugin_provider_route("m-prov-x") == {"provider_type": "sample"}
    finally:
        _rf.reset_router()


def test_route_key_falls_back_to_dialect_table_not_provider_name() -> None:
    """provider_type 缺席但方言表有 type → 回退目标=方言表条目。

    区分度：同配置两次构建 + 两组不同 provider/type 组合，路由键均与
    方言表（provider_type_map）逐条同源——键位与方言表对齐的性质证明。
    """
    data = _cfg(
        {
            "alpha": {"type": "anthropic", "via": "plugin"},
            "beta": {"type": "gemini", "via": "plugin"},
        }
    )
    try:
        _build(data)
        dialect_table = dict(_rf._provider_type_map)
        for prov, model_id in (("alpha", "m-alpha"), ("beta", "m-beta")):
            route = _rf.get_plugin_provider_route(model_id)
            assert route is not None
            assert route["provider_type"] == dialect_table[prov]
            assert route["provider_type"] != prov
    finally:
        _rf.reset_router()


def test_via_plugin_without_dialect_key_fail_closed() -> None:
    """via=plugin 且方言键与遗留字段双缺 → 构建期 ValueError（fail-closed）。

    provider 名不是合法路由键；错误在整体替换引用之前抛出，上一代完整表
    原样可读（读侧无空窗性质：失败构建不暴露空表/半代表）。
    """
    good = _cfg({"ok_prov": {"type": "openai"}})
    try:
        _build(good)
        baseline_provider = _rf.get_provider_for_model("m-ok_prov")
        assert baseline_provider == "ok_prov"

        bad = _cfg(
            {
                "nameless": {"via": "plugin"},  # 无 type 无 provider_type
                "ok_prov": {"type": "openai"},
            }
        )
        with pytest.raises(ValueError, match="nameless"):
            _build(bad)

        # 上一代完整表原样：直连面读侧与构建前一致（无空窗）
        assert _rf.get_provider_for_model("m-ok_prov") == baseline_provider
        assert _rf.get_plugin_provider_route("m-nameless") is None
        assert _rf.get_provider_conf("nameless") is None
    finally:
        _rf.reset_router()


# ─────────────────────────── 配置形态行为零变化 ───────────────────────────


def test_config_without_via_builds_deterministic_direct_tables() -> None:
    """无 via=plugin 的配置（37 条 llm.yaml 现状形态）→ 插件路由表恒空，
    直连面表逐代确定性重建（同配置两次构建逐键相等）。"""
    data = _cfg(
        {
            "zhipu_coding": {"type": "zhipu", "api_base": "https://open.bigmodel.cn/api/paas/v4/"},
            "deepseek": {"type": "deepseek", "api_base": "https://api.deepseek.com/v1"},
            "cpa_gateway": {"type": "openai", "api_base": "http://127.0.0.1:8317/v1"},
        }
    )
    try:
        _build(data)
        first = {
            "routes": dict(_rf._plugin_provider_routes),
            "types": dict(_rf._provider_type_map),
            "m2p": dict(_rf._model_to_provider),
            "m2n": dict(_rf._model_to_name),
            "conf": {k: dict(v) for k, v in _rf._provider_conf_map.items()},
        }
        # 同配置重建（另一代 _FakeLoader 实例，模拟配置变更钩子重灌）
        _build(_cfg(data["providers"], dict(data["models"])))
        second = {
            "routes": dict(_rf._plugin_provider_routes),
            "types": dict(_rf._provider_type_map),
            "m2p": dict(_rf._model_to_provider),
            "m2n": dict(_rf._model_to_name),
            "conf": {k: dict(v) for k, v in _rf._provider_conf_map.items()},
        }
        assert first == second
        # 现状形态：无 via 生效条目 → 插件路由零实例（直连路径承载全部模型）
        assert first["routes"] == {}
        assert first["types"] == {
            "zhipu_coding": "zhipu",
            "deepseek": "deepseek",
            "cpa_gateway": "openai",
        }
    finally:
        _rf.reset_router()


def test_mixed_config_route_keys_stay_in_dialect_key_space() -> None:
    """混合配置（直连 + via=plugin 并存）性质断言：插件路由键恒落在方言键
    空间（providers.type 值 ∪ 遗留 provider_type 值），绝不含裸 provider 名。

    ≥2 组区分度输入：两组 provider/type/legacy 组合互不相同，性质逐组成立。
    """
    case_a = _cfg(
        {
            "direct_a": {"type": "openai"},
            "plug_a": {"type": "dsh", "via": "plugin"},
        }
    )
    case_b = _cfg(
        {
            "direct_b": {"type": "deepseek"},
            "plug_b": {"type": "myproto", "via": "plugin", "provider_type": "myplug"},
        }
    )
    for data, expected_keys in (
        (case_a, {"dsh"}),
        (case_b, {"myplug"}),
    ):
        try:
            _build(data)
            routes = dict(_rf._plugin_provider_routes)
            assert routes, "via=plugin 模型应产生路由条目"
            dialect_space = {
                data["providers"][p].get("type", "") for p in data["providers"]
            }
            legacy_space = {
                data["providers"][p].get("provider_type", "") for p in data["providers"]
            } - {""}
            for model_id, route in routes.items():
                assert route["provider_type"] in expected_keys
                assert route["provider_type"] in (dialect_space | legacy_space)
                prov = _rf.get_provider_for_model(model_id)
                assert route["provider_type"] != prov or data["providers"][prov].get(
                    "type"
                ) == route["provider_type"]
        finally:
            _rf.reset_router()
