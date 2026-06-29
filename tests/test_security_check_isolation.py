"""隔离判断单元测试 — security_check 的 _is_isolated 直读 ws_meta.mode。

验证隔离真相判断契约（纯单元测试）：

1. worktree/project_root/branch → 已隔离（放行），即使文件工具 provider=host
2. shared(host 裸操作)/plain → 未隔离（危险工具需审批）
3. docker 容器 → 已隔离（保留原 provider 判定）
4. 无 task_id（主管道）/ ws_meta 缺失 → 未隔离（保守）
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from plugins.input.security_check.plugin import SecurityCheckPlugin


def _make_ctx(state: dict[str, Any], task: Any | None = None) -> Any:
    """构造 PluginContext：state + task_service（返回给定 task）。"""
    ctx = MagicMock()
    ctx.state = state
    if task is not None:
        task_service = MagicMock()
        task_service.get_task.return_value = task
        ctx.get_service.side_effect = lambda name: task_service if name == "task_service" else (_ for _ in ()).throw(KeyError(name))
    else:
        ctx.get_service.side_effect = lambda name: (_ for _ in ()).throw(KeyError(name))
    return ctx


def _make_task(ws_mode: str | None) -> Any:
    """构造带 ws_meta 的 task；ws_mode=None 表示无 ws_meta。"""
    task = MagicMock()
    task.metadata = {"ws_meta": {"mode": ws_mode}} if ws_mode else {}
    return task


class TestIsIsolated:
    """_is_isolated 应直读 ws_meta.mode 判断隔离，不靠 provider 反推。"""

    @pytest.mark.parametrize("mode", ["worktree", "project_root", "branch"])
    def test_isolated_ws_modes_are_isolated_even_with_host_provider(self, mode: str) -> None:
        """worktree 等隔离副本下，文件工具 provider=host 也应判为已隔离。

        这是卡死根因：原逻辑只认 provider==docker 才放行，
        worktree（provider=host）被误判为裸 host 强行弹审批。
        """
        task = _make_task(mode)
        ctx = _make_ctx({"task_id": "t1"}, task)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, [{"provider": "host"}]) is True

    @pytest.mark.parametrize("mode", ["shared", "plain"])
    def test_bare_ws_modes_not_isolated(self, mode: str) -> None:
        """shared(host 直接操作项目目录)/plain 是裸操作，未隔离需审批。"""
        task = _make_task(mode)
        ctx = _make_ctx({"task_id": "t2"}, task)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, []) is False

    def test_docker_container_isolated(self) -> None:
        """docker 容器（provider 全为 docker）保留原判定为已隔离。"""
        ctx = _make_ctx({"task_id": "t3"}, _make_task(None))
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, [{"provider": "docker"}]) is True

    def test_mixed_docker_host_not_isolated_by_provider(self) -> None:
        """混合 provider（含 host）时按 ws_meta.mode 兜底判断。"""
        task = _make_task("shared")
        ctx = _make_ctx({"task_id": "t4"}, task)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, [{"provider": "docker"}, {"provider": "host"}]) is False

    def test_main_pipeline_no_task_id_not_isolated(self) -> None:
        """主管道无 task_id → 无法判断隔离，保守判为未隔离。"""
        ctx = _make_ctx({}, None)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, []) is False

    def test_missing_ws_meta_not_isolated(self) -> None:
        """task 无 ws_meta → 保守判为未隔离。"""
        task = MagicMock()
        task.metadata = {}
        ctx = _make_ctx({"task_id": "t5"}, task)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, []) is False

    def test_task_service_unavailable_not_isolated(self) -> None:
        """task_service 不可用 → 保守判为未隔离（按裸操作审批）。"""
        ctx = MagicMock()
        ctx.state = {"task_id": "t6"}
        ctx.get_service.side_effect = KeyError("task_service")
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, []) is False

    def test_subtask_inherited_isolated_is_isolated(self) -> None:
        """子任务继承了父任务的隔离副本（ws_meta.inherited_isolated=True）→ 应放行。

        卡死根因：子任务物理上在父 worktree 目录里跑，但 mode 被钉成 shared，
        导致危险工具一律弹审批。修复后子任务 ws_meta 带 inherited_isolated 标记，
        _is_isolated 据此判为隔离，与父任务同等放行。
        """
        task = MagicMock()
        task.metadata = {"ws_meta": {"mode": "shared", "inherited_isolated": True}}
        ctx = _make_ctx({"task_id": "t7"}, task)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, [{"provider": "host"}]) is True

    def test_subtask_without_inherited_marker_not_isolated(self) -> None:
        """子任务未继承隔离标记（host 父任务，mode=shared 无标记）→ 未隔离需审批。

        host 父任务本身不隔离，其子任务也不应放行，保持裸操作审批语义。
        """
        task = MagicMock()
        task.metadata = {"ws_meta": {"mode": "shared"}}
        ctx = _make_ctx({"task_id": "t8"}, task)
        plugin = SecurityCheckPlugin()
        assert plugin._is_isolated(ctx, []) is False
