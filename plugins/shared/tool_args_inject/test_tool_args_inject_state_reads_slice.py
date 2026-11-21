# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""tool_args_inject `state.reads` 切片投喂回归（父工作空间坐标 + 管道级授权区）。

漏声明形态（2026-11-12 修复）：
1. `task.ws_meta` 未声明（只声明了会话域 `ws_meta`）→ 切片后任务管道的父
   工作空间坐标恒读不到 → `task_submit` 调用的 `parent_ws_meta` 恒 None，
   子任务失去父坐标（workspace_lifecycle 的共享决策退回聚合读可见性赌运气）。
2. `task.authorized_write_zones` / `task.authorized_read_zones` 未声明（读键
   取 `zone_policy.STATE_KEY*` 常量单源）→ 切片后恒空 → 用户批准的管道级
   授权区注入不到工具参数，被批准的操作仍被判越界。

本用例按 manifest 声明切片投喂（与生产 invoker 同形）断言两条注入成立。
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

for _d in (str(_DIR), str(_SHARED)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

import zone_policy  # noqa: E402
from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_plugin_module() -> Any:
    mod_name = "tool_args_inject_state_reads_slice_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_plugin_module()
ToolArgsInjectPlugin = _MOD.ToolArgsInjectPlugin


MANIFEST_READS = manifest_reads(_DIR)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "core_type": "tool_execute",
        "raw_tool_calls": [{"name": "memory", "args": {}}],
        "session_id": "s-1",
        "user_id": "u-1",
        "task.id": "t-1",
        "pipeline_id": "t-1",
        "agent_level": "L2",
        "workspace": "/ws/root",
    }
    state.update(overrides)
    return state


def _inject(state: dict[str, Any]) -> dict[str, Any]:
    """切片投喂 → 执行 → 返回首个工具调用的注入后参数。"""
    fed = slice_feed(state, MANIFEST_READS)
    ctx = PluginContext(state=fed, config={})
    result = asyncio.run(ToolArgsInjectPlugin(config={})._do_work(ctx))
    return dict(result["raw_tool_calls"][0]["args"])


class TestDeclaration:
    def test_slice_carried_keys_declared(self) -> None:
        declared = visible_keys(MANIFEST_READS)
        for key in ("task.ws_meta", zone_policy.STATE_KEY, zone_policy.STATE_KEY_READ):
            assert key in declared, f"{key} 未在 state.reads 声明 → 切片后恒缺省"


class TestParentWorkspaceUnderSliceFeed:
    def test_task_submit_gets_parent_ws_meta_from_task_pipeline(self) -> None:
        """任务管道父坐标（task.ws_meta）必须注入 task_submit 的 parent_ws_meta。"""
        args = _inject(
            _state(
                raw_tool_calls=[{"name": "task_submit", "args": {}}],
                **{"task.ws_meta": {"path": "/parent/ws", "mode": "worktree"}},
            )
        )

        assert args["parent_ws_meta"] == {"path": "/parent/ws", "mode": "worktree"}

    def test_missing_parent_ws_meta_overrides_with_none(self) -> None:
        """无父坐标时以 None 覆盖（伪造坐标不放行）。"""
        args = _inject(_state(raw_tool_calls=[{"name": "task_submit", "args": {}}]))

        assert args["parent_ws_meta"] is None


class TestAuthorizedZonesUnderSliceFeed:
    def test_pipeline_granted_zones_injected_under_slice_feed(self) -> None:
        args = _inject(
            _state(
                **{
                    zone_policy.STATE_KEY: '["/zone/w1"]',
                    zone_policy.STATE_KEY_READ: '["/zone/r1"]',
                }
            )
        )

        assert args["authorized_zones"] == '["/zone/w1"]'
        assert args["authorized_read_zones"] == '["/zone/r1"]'

    def test_no_grant_means_no_zone_args(self) -> None:
        args = _inject(_state())

        assert "authorized_zones" not in args
        assert "authorized_read_zones" not in args
