# @feature: FP-0.2.CFG 配置系统与插件配置注入 | @ci: python-coverage
"""model.resolve 单源解析（模型落定键地基）。

契约：``resolve_model_id`` 按统一优先级解析最终模型 id——
显式 model_id > defaults.tiers[model_tier] > defaults.chat；
全空（未表达模型意图）返回空串。llm_core 选模与服务处理器共用本函数
（单源零漂移）；manifest capabilities.services 与 server 处理器同名对账。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))


@pytest.fixture
def mod() -> Any:
    """按显式路径加载 _config_models（裸名防劫持）。"""
    spec = importlib.util.spec_from_file_location(
        "llm_config_models_resolve_test", str(_DIR / "_config_models.py")
    )
    assert spec is not None
    assert spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_config_models_resolve_test"] = m
    spec.loader.exec_module(m)
    return m


_LLM_CONF: dict[str, Any] = {
    "defaults": {
        "chat": "chat-default",
        "tiers": {"large": "tier-large", "small": "tier-small"},
    },
}


class TestResolveModelId:
    def test_explicit_id_wins_over_tier_and_chat(self, mod: Any) -> None:
        mod.set_config({"llm": _LLM_CONF})

        assert mod.resolve_model_id("explicit-id", "large") == "explicit-id"

    def test_no_explicit_id_resolves_tier_via_tiers_mapping(self, mod: Any) -> None:
        mod.set_config({"llm": _LLM_CONF})

        assert mod.resolve_model_id("", "large") == "tier-large"

    @pytest.mark.parametrize("tier", ["medium", "unknown-tier"])
    def test_unresolvable_tier_falls_back_to_chat(self, mod: Any, tier: str) -> None:
        """tiers 未映射的档位（含任意未知档位词）回退 defaults.chat。"""
        mod.set_config({"llm": _LLM_CONF})

        assert mod.resolve_model_id("", tier) == "chat-default"

    def test_all_empty_returns_chat_default(self, mod: Any) -> None:
        mod.set_config({"llm": _LLM_CONF})

        assert mod.resolve_model_id("", "") == "chat-default"

    def test_no_defaults_returns_empty(self, mod: Any) -> None:
        """llm.yaml 无 defaults（未表达模型意图）→ 空串，由调用方保持自身默认。"""
        mod.set_config({"llm": {"models": {}}})

        assert mod.resolve_model_id("", "") == ""

    def test_priority_is_total_order(self, mod: Any) -> None:
        """性质断言：显式 id 与 tiers 同时可用时，恒取显式 id（与档位无关）。"""
        mod.set_config({"llm": _LLM_CONF})

        for tier in ("large", "small", "unknown"):
            assert mod.resolve_model_id("explicit-id", tier) == "explicit-id"


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _load_server() -> Any:
    """加载 llm/server.py（与 test_streaming_server 同款 importlib 装载）。"""
    mod_name = "llm_server_model_resolve_test"
    cached = sys.modules.get(mod_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "server.py")
    assert spec is not None, "cannot load llm plugin server.py"
    assert spec.loader is not None, "cannot load llm plugin server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class TestModelResolveService:
    def test_handler_returns_resolved_id(self) -> None:
        srv = _load_server()
        srv.set_config({"llm": _LLM_CONF})

        result = _run(srv.model_resolve("", "large"))

        assert result == {"model_id": "tier-large"}

    def test_handler_passes_through_explicit_id(self) -> None:
        srv = _load_server()
        srv.set_config({"llm": _LLM_CONF})

        result = _run(srv.model_resolve("explicit-id", "large"))

        assert result == {"model_id": "explicit-id"}

    def test_manifest_declares_service(self) -> None:
        """manifest capabilities.services 在册 model.resolve（接线对账）。"""
        manifest = json.loads((_DIR / "plugin.json").read_text(encoding="utf-8"))
        names = {
            s.get("name")
            for s in manifest.get("capabilities", {}).get("services", [])
        }

        assert "model.resolve" in names
