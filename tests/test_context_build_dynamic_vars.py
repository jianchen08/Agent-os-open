# @feature: FP-0.2.二 dynamic_vars 配置装载与零兜底 | @ci: python-coverage
"""context_build / prompt_build 的 dynamic_vars 三层契约（F9）。

锁三件事：
1. **装载**：agent yaml 的 dynamic_vars.items → state["context.dynamic_vars"]，
   prompt_build 据此渲染动态变量（含 {{path:}} 占位符）；
2. **零兜底**：无 agent 配置且无插件默认 → 不注入任何动态变量消息；
3. **无实例缓存污染**：同一插件实例先跑 L1 再跑子 agent，
   context.agent_name 各次正确（不得由实例缓存串味）。

[来源: docs/working/管道执行bug修复方案_20260820.md F9]
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "context_build")
import plugin as context_build_mod  # noqa: E402

add_plugin_dir("input", "prompt_build")
import plugin as prompt_build_mod  # noqa: E402


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


def test_agent_dynamic_vars_loaded_into_state(config_root, tmp_path):
    """装载：agent yaml dynamic_vars.items → context.dynamic_vars。"""
    _write_agent(
        tmp_path / "agents",
        "dv_agent",
        "display_name: DV Agent\n"
        "dynamic_vars:\n"
        "  enabled: true\n"
        "  items:\n"
        "  - '{{timestamp:%Y-%m-%d}}'\n"
        "  - type: session\n"
        "    name: 会话\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "dv_agent"}))).state_updates
    assert updates.get("context.dynamic_vars") == [
        "{{timestamp:%Y-%m-%d}}",
        {"type": "session", "name": "会话"},
    ], "agent yaml 的 dynamic_vars.items 应装载进 context.dynamic_vars"


def test_agent_dynamic_vars_disabled_not_loaded(config_root, tmp_path):
    """enabled: false → 不装载（配置开关语义保持）。"""
    _write_agent(
        tmp_path / "agents",
        "dv_off",
        "display_name: Off\n"
        "dynamic_vars:\n"
        "  enabled: false\n"
        "  items:\n"
        "  - '{{timestamp}}'\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "dv_off"}))).state_updates
    assert "context.dynamic_vars" not in updates


def test_chain_renders_configured_dynamic_vars(config_root, tmp_path):
    """端到端：context_build 装载 → prompt_build 按配置渲染（不再吃兜底块）。"""
    _write_agent(
        tmp_path / "agents",
        "dv_chain",
        "system_prompt: '# CHAIN 你是链路测试'\n"
        "dynamic_vars:\n"
        "  enabled: true\n"
        "  items:\n"
        "  - type: timestamp\n"
        "    name: 时间\n"
        "    format: '%H:%M:%S'\n",
    )
    state: dict = {"agent_id": "dv_chain"}
    cb = context_build_mod.ContextBuildPlugin(config={})
    for k, v in asyncio.run(cb.execute(_ctx(state))).state_updates.items():
        state[k] = v
    pb = prompt_build_mod.PromptBuildPlugin(
        config={"include_compressed_layers": False, "include_static_vars": False}
    )
    updates = asyncio.run(pb.execute(_ctx(state))).state_updates
    msg = updates["prompt.dynamic_vars"]
    assert msg is not None, "配置声明了 dynamic_vars → 应注入"
    assert "- 时间: " in msg["content"]
    # 零兜底：配置只声明了时间——不得出现兜底块的其它行
    assert "- Agent:" not in msg["content"], "兜底块 Agent 行已删，身份归 persona"
    assert "- 会话:" not in msg["content"], "兜底块会话行已删"


def test_agent_name_no_instance_cache_pollution(config_root, tmp_path):
    """同一插件实例跨管道复用：agent_name 每次按当前管道 agent 重新解析。

    旧 bug：L1 先跑把 display_name 写进 self._agent_name，同 sidecar 进程内
    子任务管道复用实例时 `if not self._agent_name` 恒 False——子 agent 的
    "代码审查专家"永远写不进，context.agent_name 恒"灵汐"（实测根因）。
    """
    _write_agent(tmp_path / "agents", "l1_main", "display_name: 灵汐\n")
    _write_agent(tmp_path / "agents", "sub_reviewer", "display_name: 代码审查专家\n")

    cb = context_build_mod.ContextBuildPlugin(config={})  # 同一实例
    first = asyncio.run(cb.execute(_ctx({"agent_id": "l1_main"}))).state_updates
    second = asyncio.run(cb.execute(_ctx({"agent_id": "sub_reviewer"}))).state_updates

    assert first["context.agent_name"] == "灵汐"
    assert second["context.agent_name"] == "代码审查专家", (
        "实例缓存污染回归：第二次执行必须拿到当前管道 agent 的名字"
    )


def test_agent_name_config_default_when_yaml_missing(config_root, tmp_path):
    """agent yaml 缺 display_name → 回退插件配置默认（agent_name 键）。"""
    _write_agent(tmp_path / "agents", "no_display", "system_prompt: x\n")
    cb = context_build_mod.ContextBuildPlugin(config={"agent_name": "配置默认名"})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "no_display"}))).state_updates
    assert updates["context.agent_name"] == "配置默认名"
