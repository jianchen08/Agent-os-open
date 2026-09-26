# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""download workspace_aware 缺口补测（策略层不可用的 fail-closed 三处缺口）。

覆盖：
1. `_get_policy_manager` 的 SDK 导入失败 except（65-67 行）：首次调用返回 None
   且恰一条 warning；二次调用走 warn-once 早退（30-33 行）——仍 None、不再告警；
2. `check_path_allowed` 的 PermissionChecker 执行失败 except（124-126 行）：
   策略管理器可用但检查器导入失败 → 拒绝 + 含"执行失败"的原因。

失败注入走**真实导入失败**而非 mock 语义：把 `sys.modules` 的目标子模块槽位置
为 None，`from agentos_plugin_sdk.permission_policy import ...` 即真抛
ModuleNotFoundError（import 系统对 None 槽位的行为）。类级缓存 `_policy_manager`
与模块级 warn-once 旗标经 monkeypatch 保存/还原（自动收尾）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TOOLS_DIR = Path(__file__).resolve().parent.parent  # plugins/shared/tools/
_SYSTEM_DIR = _TOOLS_DIR.parent / "system"
_ISOLATION_DIR = _SYSTEM_DIR / "isolation"
_DOWNLOAD_DIR = _TOOLS_DIR / "download"
for _p in (_TOOLS_DIR, _SYSTEM_DIR, _ISOLATION_DIR, _DOWNLOAD_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_MOD_NAME = "workspace_aware_gaps_under_test"


def _load_mixin_module() -> Any:
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _DOWNLOAD_DIR / "workspace_aware.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mixin_module()
WorkspaceAwareMixin: Any = _MOD.WorkspaceAwareMixin


class _Tool(WorkspaceAwareMixin):
    """测试用最小工具（workspace/project_root 已初始化）。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._project_root = workspace
        self.base_path = workspace


@pytest.fixture
def fresh_policy_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """清类级 manager 缓存与模块级 warn-once 旗标（monkeypatch 自动还原）。"""
    monkeypatch.setattr(WorkspaceAwareMixin, "_policy_manager", None)
    monkeypatch.setattr(_MOD, "_policy_unavailable_warned", False)


class TestPolicyManagerImportFailure:
    """65-67 行：permission_policy 导入真失败 → warn-once + 返回 None。"""

    def test_missing_policy_module_warns_once_and_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fresh_policy_state: None
    ) -> None:
        monkeypatch.setitem(sys.modules, "agentos_plugin_sdk.permission_policy", None)

        with caplog.at_level(logging.WARNING, logger=_MOD_NAME):
            first = WorkspaceAwareMixin._get_policy_manager()
            second = WorkspaceAwareMixin._get_policy_manager()

        assert first is None
        assert second is None
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1  # warn-once：第二次走 30-33 早退，不再刷屏
        assert "permission_policy 加载失败" in warnings[0].getMessage()
        assert "ModuleNotFoundError" in warnings[0].getMessage()  # 真实失败原因留痕

    def test_check_path_allowed_fail_closed_when_manager_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fresh_policy_state: None
    ) -> None:
        """manager 缺失 → 拒绝（不是放行），原因含"路径权限"。"""
        monkeypatch.setitem(sys.modules, "agentos_plugin_sdk.permission_policy", None)
        tool = _Tool(tmp_path)

        allowed, reason = tool.check_path_allowed(str(tmp_path / "a.txt"), "read")

        assert allowed is False
        assert "路径权限" in reason

    def test_manager_cached_when_import_succeeds(
        self, tmp_path: Path, fresh_policy_state: None
    ) -> None:
        """对照组：导入可用时返回真实 manager 并缓存（二次取用同一实例）。"""
        first = WorkspaceAwareMixin._get_policy_manager()
        second = WorkspaceAwareMixin._get_policy_manager()

        assert first is not None
        assert second is first


class TestPermissionCheckerExecutionFailure:
    """124-126 行：checker 导入/执行失败 → 拒绝 + "执行失败"原因。"""

    def test_checker_import_failure_denies_with_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, fresh_policy_state: None
    ) -> None:
        monkeypatch.setitem(sys.modules, "agentos_plugin_sdk.permission_checker", None)
        tool = _Tool(tmp_path)

        allowed, reason = tool.check_path_allowed(str(tmp_path / "b.txt"), "write")

        assert allowed is False
        assert "执行失败" in reason
        assert "permission_checker" in reason
        warnings = [r for r in caplog.records if "路径权限校验层不可用" in r.getMessage()]
        assert len(warnings) == 1
        assert "permission_checker 执行失败" in warnings[0].getMessage()

    @pytest.mark.parametrize(
        ("operation", "agent_level", "expect_allowed"),
        [("read", None, True), ("write", None, True), ("write", 2, False)],
    )
    def test_real_checker_still_works_when_importable(
        self,
        tmp_path: Path,
        fresh_policy_state: None,
        operation: str,
        agent_level: int | None,
        expect_allowed: bool,
    ) -> None:
        """对照组：checker 可用时走真实决策（L1 项目内写放行、L2 workspace 外写拒）。"""
        workspace = tmp_path / "ws"
        workspace.mkdir()
        tool = _Tool(tmp_path / "project")
        tool._workspace = workspace
        tool._project_root = tmp_path / "project"
        outside = tmp_path / "project" / "outside.txt"

        allowed, reason = tool.check_path_allowed(str(outside), operation, agent_level=agent_level)

        assert allowed is expect_allowed
        if expect_allowed:
            assert reason == ""
