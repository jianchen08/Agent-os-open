# @feature: FP-0.2.二 agent yaml 加载可观测（兜底反模式审查 P3） | @ci: python-coverage
"""context_build agent yaml 加载失败的可观测性契约。

锁两件事：
1. **归因区分**：未找到 agent yaml / agents 目录不存在（合法，debug 日志，
   按默认配置运行）与 yaml 存在但解析失败（配置错误）必须分开；
2. **失败语义**：解析失败必须上抛终止管道——人格/system_prompt/tool_ids
   整体静默降级为默认值会把配置错误伪装成"无配置 agent"，禁止；仅
   "配置缺失形态"（目录不存在/未命中文件）保留默认运行。
"""

from __future__ import annotations

import asyncio
import logging
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


def test_corrupt_yaml_raises_with_path(config_root, tmp_path):
    """找到但解析失败 → 上抛，异常携带损坏文件 path 与原异常链。"""
    _write_agent(
        tmp_path / "agents",
        "broken_agent",
        "level: [unclosed\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    with pytest.raises(RuntimeError) as exc_info:
        cb._load_agent_config("broken_agent")
    msg = str(exc_info.value)
    assert "解析失败" in msg
    assert "broken_agent.yaml" in msg, "异常信息必须带损坏文件 path"
    assert exc_info.value.__cause__ is not None, "原始解析异常必须经 __cause__ 保留"


def test_corrupt_yaml_execute_terminates(config_root, tmp_path):
    """端到端：损坏 yaml 的 agent 管道必须在输入阶段终止（不降级续跑）。"""
    _write_agent(
        tmp_path / "agents",
        "broken_e2e",
        "system_prompt: [bad\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    with pytest.raises(RuntimeError, match="解析失败"):
        asyncio.run(cb.execute(_ctx({"agent.id": "broken_e2e"})))


def test_missing_yaml_logs_debug_and_defaults(config_root, tmp_path, caplog):
    """未找到 yaml（合法）→ debug 日志 + 空配置按默认运行，不得上抛/打 error。"""
    (tmp_path / "agents").mkdir(parents=True)  # 目录存在但无该 agent 的 yaml
    cb = context_build_mod.ContextBuildPlugin(config={})
    with caplog.at_level(logging.DEBUG, logger="plugin"):
        data = cb._load_agent_config("ghost_agent")
    assert data == {}
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
        "未找到 yaml 是合法路径，不应打 error"
    )
    assert any("未找到" in r.getMessage() for r in caplog.records), (
        "未找到路径应有 debug 留痕"
    )


def test_agents_dir_missing_still_runs_with_defaults(tmp_path, caplog):
    """agents 目录不存在 → 默认跑通（插件配置默认值生效），不打 error/warning。"""
    import os

    nonexistent_root = tmp_path / "no_such_config_root"
    os.environ["AGENTOS_CONFIG_ROOT"] = str(nonexistent_root)
    try:
        cb = context_build_mod.ContextBuildPlugin(
            config={"system_prompt": "默认骨架提示词"}
        )
        updates = asyncio.run(cb.execute(_ctx({"agent.id": "any_agent"}))).state_updates
    finally:
        os.environ.pop("AGENTOS_CONFIG_ROOT", None)
    assert updates["context.system_prompt"] == "默认骨架提示词", (
        "目录不存在时 system_prompt 回退插件配置默认值，管道照常供给"
    )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_valid_yaml_no_logs(config_root, tmp_path, caplog):
    """正常 yaml → 无 error/warning（回归）。"""
    _write_agent(
        tmp_path / "agents",
        "ok_agent",
        "level: L2\ndisplay_name: OK\nsystem_prompt: 来自yaml\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    with caplog.at_level(logging.DEBUG, logger="plugin"):
        data = cb._load_agent_config("ok_agent")
    assert data.get("level") == "L2"
    assert data.get("system_prompt") == "来自yaml"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_tool_ids_key_present_writes_even_when_empty(config_root, tmp_path):
    """tool_ids 键存在即写（含显式空表 = 声明零工具），与"未声明"严格区分——
    下游 tool_schema 据此跳过配置断链告警（K10）。"""
    _write_agent(
        tmp_path / "agents",
        "zero_tool_agent",
        "system_prompt: p\ntool_ids: []\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent.id": "zero_tool_agent"}))).state_updates
    assert updates["tool_ids"] == [], "显式空表必须写入（声明零工具 ≠ 断链）"


def test_tool_ids_absent_writes_nothing(config_root, tmp_path):
    """yaml 无 tool_ids 键 → 不写该键（由下游按断链处理）。"""
    _write_agent(
        tmp_path / "agents",
        "no_tools_key_agent",
        "system_prompt: p\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent.id": "no_tools_key_agent"}))).state_updates
    assert "tool_ids" not in updates


def test_tool_ids_nonempty_written(config_root, tmp_path):
    """非空 tool_ids 照常写入（唯一供给方 = context_build）。"""
    _write_agent(
        tmp_path / "agents",
        "tool_agent",
        "system_prompt: p\ntool_ids: [file_read, bash_execute]\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent.id": "tool_agent"}))).state_updates
    assert updates["tool_ids"] == ["file_read", "bash_execute"]
