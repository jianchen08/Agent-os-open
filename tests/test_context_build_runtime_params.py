# @feature: FP-0.2.二 agent 运行时参数配置装载 | @ci: python-coverage
"""context_build 的 agent 运行时参数装载契约（model_tier 等 4 字段）。

锁三件事：
1. **装载**：agent yaml 顶层声明的 model_tier / max_iterations /
   max_reminders / timeout_seconds → 顶层 state 键（stop_check /
   task_reminder / llm_core 读 state 消费）；
2. **优先级**：state 已有非空显式值（overlay/上游注入）优先，yaml 不覆盖；
   空串/None 视为未设置（step context 模板对缺失键渲染出 ""，先于本插件
   落 state，不得挡住 yaml 装载）；
3. **-1 语义**：无限制标记 -1 原值透传（不得被当作假值丢弃），
   未声明的键不注入（零兜底）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "context_build")
import plugin as context_build_mod  # noqa: E402


def _ctx(state: dict):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


def _write_agent(agents_dir: Path, agent_id: str, content: str) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_id}.yaml").write_text(content, encoding="utf-8")


@pytest.fixture()
def config_root(tmp_path, monkeypatch):
    """隔离的 AGENTOS_CONFIG_ROOT（agents 目录按用例内容写入）。"""
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))
    return tmp_path


def test_runtime_params_loaded_from_agent_yaml(config_root, tmp_path):
    """装载：yaml 声明的 4 个运行时参数 → 顶层 state 键原值注入。"""
    _write_agent(
        tmp_path / "agents",
        "rt_agent",
        "model_tier: small\n"
        "max_iterations: 500\n"
        "max_reminders: 3\n"
        "timeout_seconds: 2400\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "rt_agent"}))).state_updates
    assert updates.get("model_tier") == "small"
    assert updates.get("max_iterations") == 500
    assert updates.get("max_reminders") == 3
    assert updates.get("timeout_seconds") == 2400


def test_runtime_params_negative_one_passthrough(config_root, tmp_path):
    """-1 = 无限制：原值透传，不得被当作假值丢弃（main agent 声明 -1）。"""
    _write_agent(
        tmp_path / "agents",
        "rt_unlimited",
        "max_iterations: -1\ntimeout_seconds: -1\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "rt_unlimited"}))).state_updates
    assert updates.get("max_iterations") == -1
    assert updates.get("timeout_seconds") == -1


def test_state_explicit_value_wins_over_yaml(config_root, tmp_path):
    """优先级：state 已有显式值（overlay/上游注入）优先，yaml 不覆盖。"""
    _write_agent(
        tmp_path / "agents",
        "rt_override",
        "model_tier: large\nmax_iterations: 500\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(
        cb.execute(_ctx({"agent_id": "rt_override", "model_tier": "small", "max_iterations": 50}))
    ).state_updates
    assert "model_tier" not in updates, "state 显式 model_tier 存在时 yaml 值不得覆盖"
    assert "max_iterations" not in updates, "state 显式 max_iterations 存在时 yaml 值不得覆盖"


def test_state_empty_value_treated_as_unset(config_root, tmp_path):
    """空值视为未设置：state 的空串/None（模板对缺失键的渲染产物）不挡 yaml 装载。

    回归契约：prepare step 的 context 回显 ``model_tier: "{{state.model_tier}}"``
    在本插件运行前把 "" merge 进 state——若按"键存在即显式值"判优，agent yaml
    的 model_tier 永远装不进去，llm_core 落 defaults.chat 兜底模型。
    """
    _write_agent(
        tmp_path / "agents",
        "rt_empty",
        "model_tier: large\nmax_iterations: 500\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(
        cb.execute(
            _ctx({"agent_id": "rt_empty", "model_tier": "", "max_iterations": None})
        )
    ).state_updates
    assert updates.get("model_tier") == "large", "state 空串 model_tier 不得挡住 yaml 装载"
    assert updates.get("max_iterations") == 500, "state None max_iterations 不得挡住 yaml 装载"


def test_runtime_params_absent_not_injected(config_root, tmp_path):
    """零兜底：yaml 未声明的键不注入（下游回退各自默认值）。"""
    _write_agent(tmp_path / "agents", "rt_bare", "display_name: Bare\n")
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "rt_bare"}))).state_updates
    for key in ("model_tier", "max_iterations", "max_reminders", "timeout_seconds"):
        assert key not in updates
