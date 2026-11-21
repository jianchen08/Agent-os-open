# @feature: FP-0.2.二 项目状态门 | @ci: python-coverage
"""task_submit project.* 服务消费面测试（T6a：plan 态闸/mode 闸/项目路径解析）。

锁定契约：服务桩直连真实登记簿实现（关键路径走真实依赖，只桩 RPC 层）；
服务不可用 = fail-closed 失败信封（登记读面不可降级）；plan 态白名单基线
自 mode_keys（收编后单源）。
"""

from __future__ import annotations

import os  # noqa: F401
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

# 共享根自举（plugins/shared/ —— task_submit tool.py 与 project_manager 包所在）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

import tool as _tool_mod  # noqa: E402
from _root_api import collect_planning_executors  # noqa: E402
from project_manager import project_registry as pr  # noqa: E402
from tool import TaskSubmitTool  # noqa: E402


@pytest.fixture
def reg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """隔离登记目录 + 工作空间基目录（不落仓库根 .ai_workspaces）。"""
    ws_base = tmp_path / "ws"
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks_data"))
    monkeypatch.setattr("project_manager.project_registry.workspace_base_dir", lambda: ws_base)
    return {"ws_base": ws_base, "tmp": tmp_path}


def _install_real_project_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """project.* 服务桩：直连真实登记簿实现（只桩跨插件 RPC 层）。"""

    class _Handle:
        async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
            if method == "get":
                project = pr.ProjectRegistry().get(str(args.get("project_id") or ""))
                return (
                    {"found": True, "project": asdict(project)}
                    if project is not None
                    else {"found": False}
                )
            if method == "list_paths":
                return {"paths": pr.load_project_paths()}
            raise AssertionError(f"unexpected method: {method}")

    monkeypatch.setattr(_tool_mod, "_project_service", lambda method, args: _Handle().call(method, args))


def _install_broken_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """故障桩：调用抛错（服务不可用形态）。"""

    async def _boom(method: str, args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("project_manager sidecar down")

    monkeypatch.setattr(_tool_mod, "_project_service", _boom)


def _mk_plan_project(reg_env: dict[str, Path], path: Path) -> str:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    project, _ = pr.ensure_project_registered("闸项目", str(target))
    return project.id


class TestPlanStateGate:
    async def test_plan_state_blocks_non_whitelisted_executor(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """plan 态项目：白名单外执行者 → PROJECT_PLAN_LOCKED 失败信封。"""
        _install_real_project_service(monkeypatch)
        pid = _mk_plan_project(reg_env, reg_env["tmp"] / "plan_proj")
        fail = await TaskSubmitTool._gate_project_plan_state(pid, "agent", "code_writer_agent")
        assert fail is not None
        assert fail.error_code == "PROJECT_PLAN_LOCKED"

    async def test_plan_state_allows_whitelisted_executor(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """plan 态项目：系统基线执行者放行（返回 None = 不过闸）。"""
        _install_real_project_service(monkeypatch)
        pid = _mk_plan_project(reg_env, reg_env["tmp"] / "plan_proj_ok")
        baseline = "research_agent"
        assert baseline in {"research_agent", "environment_setup_agent"}
        fail = await TaskSubmitTool._gate_project_plan_state(pid, "agent", baseline)
        assert fail is None

    async def test_running_state_not_gated(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """running 态项目：执行类派发不受 plan 闸约束。"""
        _install_real_project_service(monkeypatch)
        target = reg_env["tmp"] / "running_proj"
        target.mkdir(parents=True)
        project, _ = pr.ensure_project_registered("跑项目", str(target))
        pr.transition_workflow_state(project, "running")
        pr.ProjectRegistry().save(project)
        fail = await TaskSubmitTool._gate_project_plan_state(project.id, "agent", "code_writer_agent")
        assert fail is None

    async def test_service_unavailable_fail_closed(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务不可用 → fail-closed 失败信封（不静默放行 plan 闸）。"""
        _install_broken_service(monkeypatch)
        fail = await TaskSubmitTool._gate_project_plan_state("proj-1", "agent", "code_writer_agent")
        assert fail is not None
        assert fail.error_code == "PROJECT_SERVICE_UNAVAILABLE"


class TestModeGate:
    async def test_mode_outside_registered_tags_rejected(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 mode 不在项目登记集合 → MODE_NOT_IN_PROJECT_TAGS（集合可见）。"""
        _install_real_project_service(monkeypatch)
        target = reg_env["tmp"] / "tagged_proj"
        target.mkdir(parents=True)
        project, _ = pr.ensure_project_registered(
            "标签项目", str(target), mode_tags=["coding"], default_mode="coding"
        )
        fail = await TaskSubmitTool._gate_mode_registration(project.id, "writing", "general_agent")
        assert fail is not None
        assert fail.error_code == "MODE_NOT_IN_PROJECT_TAGS"
        assert "coding" in (fail.error or "")

    async def test_mode_in_registered_tags_passes(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """显式 mode ∈ 登记集合 → 不过闸。"""
        _install_real_project_service(monkeypatch)
        target = reg_env["tmp"] / "tagged_ok"
        target.mkdir(parents=True)
        project, _ = pr.ensure_project_registered(
            "标签项目OK", str(target), mode_tags=["coding"], default_mode="coding"
        )
        assert await TaskSubmitTool._gate_mode_registration(project.id, "coding", "general_agent") is None

    async def test_service_unavailable_fail_closed(
        self, reg_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """服务不可用 → fail-closed 失败信封。"""
        _install_broken_service(monkeypatch)
        fail = await TaskSubmitTool._gate_mode_registration("proj-1", "coding", "general_agent")
        assert fail is not None
        assert fail.error_code == "PROJECT_SERVICE_UNAVAILABLE"


class TestPlanWhitelistBaselineSource:
    def test_baseline_now_lives_in_mode_keys(self) -> None:
        """系统基线收编 mode_keys（T6a）：与 planning_executors 聚合面同居单源。"""
        from _root_api import PLAN_DISPATCH_WHITELIST
        from mode_keys import PLAN_DISPATCH_WHITELIST as _from_mode_keys

        assert PLAN_DISPATCH_WHITELIST is _from_mode_keys
        assert "research_agent" in PLAN_DISPATCH_WHITELIST
        assert "environment_setup_agent" in PLAN_DISPATCH_WHITELIST
        # 注册贡献并集形态仍成立（空注册环境下基线即全集）
        assert PLAN_DISPATCH_WHITELIST | collect_planning_executors() >= PLAN_DISPATCH_WHITELIST
