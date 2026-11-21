# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""workspace_lifecycle sidecar 实例复用契约（per-pipeline state 隔离）。

同 sidecar 实例被多管道并发复用，async handler 不串行：``_last_state`` 曾以
实例字段承载「本管道 state」，A 管道在 await 处让出后会被 B 管道的 execute
覆盖——task_tree 父链兜底随即读到别的任务身份（父工作空间定位串道）。
现由 ContextVar 承载（每条 asyncio 任务各持独立 context 副本）。

锁定：交叠执行下 A 管道的 ``get_task`` 仍读 A 自己的 task.id / lineage；
execute 返回后绑定必然复位（不残留到下一轮/其他调用）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[0]  # plugins/shared
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

_MOD_NAME = "workspace_lifecycle_sidecar_isolation_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPipelineStateBinding:
    def test_interleaved_executes_read_own_state(self) -> None:
        """A 在 init 体让出，B 整轮跑完；A 收尾时 task_tree 必须仍读 A 的 state。"""
        mod = _load_module()
        mod._state_reader = None
        mod._state_rows_cache = []  # 无聚合行 → 走本 state 的 lineage 兜底
        plugin = mod.WorkspaceLifecyclePlugin(config={})

        release = asyncio.Event()
        entered_a = asyncio.Event()
        observed: dict[str, Any] = {}

        async def fake_bootstrap(ctx: Any) -> Any:
            task_id = str(ctx.state.get("task.id") or "")
            if task_id == "task-a":
                entered_a.set()
                await release.wait()
            task = mod._ExecutionContextTaskTree(plugin).get_task(task_id)
            observed[task_id] = None if task is None else task.parent_task_id
            return mod.PluginResult()

        plugin._bootstrap = fake_bootstrap  # type: ignore[method-assign]

        ctx_a = mod.PluginContext(
            state={
                "pipeline_id": "task-a",
                "task.id": "task-a",
                "current_phase": "init",
                "lineage.parent_pipeline_id": "parent-a",
            },
            config={},
        )
        ctx_b = mod.PluginContext(
            state={
                "pipeline_id": "task-b",
                "task.id": "task-b",
                "current_phase": "init",
                "lineage.parent_pipeline_id": "parent-b",
            },
            config={},
        )

        async def scenario() -> None:
            task_a = asyncio.create_task(plugin.execute(ctx_a))
            await entered_a.wait()
            await plugin.execute(ctx_b)
            release.set()
            await task_a

        _run(scenario())

        assert observed == {"task-b": "parent-b", "task-a": "parent-a"}, (
            f"交叠执行串台：{observed}（A 读到了 B 的 task.id → 父链兜底错管道）"
        )
        # execute 收尾复位：绑定不得残留
        assert mod._current_pipeline_state.get() is None

    def test_prepare_task_workspace_binds_its_own_state(self) -> None:
        """出生期 prepare 路径同样按 ContextVar 绑定（无实例字段残留）。"""
        mod = _load_module()
        mod._state_reader = None
        mod._state_rows_cache = []
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        seen: list[Any] = []

        async def fake_bootstrap(ctx: Any) -> Any:
            task = mod._ExecutionContextTaskTree(plugin).get_task(
                str(ctx.state.get("task.id") or "")
            )
            seen.append(None if task is None else task.parent_task_id)
            return mod.PluginResult()

        plugin._bootstrap = fake_bootstrap  # type: ignore[method-assign]

        _run(
            plugin.prepare_task_workspace(
                pipeline_id="task-prep",
                state={
                    "task.id": "task-prep",
                    "lineage.parent_pipeline_id": "parent-prep",
                },
            )
        )

        assert seen == ["parent-prep"]
        assert mod._current_pipeline_state.get() is None

    def test_exit_and_main_phases_dispatch_with_binding(self) -> None:
        """exit 走 _finalize、其它循环体零产出；两条路径同样复位绑定。"""
        mod = _load_module()
        mod._state_reader = None
        mod._state_rows_cache = []
        plugin = mod.WorkspaceLifecyclePlugin(config={})
        seen: list[str] = []

        async def fake_finalize(ctx: Any) -> Any:
            seen.append("finalize")
            return mod.PluginResult()

        async def fake_bootstrap(ctx: Any) -> Any:  # noqa: ARG001
            seen.append("bootstrap")
            return mod.PluginResult()

        plugin._finalize = fake_finalize  # type: ignore[method-assign]
        plugin._bootstrap = fake_bootstrap  # type: ignore[method-assign]

        exit_ctx = mod.PluginContext(
            state={"task.id": "task-exit", "current_phase": "exit"},
            config={},
        )
        _run(plugin.execute(exit_ctx))
        assert seen == ["finalize"]
        assert mod._current_pipeline_state.get() is None

        main_ctx = mod.PluginContext(
            state={"task.id": "task-main", "current_phase": "main"},
            config={},
        )
        result = _run(plugin.execute(main_ctx))
        assert seen == ["finalize"], "main 循环体生命周期插件不参与"
        assert result.state_updates == {}
        assert mod._current_pipeline_state.get() is None
