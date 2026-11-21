# @feature: FP-MIGR 任务提交参数解耦 | @ci: python-coverage
"""task_submit execution_profile（MXC 执行档）解析测试。

覆盖：
1. 显式值合法/非法（INVALID_EXECUTION_PROFILE）；
2. 平台门：native-win 在非 Windows 拒绝（PROFILE_PLATFORM_UNSUPPORTED），
   Windows 放行；
3. 缺省 = linux-bash；父任务/接续源 task.execution_profile 继承（state 聚合
   不可用按缺省，不阻断提交）；
4. 普通子任务显式指定被闸门拒绝（SUBTASK_INHERITS_PARAMS）；
5. execution_context 与初始输入键装配（execution.profile / 完备性左值）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SDK_DIR = _REPO_ROOT / "plugins" / "sdk" / "src"
_TASKS_DIR = _REPO_ROOT / "plugins" / "shared" / "tasks"
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared"


@pytest.fixture(scope="module", autouse=True)
def _module_sys_path() -> Any:
    added: list[str] = []
    for _p in (_SDK_DIR, _TASKS_DIR, _TOOL_DIR, _SYSTEM_DIR):
        s = str(_p)
        if s not in sys.path:
            sys.path.insert(0, s)
            added.append(s)
    yield
    for s in added:
        sys.path.remove(s)


@pytest.fixture(scope="module")
def tool_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "task_submit_tool_execution_profile_test_mod", _TOOL_DIR / "tool.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tool(tool_module: Any) -> Any:
    return tool_module.TaskSubmitTool()


class TestResolveExecutionProfile:
    def test_explicit_valid(self, tool_module: Any) -> None:
        inputs: dict[str, Any] = {"execution_profile": "linux-bash"}
        assert _run(_tool(tool_module)._resolve_execution_profile(inputs, None)) is None
        assert inputs["_resolved_execution_profile"] == "linux-bash"

    def test_invalid_rejected(self, tool_module: Any) -> None:
        inputs: dict[str, Any] = {"execution_profile": "windows-sandbox"}
        fail = _run(_tool(tool_module)._resolve_execution_profile(inputs, None))
        assert fail is not None
        assert fail.error_code == "INVALID_EXECUTION_PROFILE"

    def test_default_is_linux_bash(self, tool_module: Any) -> None:
        inputs: dict[str, Any] = {}
        assert _run(_tool(tool_module)._resolve_execution_profile(inputs, None)) is None
        assert inputs["_resolved_execution_profile"] == "linux-bash"

    def test_native_win_rejected_on_non_windows(
        self, tool_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tool_module.os, "name", "posix")
        inputs: dict[str, Any] = {"execution_profile": "native-win"}
        fail = _run(_tool(tool_module)._resolve_execution_profile(inputs, None))
        assert fail is not None
        assert fail.error_code == "PROFILE_PLATFORM_UNSUPPORTED"

    def test_native_win_allowed_on_windows(
        self, tool_module: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tool_module.os, "name", "nt")
        inputs: dict[str, Any] = {"execution_profile": "native-win"}
        assert _run(_tool(tool_module)._resolve_execution_profile(inputs, None)) is None
        assert inputs["_resolved_execution_profile"] == "native-win"

    def test_inherits_from_parent_row(self, tool_module: Any) -> None:
        tool = _tool(tool_module)

        async def fake_rows() -> list[dict[str, Any]]:
            return [{"pipeline_id": "p1", "task.id": "p1", "task.execution_profile": "native-win"}]

        tool._read_state_rows = fake_rows  # type: ignore[method-assign]
        inputs: dict[str, Any] = {}
        assert _run(tool._resolve_execution_profile(inputs, "p1")) is None
        assert inputs["_resolved_execution_profile"] == "native-win"

    def test_inherits_from_inherit_source(self, tool_module: Any) -> None:
        tool = _tool(tool_module)

        async def fake_rows() -> list[dict[str, Any]]:
            return [{"pipeline_id": "src", "task.execution_profile": "native-win"}]

        tool._read_state_rows = fake_rows  # type: ignore[method-assign]
        inputs: dict[str, Any] = {"inherit_from": "src"}
        assert _run(tool._resolve_execution_profile(inputs, None)) is None
        assert inputs["_resolved_execution_profile"] == "native-win"

    def test_state_unavailable_defaults(self, tool_module: Any) -> None:
        tool = _tool(tool_module)

        async def fake_rows() -> None:
            return None

        tool._read_state_rows = fake_rows  # type: ignore[method-assign]
        inputs: dict[str, Any] = {}
        assert _run(tool._resolve_execution_profile(inputs, "p1")) is None
        assert inputs["_resolved_execution_profile"] == "linux-bash"


class TestSubtaskGate:
    def test_subtask_rejects_execution_profile(self, tool_module: Any) -> None:
        tool = _tool(tool_module)
        fail = tool._gate_subtask_param_inheritance(
            {"execution_profile": "native-win"}, "parent-1", ""
        )
        assert fail is not None
        assert fail.error_code == "SUBTASK_INHERITS_PARAMS"

    def test_root_allows_execution_profile(self, tool_module: Any) -> None:
        tool = _tool(tool_module)
        assert (
            tool._gate_subtask_param_inheritance(
                {"execution_profile": "native-win"}, None, ""
            )
            is None
        )


class TestExecutionContextAssembly:
    def test_execution_context_carries_profile(self, tool_module: Any) -> None:
        tool = _tool(tool_module)
        ec = tool._build_execution_context(
            {"_resolved_execution_profile": "native-win"}, workspace_explicit=False
        )
        assert ec["execution"] == {"profile": "native-win"}
        assert ec["isolation"] == {"level": "isolated"}
        assert ec["workspace"]["source_path"] == ""

    def test_execution_context_without_profile_has_no_block(self, tool_module: Any) -> None:
        tool = _tool(tool_module)
        ec = tool._build_execution_context({}, workspace_explicit=False)
        assert "execution" not in ec

    def test_initial_input_keys_include_profile(self, tool_module: Any) -> None:
        keys = tool_module.TaskSubmitTool._assemble_initial_input_keys(
            {"execution_profile": "linux-bash"}, None
        )
        assert "execution_profile" in keys
