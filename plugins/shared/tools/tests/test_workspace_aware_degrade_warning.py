# @feature: FP-0.2.二 工具路径校验降级可观测（兜底反模式审查 P7） | @ci: none-local
"""workspace_aware isolation 降级一次性告警测试（2026-08-20 P7）。

锁一件事：isolation 插件不可用时 `check_path_allowed` 仍降级放行
（安全设计：pipeline 侧 security_check 是第二道防线），但本层降级
必须一次性 warning 留痕——两层防线同时哑火时不可观测是审查点名的
反模式（对齐 isolation_guard `_service_warned` 模式）。

[来源: docs/working/兜底反模式全库审查_20260820.md 三节 P7]
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_TOOLS_DIR = Path(__file__).resolve().parent.parent  # plugins/shared/tools/


def _load_module():
    mod_name = "workspace_aware_under_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TOOLS_DIR / "workspace_aware.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _Tool:
    """最小宿主：混入 WorkspaceAwareMixin 并预置 workspace/project_root。"""

    def __init__(self, workspace_aware_mod):
        # 动态混入（避免模块加载顺序依赖）
        self.__class__ = type(
            "_ToolWithMixin", (workspace_aware_mod.WorkspaceAwareMixin, _Tool), {}
        )
        self._workspace = Path("D:/ws")
        self._project_root = Path("D:/proj")


@pytest.fixture()
def force_isolation_import_error(monkeypatch):
    """强制 isolation 包导入失败（sys.modules 占位 None → ImportError）。"""
    monkeypatch.setitem(sys.modules, "isolation", None)


def test_degrade_warns_once(force_isolation_import_error, caplog):
    """降级放行 (True, "") + 一次性 warning；重复调用不再刷屏。"""
    mod = _load_module()
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setattr(mod, "_isolation_degrade_warned", False)
    try:
        tool = _Tool(mod)
        with caplog.at_level(logging.WARNING):
            ok, err = tool.check_path_allowed("some/file.txt", "read", 1)
        assert (ok, err) == (True, ""), "降级语义保持：isolation 不可用时放行"
        warns = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warns, "首次降级必须有 warning"
        assert "isolation" in warns[0].getMessage() and "降级" in warns[0].getMessage()

        records_before = len(caplog.records)
        tool.check_path_allowed("other/file.txt", "write", 2)
        assert len(caplog.records) == records_before, "一次性告警：后续降级不重复刷屏"
    finally:
        monkeypatch_env.undo()


def test_policy_manager_unavailable_returns_none(force_isolation_import_error):
    """_get_policy_manager：isolation 不可导入 → None（缓存不写入）。"""
    mod = _load_module()
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setattr(mod.WorkspaceAwareMixin, "_policy_manager", None)
    try:
        assert mod.WorkspaceAwareMixin._get_policy_manager() is None
    finally:
        monkeypatch_env.undo()


def test_uninitialized_workspace_still_rejected():
    """workspace 未初始化 → 显式拒绝（回归：降级不等于无条件放行）。"""
    mod = _load_module()
    tool = mod.WorkspaceAwareMixin()
    ok, err = tool.check_path_allowed("x.txt", "read", 1)
    assert ok is False and "未初始化" in err
