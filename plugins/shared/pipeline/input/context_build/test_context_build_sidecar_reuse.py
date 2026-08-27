# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""context_build sidecar 实例复用契约测试（M2 状态复位）。

同 sidecar 进程内一个 ContextBuildPlugin 实例被多个 agent 管道连续复用：
前一 agent yaml 的 level 覆盖实例默认层级后，下一 agent（yaml 无 level 或
无配置）必须回到插件构造默认——agent 层级不得跨管道残留。
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
    mod_name = "context_build_plugin_sidecar_test"
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
    """临时 AGENTOS_CONFIG_ROOT：仅含一个声明 level=L2 的 agent yaml。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "l2coder.yaml").write_text(
        "level: L2\nsystem_prompt: 你是叶子执行者\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))
    return tmp_path


class TestAgentLevelResetPerPipeline:
    def test_yaml_level_does_not_leak_to_next_agent(self, agents_root: Path) -> None:
        """Agent A（yaml 声明 L2）先把实例层级抬到 L2；随后 Agent B 无任何
        配置来源，同实例执行必须回默认 L1（is_project=True），不得残留 L2。"""
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={})

        res_a = _run(plugin.execute(_ctx({"agent_id": "l2coder"})))
        assert res_a.state_updates["context.is_project"] is False, (
            "Agent A 为 L2 叶子执行者，不应标记为项目级"
        )

        res_b = _run(plugin.execute(_ctx({"agent_id": "no-such-agent"})))
        assert res_b.state_updates["context.is_project"] is True, (
            f"Agent B 无层级配置应回默认 L1，实际残留: {res_b.state_updates}"
        )

    def test_plugin_config_level_still_highest_priority(self, agents_root: Path) -> None:
        """插件配置显式 agent_level 高于 yaml level（现行优先级不变，防回归）。"""
        mod = _load_plugin_module()
        plugin = mod.ContextBuildPlugin(config={"agent_level": "L1"})

        res = _run(plugin.execute(_ctx({"agent_id": "l2coder"})))
        assert res.state_updates["context.agent_name"] == ""
        # 显式插件配置 L1 压过 yaml 的 L2
        assert res.state_updates["context.is_project"] is True
