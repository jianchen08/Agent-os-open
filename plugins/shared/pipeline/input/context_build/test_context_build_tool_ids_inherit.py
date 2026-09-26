# @feature: FP-0.2.〇 管道引擎与插件执行模型（tool_ids: inherit 语义） | @ci: python-coverage
"""context_build tool_ids: inherit 通用语义。

agent yaml 声明 ``tool_ids: inherit`` = 继承基线全量工具面（附身等场景：
执行者身份换、工具能力保留）——本插件不写 state.tool_ids，下游 tool_schema
走全量注册表，主 agent 工具清单更新自动跟随。list 白名单（含显式空表）与
未声明行为不变（既有契约防回归）。
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
_SHARED = _DIR.parents[2]  # plugins/shared/

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "context_build_plugin_inherit_test"
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


class TestToolIdsInherit:
    def test_inherit_writes_no_tool_ids_key(self, agents_root: Path) -> None:
        """tool_ids: inherit → 不写 state.tool_ids（下游走基线全量工具面）。"""
        (agents_root / "inheritor.yaml").write_text(
            "display_name: 附身执行者\ntool_ids: inherit\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})

        res = _run(plugin.execute(_ctx({"agent.id": "inheritor"})))

        assert "tool_ids" not in res.state_updates, (
            f"inherit 不得写 tool_ids 键，实际: {res.state_updates.get('tool_ids')!r}"
        )
        assert res.state_updates["context.agent_name"] == "附身执行者", (
            "inherit 不影响其余装配产出"
        )

    def test_list_whitelist_unchanged(self, agents_root: Path) -> None:
        """list 白名单照旧写入（含显式空表语义，既有契约防回归）。"""
        (agents_root / "lister.yaml").write_text(
            "display_name: 白名单执行者\ntool_ids: [file_read]\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})

        res = _run(plugin.execute(_ctx({"agent.id": "lister"})))

        assert res.state_updates["tool_ids"] == ["file_read"]

    def test_undeclared_still_writes_no_key(self, agents_root: Path) -> None:
        """未声明 tool_ids 照旧不写键（inherit 与未声明同为全量面，但语义可辨）。"""
        (agents_root / "bare.yaml").write_text(
            "display_name: 裸执行者\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})

        res = _run(plugin.execute(_ctx({"agent.id": "bare"})))

        assert "tool_ids" not in res.state_updates

    def test_same_instance_switches_between_forms(self, agents_root: Path) -> None:
        """同实例连续复用：inherit agent 与 list agent 各按声明装配（不串）。"""
        (agents_root / "inheritor.yaml").write_text(
            "display_name: 附身执行者\ntool_ids: inherit\n", encoding="utf-8"
        )
        (agents_root / "lister.yaml").write_text(
            "display_name: 白名单执行者\ntool_ids: [file_read]\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})

        first = _run(plugin.execute(_ctx({"agent.id": "inheritor"})))
        second = _run(plugin.execute(_ctx({"agent.id": "lister"})))

        assert "tool_ids" not in first.state_updates
        assert second.state_updates["tool_ids"] == ["file_read"]
