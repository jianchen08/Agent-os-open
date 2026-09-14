# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 内部模块缺口补测（tool.py 1298-1299 / _executor.py 35 / server.py 111）。

覆盖：
1. tool.py `_ensure_criteria_from_state` 读 state 抛异常的兜底 except（1298-1299）：
   静默返回、不抛、task.metadata 未被改动；
2. _executor.py 的 `sys.path.insert(0, _SHARED_ROOT)` 自举（35 行）：以唯一模块名
   装载前把 plugins/shared 从 sys.path 移除，装载后应被自举回、`state_fields` 可用；
3. server.py `task_evaluate` handler 的兜底返回（111 行）：result 无 to_dict 且
   success=False → {"error": result.error}。

外部面打桩：state 读面（跨进程 capability）以抛异常替身注入；tool 模块面以
无 to_dict 的假模块注入。模块装载/dispatch 走真实实现。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[1] / "system" / "tasks"
_SHARED_ROOT = _TE_DIR.parents[1]  # plugins/shared

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_ROOT):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module(name: str, file_name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TE_DIR / file_name)
    assert spec is not None and spec.loader is not None, f"cannot load {file_name}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool_mod() -> Any:
    return _load_module("task_eval_tool_gaps_under_test", "tool.py")


class _TaskStub:
    """评估兜底只需 id + metadata 两个属性（duck-typing 面）。"""

    def __init__(self, metadata: dict[str, Any] | None) -> None:
        self.id = "pipe-abcd1234efgh"
        self.metadata = metadata


class TestEnsureCriteriaFromStateReadFailure:
    """1298-1299 行：读 state 抛异常 → 兜底静默返回（既有降级路径）。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("metadata", [None, {}])
    async def test_read_exception_is_swallowed_and_metadata_untouched(
        self, tool_mod: Any, monkeypatch: pytest.MonkeyPatch, metadata: dict | None
    ) -> None:
        async def _boom(self: Any) -> list[dict[str, Any]]:
            raise RuntimeError("pipeline-state 通道断")

        monkeypatch.setattr(tool_mod.TaskEvaluateTool, "_read_state_rows", _boom)
        task = _TaskStub(metadata)
        tool = tool_mod.TaskEvaluateTool()

        await tool._ensure_criteria_from_state(task)  # 不抛

        assert task.metadata == metadata
        assert not (task.metadata or {}).get("acceptance_criteria")

    @pytest.mark.asyncio
    async def test_criteria_backfilled_from_state_rows(
        self, tool_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照组：读面正常时从 state 行补齐 acceptance_criteria（真实分支）。"""

        async def _rows(self: Any) -> list[dict[str, Any]]:
            return [
                {"pipeline_id": "other", "task.acceptance_criteria": {"m1": {}}},
                {
                    "pipeline_id": "pipe-abcd1234efgh",
                    "task.acceptance_criteria": '{"m2": {"threshold": 1}}',
                },
            ]

        monkeypatch.setattr(tool_mod.TaskEvaluateTool, "_read_state_rows", _rows)
        task = _TaskStub(None)

        await tool_mod.TaskEvaluateTool()._ensure_criteria_from_state(task)

        assert task.metadata == {"acceptance_criteria": {"m2": {"threshold": 1}}}

    @pytest.mark.asyncio
    async def test_existing_criteria_short_circuits_reader(
        self, tool_mod: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """对照组：metadata 已声明指标 → 根本不读 state（零 IO）。"""
        calls: list[str] = []

        async def _rows(self: Any) -> list[dict[str, Any]]:
            calls.append("read")
            return []

        monkeypatch.setattr(tool_mod.TaskEvaluateTool, "_read_state_rows", _rows)
        task = _TaskStub({"acceptance_criteria": {"m9": {}}})

        await tool_mod.TaskEvaluateTool()._ensure_criteria_from_state(task)

        assert calls == []
        assert task.metadata == {"acceptance_criteria": {"m9": {}}}


class TestExecutorSharedRootBootstrap:
    """_executor.py 35 行：plugins/shared 不在 sys.path 时自举注入。"""

    @staticmethod
    def _strip_shared_root() -> None:
        target = os.path.abspath(str(_SHARED_ROOT))
        for entry in list(sys.path):
            if entry and os.path.abspath(entry) == target:
                sys.path.remove(entry)

    def test_executor_bootstraps_shared_root_for_state_fields(self, monkeypatch) -> None:
        saved_state_fields = sys.modules.pop("state_fields", None)
        original_path = list(sys.path)
        try:
            self._strip_shared_root()
            assert not any(
                entry and os.path.abspath(entry) == os.path.abspath(str(_SHARED_ROOT))
                for entry in sys.path
            )
            mod_name = "task_eval_executor_bootstrap_under_test"
            sys.modules.pop(mod_name, None)
            spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "_executor.py")
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)

            assert any(
                entry and os.path.abspath(entry) == os.path.abspath(str(_SHARED_ROOT))
                for entry in sys.path
            )
            assert module.state_fields.as_dict("{\"a\": 1}", field="x") == {"a": 1}
        finally:
            sys.path[:] = original_path
            sys.modules.pop("state_fields", None)
            if saved_state_fields is not None:
                sys.modules["state_fields"] = saved_state_fields

    def test_executor_load_does_not_duplicate_shared_root(self, monkeypatch) -> None:
        """对照：shared root 已在 sys.path 时不自举重复（幂等守卫）。"""
        original_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location(
                "task_eval_executor_idempotent_under_test", _TE_DIR / "_executor.py"
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules["task_eval_executor_idempotent_under_test"] = module
            spec.loader.exec_module(module)

            target = os.path.abspath(str(_SHARED_ROOT))
            matches = [e for e in sys.path if e and os.path.abspath(e) == target]
            assert len(matches) == 1
            assert module.state_fields is sys.modules["state_fields"]
        finally:
            sys.path[:] = original_path


class TestServerHandlerFallbackReturn:
    """server.py 111 行：无 to_dict 的 result + success=False → {"error"}。"""

    @staticmethod
    def _load_server() -> Any:
        name = "task_eval_server_gaps_under_test"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, _TE_DIR / "server.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    @pytest.mark.asyncio
    async def test_plain_failure_object_returns_error_dict(self, monkeypatch) -> None:
        import types

        class _PlainFailure:
            """无 to_dict（非 ToolExecutionResult 实现）的失败结果。"""

            success = False
            error = "评估执行器未注入"
            output = {"ignored": True}

        class _FakeTool:
            async def execute(self, inputs: dict[str, Any]) -> Any:
                return _PlainFailure()

        fake_mod = types.ModuleType("tool")
        fake_mod.TaskEvaluateTool = _FakeTool
        server_mod = self._load_server()
        monkeypatch.setattr(server_mod, "tool_mod", fake_mod)

        out = await asyncio.wait_for(server_mod.task_evaluate(action="auto_complete"), timeout=5)

        assert out == {"error": "评估执行器未注入"}

    @pytest.mark.asyncio
    async def test_envelope_capable_result_still_preferred(self, monkeypatch) -> None:
        """对照组：带 to_dict 的结果走信封路径（兜底不抢占正式契约）。"""
        import types

        from agentos_plugin_sdk import create_failure_result

        class _FakeTool:
            async def execute(self, inputs: dict[str, Any]) -> Any:
                return create_failure_result(error="信封路径", error_code="X")

        fake_mod = types.ModuleType("tool")
        fake_mod.TaskEvaluateTool = _FakeTool
        server_mod = self._load_server()
        monkeypatch.setattr(server_mod, "tool_mod", fake_mod)

        out = await server_mod.task_evaluate(action="auto_complete")

        assert out["success"] is False
        assert out["error"] == "信封路径"
        assert out["error_code"] == "X"
