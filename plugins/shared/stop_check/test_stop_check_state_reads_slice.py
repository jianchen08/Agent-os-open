# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""stop_check `state.reads` 切片投喂回归（轮次预算锚 + 任务终态缓存键）。

两处静默降级（2026-11-12 修复）：
1. **轮次预算锚失效**：`track.llm_usage` 未声明 → 切片后恒读不到 → 轮次锚
   退回引擎 `iteration`（拆分管道形态下该锚把预算隐式减半/失效），
   「按 LLM 调用计」的用户裁定（2026-10-17）在切片投喂下不存在。
2. **任务终态缓存读失效**：`task_status`（裸下划线缓存键，恢复链 overlay
   写入）未声明 → 切片后恒空 → `_cached_terminal_hit` 恒假，终态判据只剩
   聚合读一条腿（诊断打点 source 也永远报 aggregated）。

本用例按 manifest 声明切片投喂（与生产 invoker 同形）断言两条行为成立。
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

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.testing.state_slice import (  # noqa: E402
    manifest_reads,
    slice_feed,
    visible_keys,
)


def _load_plugin_module() -> Any:
    mod_name = "stop_check_state_reads_slice_test"
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
StopCheckPlugin = _MOD.StopCheckPlugin


MANIFEST_READS = manifest_reads(_DIR)


def _fed(state: dict[str, Any]) -> dict[str, Any]:
    return slice_feed(state, MANIFEST_READS)


def _run(state: dict[str, Any], **config: Any) -> dict[str, Any]:
    plugin = StopCheckPlugin(config)
    ctx = PluginContext(state=_fed(state), config=config)
    result = asyncio.run(plugin.execute(ctx))
    return dict(result.state_updates)


def _task_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "pipeline_id": "p-1",
        "run_id": "run-1",
        "task.id": "p-1",
        "task.status": "running",
        "iteration": 0,
        "raw_tool_calls": [],
        "messages": [{"role": "user", "content": "做任务"}],
        "tool_results": [],
    }
    state.update(overrides)
    return state


class TestDeclaration:
    def test_slice_carried_keys_declared(self) -> None:
        declared = visible_keys(MANIFEST_READS)
        for key in ("track.llm_usage", "task_status"):
            assert key in declared, f"{key} 未在 state.reads 声明 → 切片后恒缺省"


class TestRoundBudgetAnchorUnderSliceFeed:
    """轮次预算锚：LLM 调用次数在场时按它判上限（用户裁定 2026-10-17）。"""

    def test_llm_usage_anchor_trips_budget_under_slice_feed(self) -> None:
        state = _task_state(**{"track.llm_usage": {"total_calls": 5}})

        updates = _run(state, max_iterations=4)

        assert updates.get("router.stop_reason") == "max_iterations"
        content = updates["messages"]["_ops"][0]["msg"]["content"]
        assert "按LLM 调用计" in content, f"轮次锚未取 LLM 调用次数：{content}"
        assert updates["task.status"] == "failed"

    def test_iteration_anchor_still_used_without_usage(self) -> None:
        """无用量管道回退迭代锚（切片修复不得改变旧形态语义）。"""
        updates = _run(_task_state(iteration=9), max_iterations=4)

        assert updates.get("router.stop_reason") == "max_iterations"
        content = updates["messages"]["_ops"][0]["msg"]["content"]
        assert "按迭代计" in content, content


class TestCachedTerminalStatusUnderSliceFeed:
    """任务终态缓存键：恢复链 overlay 写入的 `task_status` 必须可见。"""

    def test_cached_terminal_status_stops_pipeline_under_slice_feed(self) -> None:
        updates = _run(_task_state(task_status="completed"))

        assert updates.get("ended") is True
        assert updates.get("router.stop_reason") == "task_completed"

    def test_running_status_does_not_stop(self) -> None:
        updates = _run(_task_state(task_status="running"))

        assert "ended" not in updates
