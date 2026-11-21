# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""environment_lifecycle `state.reads` 切片投喂回归（容器名按名销毁）。

漏声明形态（2026-11-12 修复）：`isolation.container_name` 未进 manifest
`state.reads` → 切片后恒读不到 → `isolation.destroy_env` 收到空容器名，
只能靠服务端内存登记反查。服务重启后登记为空（D6 明令「杜绝登记丢失=泄漏」），
任务容器按名删除这条腿在切片投喂下不存在，容器泄漏无告警。

本用例按 manifest 声明切片投喂（与生产 invoker 同形）断言按名销毁成立。
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
    mod_name = "environment_lifecycle_state_reads_slice_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


MANIFEST_READS = manifest_reads(_DIR)


class _RecordingDestroyCaller:
    """记录销毁调用的假 caller（外部子系统隔离）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tool_name: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool_name, dict(args)))
        return {"success": True}


def _exit_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "current_phase": "exit",
        "environment_basis": {"level": "isolated", "resolved": True},
        "task.id": "t-1",
        "pipeline_id": "t-1",
        "isolation.container_name": "agentos-task-t-1",
    }
    state.update(overrides)
    return state


def _run_exit(state: dict[str, Any]) -> tuple[dict[str, Any], _RecordingDestroyCaller]:
    mod = _load_plugin_module()
    caller = _RecordingDestroyCaller()
    mod.set_destroy_caller(caller)
    fed = slice_feed(state, MANIFEST_READS)
    result = asyncio.run(mod.EnvironmentLifecyclePlugin(config={}).execute(PluginContext(state=fed)))
    return dict(result.state_updates), caller


class TestDeclaration:
    def test_container_name_declared(self) -> None:
        assert "isolation.container_name" in visible_keys(MANIFEST_READS), (
            "isolation.container_name 未声明 → 切片后销毁调用恒收空容器名"
        )


class TestReleaseUnderSliceFeed:
    def test_destroy_called_with_container_name_under_slice_feed(self) -> None:
        updates, caller = _run_exit(_exit_state())

        assert updates == {"environment_released": True}
        assert len(caller.calls) == 1
        tool_name, args = caller.calls[0]
        assert tool_name == "isolation.destroy_env"
        assert args["container_name"] == "agentos-task-t-1", (
            f"按名销毁腿失效（读点漏声明时恒空）：{args}"
        )
        assert args["task_id"] == "t-1"

    def test_main_session_without_container_name_still_releases(self) -> None:
        """无容器名的任务（容器按需未创建）照常释放，不因空名阻塞。"""
        state = _exit_state()
        state.pop("isolation.container_name")

        updates, caller = _run_exit(state)

        assert updates == {"environment_released": True}
        assert caller.calls[0][1]["container_name"] == ""
