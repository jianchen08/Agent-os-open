# @feature: FP-0.2.〇 管道引擎与插件执行模型（agent yaml tool_deny 动态面否决） | @ci: python-coverage
"""agent_config_load tool_deny 三态语义。

agent yaml 声明 ``tool_deny: [工具名]`` = 该 agent 的动态工具面否决集
（resource_search 搜不到 + call_tool 调不动，ADR 2026-11-18）——本插件写
state.tool_deny 供 tool_args_inject 同轮注入两工具入参。list 非空 = 即写；
空表/未声明 = 不写键（缺席语义 = 无否决）。
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
_SHARED = _DIR.parents[0]

for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_deny_test"
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


class TestToolDenyStates:
    def test_list_written_to_state(self, agents_root: Path) -> None:
        """tool_deny 非空 list → state.tool_deny 即写（字符串列表规整）。"""
        (agents_root / "guardian.yaml").write_text(
            "display_name: 守卫\ntool_ids: [file_read]\ntool_deny:\n- bash\n- phone_control\n",
            encoding="utf-8",
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})

        res = _run(plugin.execute(_ctx({"agent.id": "guardian"})))

        assert res.state_updates.get("tool_deny") == ["bash", "phone_control"]

    def test_empty_and_undeclared_write_no_key(self, agents_root: Path) -> None:
        """空表与未声明 = 不写键（缺席语义 = 无否决，两态同面）。"""
        (agents_root / "plain.yaml").write_text(
            "display_name: 朴素\ntool_ids: [file_read]\ntool_deny: []\n", encoding="utf-8"
        )
        (agents_root / "bare.yaml").write_text(
            "display_name: 裸配\ntool_ids: [file_read]\n", encoding="utf-8"
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})

        for agent_id in ("plain", "bare"):
            res = _run(plugin.execute(_ctx({"agent.id": agent_id})))
            assert "tool_deny" not in res.state_updates, (
                f"{agent_id} 不应写 tool_deny 键，实际: {res.state_updates.get('tool_deny')!r}"
            )

    def test_non_string_entries_dropped(self, agents_root: Path) -> None:
        """非字符串条目静默丢弃与 tool_ids 同口径（内核列表键契约），不整键失败。"""
        (agents_root / "mixed.yaml").write_text(
            "display_name: 混合\ntool_ids: [file_read]\ntool_deny:\n- bash\n- 42\n",
            encoding="utf-8",
        )
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})

        res = _run(plugin.execute(_ctx({"agent.id": "mixed"})))

        assert res.state_updates.get("tool_deny") == ["bash"]
