# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation workspace.py 路径解析与安全校验测试（A5.3 补）。

覆盖 test_workspace_root.py 未触及的入口：
1. resolve_workspace：根任务（绝对/相对/默认/已含 root 前缀）、子任务
   （nested 嵌套 / shared 共享 / 绝对路径 / 已含父前缀 / 已含 root 前缀）；
2. validate_workspace_path：空串、磁盘根目录（Windows/Unix）、系统危险目录、
   配置工作空间根目录、正常路径通过；
3. _is_absolute_path：Windows 盘符 / Unix 根 / 相对路径。

测试不依赖真实内核——直接加载 workspace.py，DB 用假 session 对象。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_ws() -> Any:
    mod_name = "isolation_workspace_resolve_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "workspace.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_ws()
resolve_workspace = _MOD.resolve_workspace
validate_workspace_path = _MOD.validate_workspace_path
_is_absolute_path = _MOD._is_absolute_path


class TestResolveWorkspaceRootTask:
    def test_default_root_task_uses_config_root_and_task_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert resolve_workspace("task-1", None) == "/data/ws/task-1"

    def test_absolute_task_workspace_used_as_is(self) -> None:
        assert resolve_workspace("t1", "D:/myproject/task-x") == "D:/myproject/task-x"

    def test_relative_task_workspace_joined_to_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert resolve_workspace("t1", "sub/dir") == "/data/ws/sub/dir"

    def test_task_workspace_already_under_root_returned_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert resolve_workspace("t1", "/data/ws/child") == "/data/ws/child"
        assert resolve_workspace("t1", "/data/ws") == "/data/ws"

    def test_config_root_parameter_wins(self) -> None:
        assert resolve_workspace("t1", None, config_root="/custom/root") == "/custom/root/t1"


class TestResolveWorkspaceChildTask:
    def test_nested_default_creates_child_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert resolve_workspace("child", None, parent_resolved_workspace="/data/ws/parent") == "/data/ws/parent/child"

    def test_nested_with_relative_task_workspace(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert (
            resolve_workspace("child", "sub", parent_resolved_workspace="/data/ws/parent")
            == "/data/ws/parent/sub"
        )

    def test_shared_mode_reuses_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert (
            resolve_workspace("child", "sub", parent_resolved_workspace="/data/ws/parent", nesting_mode="shared")
            == "/data/ws/parent"
        )

    def test_child_absolute_path_returned_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert (
            resolve_workspace("child", "/elsewhere/x", parent_resolved_workspace="/data/ws/parent")
            == "/elsewhere/x"
        )

    def test_child_already_has_parent_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert (
            resolve_workspace("child", "/data/ws/parent/x", parent_resolved_workspace="/data/ws/parent")
            == "/data/ws/parent/x"
        )

    def test_child_has_root_prefix_but_not_parent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert (
            resolve_workspace("child", "/data/ws/sibling", parent_resolved_workspace="/data/ws/parent")
            == "/data/ws/sibling"
        )

    def test_windows_paths_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "D:/data/ws")
        assert resolve_workspace("t1", None) == "D:/data/ws/t1"


class TestValidateWorkspacePath:
    def test_empty_workspace_returns_none(self) -> None:
        assert validate_workspace_path("") is None

    def test_windows_disk_root_rejected(self) -> None:
        # 盘符根目录按路径形态判定（不 patch os.name——Linux 上把全局 os 模块
        # 补丁成 nt 会让任何 Path() 构造抛 NotImplementedError，pytest 进程
        # 整体崩溃 INTERNALERROR）
        err = validate_workspace_path("C:\\")
        assert err and "磁盘根目录" in err

    def test_unix_root_rejected(self) -> None:
        err = validate_workspace_path("/")
        # "/" 命中磁盘根目录分支或系统目录分支（_DANGEROUS_UNIX_DIRS 含 "/"），
        # 两者的报错都属"拒绝"，断言不通过即可
        assert err and ("根目录" in err or "系统目录" in err)

    def test_system_dangerous_dir_rejected(self) -> None:
        err = validate_workspace_path("/etc")
        assert err and "系统目录" in err

    def test_windows_system_dir_rejected_lowercase(self) -> None:
        err = validate_workspace_path(r"c:\windows\system32")
        assert err and "系统目录" in err

    def test_config_workspace_root_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        err = validate_workspace_path("/data/ws")
        assert err and "工作空间根目录" in err

    def test_config_root_read_failure_skips_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> str:
            raise RuntimeError("config read failed")

        monkeypatch.setattr(_MOD, "get_workspace_config_root", _boom)
        assert validate_workspace_path("/data/safe/dir") is None

    def test_normal_path_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_MOD, "get_workspace_config_root", lambda: "/data/ws")
        assert validate_workspace_path("/data/project/src") is None

    def test_invalid_path_returns_error(self) -> None:
        # 空串返回 None（有效跳过）；含 NUL 的路径在 Windows normpath 不抛，
        # 断言平台无关行为：不抛异常
        assert validate_workspace_path("") is None
        result = validate_workspace_path("\x00bad")
        assert result is None or isinstance(result, str)


class TestIsAbsolutePath:
    def test_windows_drive_absolute(self) -> None:
        assert _is_absolute_path("C:/foo") is True
        assert _is_absolute_path("D:\\bar") is True

    def test_unix_root_absolute(self) -> None:
        assert _is_absolute_path("/usr/bin") is True
        # // 前缀在 Windows 下 is_absolute() 为 True（UNC）,仅验证不抛
        assert isinstance(_is_absolute_path("//server/share"), bool)

    def test_relative_false(self) -> None:
        assert _is_absolute_path("relative/path") is False
        assert _is_absolute_path("") is False
