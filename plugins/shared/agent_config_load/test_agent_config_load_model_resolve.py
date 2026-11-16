# @feature: FP-0.2.〇 管道引擎与插件执行模型（模型落定键） | @ci: python-coverage
"""agent_config_load 模型落定（初始化步写 state.model_id）。

契约：初始化装载 agent 配置后，若 state 无显式 model_id，经 llm_service
``model.resolve`` 服务解析（显式 id > tiers[tier] > defaults.chat）并写
``state.model_id``——prepare 链下游与 llm_core 消费同一事实（落定键）。
state 显式值优先不覆盖；通道未接线/调用失败/信封失败一律降级不写
（保持既有恒空行为，权威错误由 llm_core 执行时报出）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_model_resolve_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any]) -> PluginContext:
    return PluginContext(state=dict(state), config={})


@pytest.fixture
def agents_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "factory"))
    agents = tmp_path / "factory" / "agents"
    agents.mkdir(parents=True)
    return agents


class _FakeCaller:
    """记录调用的 fake 服务 caller（信封形态与 tool-executor 一致）。"""

    def __init__(self, envelope: dict[str, Any] | Exception) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._envelope = envelope

    async def __call__(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        self.calls.append((method, params))
        if isinstance(self._envelope, Exception):
            raise self._envelope
        return self._envelope


def _ok(model_id: str) -> dict[str, Any]:
    return {"success": True, "data": {"model_id": model_id}}


class TestModelResolve:
    def test_resolves_and_writes_model_id(self, agents_root: Path) -> None:
        """yaml model_tier=large → 服务解析 → 写 state.model_id（含线上契约对账）。"""
        (agents_root / "resolver.yaml").write_text(
            "display_name: 落定者\nmodel_tier: large\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        caller = _FakeCaller(_ok("tier-large-model"))
        plugin.set_llm_service_caller(caller)

        res = _run(plugin.execute(_ctx({"agent.id": "resolver"})))

        assert res.state_updates["model_id"] == "tier-large-model"
        assert caller.calls[0] == (
            "invoke",
            {
                "tool_name": "model.resolve",
                "plugin_id": "llm_service",
                "args": {"model_id": "", "model_tier": "large"},
            },
        )

    def test_no_tier_still_resolves_chat_default(self, agents_root: Path) -> None:
        """无 model_tier 的 agent 同样落定（服务侧回退 defaults.chat）。"""
        (agents_root / "plain.yaml").write_text(
            "display_name: 无档位者\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        caller = _FakeCaller(_ok("chat-default-model"))
        plugin.set_llm_service_caller(caller)

        res = _run(plugin.execute(_ctx({"agent.id": "plain"})))

        assert res.state_updates["model_id"] == "chat-default-model"
        assert caller.calls[0][1]["args"] == {"model_id": "", "model_tier": ""}

    def test_upstream_tier_beats_yaml(self, agents_root: Path) -> None:
        """state 显式档位优先于 yaml 档位（与既有 model_tier 写入口径一致）。"""
        (agents_root / "tiered.yaml").write_text(
            "display_name: 档位者\nmodel_tier: large\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        caller = _FakeCaller(_ok("small-model"))
        plugin.set_llm_service_caller(caller)

        _run(plugin.execute(_ctx({"agent.id": "tiered", "model_tier": "small"})))

        assert caller.calls[0][1]["args"] == {"model_id": "", "model_tier": "small"}

    def test_explicit_state_model_id_wins_and_skips_call(self, agents_root: Path) -> None:
        """state 已有显式 model_id → 不调服务、不覆盖（overlay/上游显式值优先）。"""
        (agents_root / "explicit.yaml").write_text(
            "display_name: 显式者\nmodel_tier: large\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        caller = _FakeCaller(_ok("ignored"))
        plugin.set_llm_service_caller(caller)

        res = _run(
            plugin.execute(_ctx({"agent.id": "explicit", "model_id": "explicit-id"}))
        )

        assert "model_id" not in res.state_updates
        assert caller.calls == []

    def test_missing_caller_degrades(self, agents_root: Path) -> None:
        """通道未接线 → 降级不写（保持恒空旧行为），不阻断装载。"""
        (agents_root / "nocaller.yaml").write_text(
            "display_name: 无通道\nmodel_tier: large\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})

        res = _run(plugin.execute(_ctx({"agent.id": "nocaller"})))

        assert "model_id" not in res.state_updates
        assert res.state_updates["context.agent_name"] == "无通道"

    @pytest.mark.parametrize(
        "envelope",
        [
            {"success": False, "error": "service down"},
            {"success": True, "data": {"model_id": ""}},
            {"success": True, "data": None},
            RuntimeError("caller exploded"),
        ],
        ids=["envelope-fail", "empty-id", "bad-data", "caller-raise"],
    )
    def test_service_failure_degrades(
        self, agents_root: Path, envelope: dict[str, Any] | Exception
    ) -> None:
        """调用失败/信封失败/空结果 → 降级不写，权威错误留给 llm_core。"""
        (agents_root / "degraded.yaml").write_text(
            "display_name: 降级者\nmodel_tier: large\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        plugin.set_llm_service_caller(_FakeCaller(envelope))

        res = _run(plugin.execute(_ctx({"agent.id": "degraded"})))

        assert "model_id" not in res.state_updates
