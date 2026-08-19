# @feature: FP-0.2.二 管道配置接线 | @ci: python-coverage
"""autonomous 管道 context_build 接线回归测试。

2026-08-19 bug：前端发消息后 LLM 请求 messages[0] 是空 system 消息
（payload_diag 快照实证 {"role":"system","content":""}）。
根因链：
  - prompt_build 只读 context.system_prompt（0ed9ae98 删除顶层回退键，
    契约收紧为"context_build 无条件写入"）；
  - 唯一写该键的 context_build 插件从未被接进 autonomous.yaml 的
    prepare 链（ee605eea "agent 配置出内核"迁到 sidecar 后漏接线）；
  - 内核 per-iteration load_agent_into_state 注入的顶层 system_prompt
    因此无人消费。

本文件锁两件事：
  1. 配置接线：prepare 链必须含 pipeline_context_build，且先于
     tool_schema（tool_ids 供给）与 prompt_build（system_prompt 供给）；
  2. 插件契约：context_build → prompt_build 串联产出非空 system 消息，
     跳过 context_build 时 prompt_build 产出空 content（断链症状固化，
     防止回退键悄悄复活掩盖接线缺口）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from tests._pipeline_plugin_path import add_plugin_dir

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AUTONOMOUS_YAML = _REPO_ROOT / "config" / "pipelines" / "autonomous.yaml"


def _prepare_steps() -> list[str]:
    """解析 autonomous.yaml main 循环体 prepare 组合节点的 steps 列表。"""
    cfg = yaml.safe_load(_AUTONOMOUS_YAML.read_text(encoding="utf-8"))
    bodies = cfg["loop_bodies"]
    main = next(b for b in bodies if b["id"] == "main")
    prepare = next(s for s in main["steps"] if isinstance(s, dict) and s.get("id") == "prepare")
    return [s for s in prepare["steps"] if isinstance(s, str)]


def test_prepare_chain_wires_context_build():
    """prepare 链含 pipeline_context_build，且在 tool_schema/prompt_build 之前。"""
    steps = _prepare_steps()
    assert "pipeline_context_build" in steps, (
        "pipeline_context_build 未接入 autonomous 管道 prepare 链——"
        "context.system_prompt 无人写入，LLM 请求 system 消息为空"
        "（2026-08-19 前端空 system prompt 断链）"
    )
    idx = {name: i for i, name in enumerate(steps)}
    assert idx["pipeline_context_build"] < idx["pipeline_tool_schema"], (
        "context_build 必须先于 tool_schema：agent yaml 的 tool_ids 由其注入"
    )
    assert idx["pipeline_context_build"] < idx["pipeline_prompt_build"], (
        "context_build 必须先于 prompt_build：context.system_prompt 由其供给"
    )


# ── 插件契约（context_build → prompt_build 平铺导入，逐出同名裸模块）──

add_plugin_dir("input", "context_build")
import plugin as context_build_mod  # noqa: E402

add_plugin_dir("input", "prompt_build")
import plugin as prompt_build_mod  # noqa: E402


def _chain_system_message(agent_cfg_root: Path, agent_id: str, run_context_build: bool):
    """串联执行插件链，返回 prompt_build 产出的 system_message。"""
    state: dict = {"agent_id": agent_id, "project_root": str(_REPO_ROOT)}
    if run_context_build:
        cb = context_build_mod.ContextBuildPlugin(config={})
        for k, v in asyncio.run(cb.execute(_ctx(state))).state_updates.items():
            state[k] = v
    pb = prompt_build_mod.PromptBuildPlugin(
        config={"include_compressed_layers": False, "include_static_vars": False}
    )
    updates = asyncio.run(pb.execute(_ctx(state))).state_updates
    return updates["system_message"]


def _ctx(state: dict):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


def test_chain_produces_nonempty_system_message(tmp_path, monkeypatch):
    """context_build 接通后：agent yaml 的 system_prompt 流入 system_message。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "test_agent.yaml").write_text(
        "system_prompt: '# TEST-PERSONA-MARK 你是测试 agent'\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))

    msg = _chain_system_message(tmp_path, "test_agent", run_context_build=True)

    assert msg["role"] == "system"
    assert "TEST-PERSONA-MARK" in msg["content"]


def test_chain_without_context_build_yields_empty_system(tmp_path, monkeypatch):
    """断链症状固化：跳过 context_build 时 system content 为空（顶层键无人桥接）。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "test_agent.yaml").write_text(
        "system_prompt: '# TEST-PERSONA-MARK 你是测试 agent'\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))

    msg = _chain_system_message(tmp_path, "test_agent", run_context_build=False)

    # prompt_build 契约只认 context.system_prompt：顶层 system_prompt 即使在
    # state 里（内核 load_agent_into_state 注入）也不消费。若此断言失败，
    # 说明回退键被恢复——接线缺口会被掩盖，应改回去并保证管道接线。
    assert msg["content"] == ""
