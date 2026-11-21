# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""agent_config_load sidecar 实例复用契约测试（M2 状态复位 + 并发层级隔离）。

同 sidecar 进程内一个 AgentConfigLoadPlugin 实例被多个 agent 管道**连续**复用：
前一 agent yaml 的 level 覆盖实例默认层级后，下一 agent（yaml 无 level 或
无配置）必须回到插件构造默认——agent 层级不得跨管道残留。

**并发**复用（SDK 对 async handler 不加锁）另有一层契约：层级是 per-pipeline
事实，必须由 execute 局部值贯穿本轮，不得经实例字段中转——父 L1 + 子 L3 同时
进入时，A 在 await 处让出、B 整轮覆盖实例字段，A 恢复后会把 B 的层级写进自己
的 state.AGENT_LEVEL（L1 豁免/评估闸门旁路）。交叠点取 ``_resolve_initial_model``
（``_do_work`` 内 ``:474`` 写默认与 ``:617`` 读值之间最长的让出面，首轮必走）。
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
from _libs.pipeline_base.types import StateKeys  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_sidecar_test"
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
        plugin = mod.AgentConfigLoadPlugin(config={})

        res_a = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert res_a.state_updates["context.is_project"] is False, (
            "Agent A 为 L2 叶子执行者，不应标记为项目级"
        )

        res_b = _run(plugin.execute(_ctx({"agent.id": "no-such-agent"})))
        assert res_b.state_updates["context.is_project"] is True, (
            f"Agent B 无层级配置应回默认 L1，实际残留: {res_b.state_updates}"
        )

    def test_plugin_config_level_still_highest_priority(self, agents_root: Path) -> None:
        """插件配置显式 agent_level 高于 yaml level（现行优先级不变，防回归）。"""
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={"agent_level": "L1"})

        res = _run(plugin.execute(_ctx({"agent.id": "l2coder"})))
        assert res.state_updates["context.agent_name"] == ""
        # 显式插件配置 L1 压过 yaml 的 L2
        assert res.state_updates["context.is_project"] is True


@pytest.fixture
def levels_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 AGENTOS_CONFIG_ROOT：一个无 level 的主 agent + 一个 level=L3 的执行者。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "main_agent.yaml").write_text("name: 主 agent\n", encoding="utf-8")
    (agents_dir / "l3worker.yaml").write_text("level: L3\nname: 叶子执行者\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))
    return tmp_path


class _GatedModelCaller:
    """``_resolve_initial_model`` 通道桩：首个调用（= 先入管道）在 await 处让出。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "invoke"
        assert params["tool_name"] == "model.resolve"
        self.calls += 1
        if self.calls == 1:  # 先入管道在此让出，后入管道整轮跑完
            self.entered.set()
            await self.release.wait()
        return {"success": True, "data": {"model_id": "model-x"}}


def _interleave(plugin: Any, caller: _GatedModelCaller, ctx_a: PluginContext, ctx_b: PluginContext) -> tuple[Any, Any]:
    """A 先进 execute 并在 model.resolve 处让出；B 整轮完成后放行 A。"""

    async def scenario() -> tuple[Any, Any]:
        plugin.set_llm_service_caller(caller)
        task_a = asyncio.create_task(plugin.execute(ctx_a))
        await caller.entered.wait()
        result_b = await plugin.execute(ctx_b)
        caller.release.set()
        return await task_a, result_b

    try:
        return _run(scenario())
    finally:
        plugin.set_llm_service_caller(None)


class TestAgentLevelPerPipelineConcurrent:
    """并发交错：层级是 per-pipeline 事实，A 恢复后不得读到 B 的层级。"""

    def test_interleaved_execute_keeps_own_level(self, levels_root: Path) -> None:
        """A（主 agent，无 yaml level → 默认 L1）在 model.resolve 让出，B（yaml L3）
        整轮完成并覆盖实例层级；A 的 state.agent_level 必须仍是 L1。"""
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        caller = _GatedModelCaller()

        res_a, res_b = _interleave(
            plugin,
            caller,
            _ctx({"agent.id": "main_agent"}),
            _ctx({"agent.id": "l3worker"}),
        )

        assert caller.calls == 2, "两管道各自走到 model.resolve 落定步"
        assert res_b.state_updates[StateKeys.AGENT_LEVEL] == "L3"
        assert res_a.state_updates[StateKeys.AGENT_LEVEL] == "L1", (
            "A 的层级被 B 的 L3 覆盖（L1 豁免/评估闸门将被旁路）"
        )
        assert res_a.state_updates["context.is_project"] is True

    def test_plugin_config_level_priority_held_under_interleave(self, levels_root: Path) -> None:
        """插件配置显式 agent_level=L2 与 yaml L3 并发：A 必须仍写配置值 L2。"""
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={"agent_level": "L2"})
        caller = _GatedModelCaller()

        res_a, res_b = _interleave(
            plugin,
            caller,
            _ctx({"agent.id": "main_agent"}),
            _ctx({"agent.id": "l3worker"}),
        )

        assert res_b.state_updates[StateKeys.AGENT_LEVEL] == "L2", "插件配置层压过 yaml"
        assert res_a.state_updates[StateKeys.AGENT_LEVEL] == "L2"
        assert res_a.state_updates["context.is_project"] is False

    def test_do_work_never_writes_instance_level_field(self, levels_root: Path) -> None:
        """形态约束：execute 不得把本轮层级写回实例属性（构造默认恒不变）。"""
        mod = _load_plugin_module()
        plugin = mod.AgentConfigLoadPlugin(config={})
        caller = _GatedModelCaller()

        _interleave(
            plugin,
            caller,
            _ctx({"agent.id": "main_agent"}),
            _ctx({"agent.id": "l3worker"}),
        )

        assert plugin._agent_level == "L1", "实例层级字段被某轮 execute 写入"
