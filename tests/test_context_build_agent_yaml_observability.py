# @feature: FP-0.2.二 agent yaml 加载可观测（兜底反模式审查 P3） | @ci: python-coverage
"""context_build agent yaml 加载失败的可观测性契约（P3）。

锁两件事：
1. **归因区分**：未找到 agent yaml（合法，debug 日志）与找到但解析失败
   （配置错误，error 日志含 path + 异常）必须分开，不得静默返回空 dict；
2. **行为不变**：两种情况仍返回空 dict 按默认配置降级运行（只加可观测性，
   不收紧行为）。

[来源: docs/working/兜底反模式全库审查_20260820.md 三节 P3]
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


def test_corrupt_yaml_logs_error_with_path(config_root, tmp_path, caplog):
    """找到但解析失败 → error 日志含 path + 异常（仍返回空 dict 降级）。"""
    _write_agent(
        tmp_path / "agents",
        "broken_agent",
        "level: [unclosed\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    with caplog.at_level(logging.ERROR, logger="plugin"):
        data = cb._load_agent_config("broken_agent")
    assert data == {}, "解析失败仍按空配置降级（可观测修复，非行为收紧）"
    err_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert err_records, "解析失败必须有 error 级日志"
    joined = " ".join(r.getMessage() for r in err_records)
    assert "解析失败" in joined
    assert "broken_agent.yaml" in joined, "error 日志必须带损坏文件 path"
    assert "unclosed" in joined or "解析" in joined, "error 日志应含异常信息"


def test_missing_yaml_logs_debug_only(config_root, tmp_path, caplog):
    """未找到 yaml（合法）→ debug 日志，不得出现 error。"""
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


def test_valid_yaml_no_logs(config_root, tmp_path, caplog):
    """正常 yaml → 无 error/warning（回归）。"""
    _write_agent(
        tmp_path / "agents",
        "ok_agent",
        "level: L2\ndisplay_name: OK\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    with caplog.at_level(logging.DEBUG, logger="plugin"):
        data = cb._load_agent_config("ok_agent")
    assert data.get("level") == "L2"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_corrupt_yaml_execute_still_runs_with_defaults(config_root, tmp_path):
    """端到端：损坏 yaml 的 agent 管道照跑（默认值降级不被放大成崩溃）。"""
    _write_agent(
        tmp_path / "agents",
        "broken_e2e",
        "system_prompt: [bad\n",
    )
    cb = context_build_mod.ContextBuildPlugin(config={})
    updates = asyncio.run(cb.execute(_ctx({"agent_id": "broken_e2e"}))).state_updates
    assert isinstance(updates, dict)
