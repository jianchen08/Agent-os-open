# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""isolation 插件（权限检查器）单元测试。

覆盖（对齐 SDK agentos_plugin_sdk.permission_checker，isolation/download
共享单一真值源）：
1. check_read_permission：NONE/PROJECT/WORKSPACE/CUSTOM 四种范围
2. check_write_permission：NONE/PROJECT/WORKSPACE（allow_outside / require_checkpoint /
   allowed_operations）/CUSTOM + 便捷函数（dict 策略转换）
3. is_path_in_workspace / _normalize_path / resolve_path / get_project_root

测试不依赖真实内核——直接构造 WorkspacePermissionPolicy 与临时目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.permission_checker import PermissionChecker, check_write_permission
from agentos_plugin_sdk.permission_policy import (
    PermissionPolicyType,
    PermissionScope,
    ReadPermission,
    WorkspacePermissionPolicy,
    WritePermission,
)

pytestmark = pytest.mark.unit


def _policy(
    read_scope: PermissionScope = PermissionScope.PROJECT,
    write_scope: PermissionScope = PermissionScope.WORKSPACE,
    **write_kwargs: Any,
) -> WorkspacePermissionPolicy:
    return WorkspacePermissionPolicy(
        name="test",
        policy_type=PermissionPolicyType.DEFAULT,
        read=ReadPermission(scope=read_scope, custom_paths=[] if read_scope == PermissionScope.CUSTOM else None),
        write=WritePermission(scope=write_scope, **write_kwargs),
    )


# ═══════════════════════════════════════════════════════════
# 读取权限
# ═══════════════════════════════════════════════════════════


class TestReadPermission:
    def test_scope_none_denies(self) -> None:
        checker = PermissionChecker()
        ok, msg = checker.check_read_permission("a.py", None, _policy(read_scope=PermissionScope.NONE))
        assert ok is False and "禁止所有读取" in msg

    def test_scope_project_allows(self) -> None:
        checker = PermissionChecker()
        ok, msg = checker.check_read_permission("a.py", None, _policy(read_scope=PermissionScope.PROJECT))
        assert ok is True and msg == ""

    def test_scope_workspace_inside(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, _ = checker.check_read_permission(
            "ws/file.py", "ws", _policy(read_scope=PermissionScope.WORKSPACE)
        )
        assert ok is True

    def test_scope_workspace_outside_denied(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, msg = checker.check_read_permission(
            "other/file.py", "ws", _policy(read_scope=PermissionScope.WORKSPACE)
        )
        assert ok is False and "不在工作目录" in msg

    def test_scope_workspace_no_workspace(self) -> None:
        checker = PermissionChecker()
        ok, msg = checker.check_read_permission("a.py", None, _policy(read_scope=PermissionScope.WORKSPACE))
        assert ok is False and "未指定工作目录" in msg

    def test_scope_custom_match(self) -> None:
        checker = PermissionChecker(project_root=".")
        policy = WorkspacePermissionPolicy(
            name="c",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.CUSTOM, custom_paths=["allowed"]),
            write=WritePermission(scope=PermissionScope.WORKSPACE),
        )
        ok, _ = checker.check_read_permission("allowed/x.py", None, policy)
        assert ok is True
        ok2, msg2 = checker.check_read_permission("denied/x.py", None, policy)
        assert ok2 is False and "自定义路径" in msg2


# ═══════════════════════════════════════════════════════════
# 写入权限
# ═══════════════════════════════════════════════════════════


class TestWritePermission:
    def test_scope_none_denies(self) -> None:
        checker = PermissionChecker()
        ok, msg = checker.check_write_permission("a.py", None, _policy(write_scope=PermissionScope.NONE))
        assert ok is False and "禁止所有写入" in msg

    def test_scope_project_allows_with_confirmation_log(self) -> None:
        checker = PermissionChecker()
        ok, _ = checker.check_write_permission(
            "a.py", None, _policy(write_scope=PermissionScope.PROJECT, require_confirmation=True)
        )
        assert ok is True

    def test_scope_workspace_inside(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, _ = checker.check_write_permission("ws/x.py", "ws", _policy())
        assert ok is True

    def test_scope_workspace_outside_denied(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, msg = checker.check_write_permission("other/x.py", "ws", _policy())
        assert ok is False and "之外执行写入" in msg

    def test_scope_workspace_allow_outside(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, _ = checker.check_write_permission("other/x.py", "ws", _policy(allow_outside=True))
        assert ok is True

    def test_scope_workspace_no_workspace(self) -> None:
        checker = PermissionChecker()
        ok, msg = checker.check_write_permission("a.py", None, _policy())
        assert ok is False and "未指定工作目录" in msg

    def test_scope_workspace_operation_restriction(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, msg = checker.check_write_permission(
            "ws/x.py", "ws", _policy(allowed_operations=["create"]), operation="delete"
        )
        assert ok is False and "不允许执行" in msg
        ok2, _ = checker.check_write_permission(
            "ws/x.py", "ws", _policy(allowed_operations=["create"]), operation="create"
        )
        assert ok2 is True

    def test_scope_workspace_require_checkpoint_logs(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, _ = checker.check_write_permission("ws/x.py", "ws", _policy(require_checkpoint=True))
        assert ok is True

    def test_scope_custom_match(self) -> None:
        checker = PermissionChecker(project_root=".")
        policy = _policy(
            write_scope=PermissionScope.CUSTOM,
            custom_paths=["outbox"],
        )
        ok, _ = checker.check_write_permission("outbox/f.txt", None, policy)
        assert ok is True
        ok2, _ = checker.check_write_permission("inbox/f.txt", None, policy)
        assert ok2 is False

    def test_unknown_scope_rejected(self) -> None:
        checker = PermissionChecker()
        policy = _policy()
        policy.write.scope = "mystery"  # type: ignore[assignment]  # 未知范围字符串
        ok, msg = checker.check_write_permission("a.py", "ws", policy)
        assert ok is False and "未知的权限范围" in msg


# ═══════════════════════════════════════════════════════════
# CUSTOM 范围兄弟目录绕过（前缀比较必须带路径分隔符）
# ═══════════════════════════════════════════════════════════


class TestCustomScopeSiblingBypass:
    """custom 配 …/ws 时 …/ws-evil 不得因字符串前缀命中放行。

    allow_outside=false / CUSTOM 白名单语义对兄弟目录必须成立：
    界内路径与 custom 目录本身放行，兄弟目录前缀拒绝（真实临时目录验证）。
    """

    @pytest.fixture
    def ws_root(self, tmp_path: Path) -> Path:
        ws = tmp_path / "ws"
        ws.mkdir()
        (tmp_path / "ws-evil").mkdir()
        return ws

    def _read_policy(self, custom_path: str) -> WorkspacePermissionPolicy:
        return WorkspacePermissionPolicy(
            name="c",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.CUSTOM, custom_paths=[custom_path]),
            write=WritePermission(scope=PermissionScope.WORKSPACE),
        )

    def _write_policy(self, custom_path: str) -> WorkspacePermissionPolicy:
        return WorkspacePermissionPolicy(
            name="c",
            policy_type=PermissionPolicyType.DEFAULT,
            read=ReadPermission(scope=PermissionScope.PROJECT),
            write=WritePermission(scope=PermissionScope.CUSTOM, custom_paths=[custom_path]),
        )

    def test_read_sibling_dir_denied(self, tmp_path: Path, ws_root: Path) -> None:
        checker = PermissionChecker(project_root=str(tmp_path))
        policy = self._read_policy(str(ws_root))
        ok, _ = checker.check_read_permission(str(ws_root / "x.py"), None, policy)
        assert ok is True
        evil, msg = checker.check_read_permission(str(tmp_path / "ws-evil" / "x.py"), None, policy)
        assert evil is False and "自定义路径" in msg

    def test_write_sibling_dir_denied(self, tmp_path: Path, ws_root: Path) -> None:
        checker = PermissionChecker(project_root=str(tmp_path))
        policy = self._write_policy(str(ws_root))
        ok, _ = checker.check_write_permission(str(ws_root / "x.py"), None, policy)
        assert ok is True
        evil, _ = checker.check_write_permission(str(tmp_path / "ws-evil" / "x.py"), None, policy)
        assert evil is False

    def test_custom_dir_itself_allowed(self, tmp_path: Path, ws_root: Path) -> None:
        """路径恰为 custom 目录本身时放行（与 _is_path_inside 等值放行同语义）。"""
        checker = PermissionChecker(project_root=str(tmp_path))
        ok, _ = checker.check_read_permission(str(ws_root), None, self._read_policy(str(ws_root)))
        assert ok is True
        ok2, _ = checker.check_write_permission(str(ws_root), None, self._write_policy(str(ws_root)))
        assert ok2 is True


# ═══════════════════════════════════════════════════════════
# 路径工具 + 便捷函数
# ═══════════════════════════════════════════════════════════


class TestPathUtils:
    def test_is_path_in_workspace(self) -> None:
        checker = PermissionChecker(project_root=".")
        ok, _ = checker.is_path_in_workspace("ws/x.py", "ws")
        assert ok is True
        ok2, _ = checker.is_path_in_workspace("ws-other/x.py", "ws")
        assert ok2 is False

    def test_is_path_in_workspace_exception(self, monkeypatch) -> None:
        checker = PermissionChecker(project_root=".")
        monkeypatch.setattr(checker, "_normalize_path", lambda p: (_ for _ in ()).throw(OSError("boom")))
        ok, msg = checker.is_path_in_workspace("x.py", "ws")
        assert ok is False and msg is not None and "路径检查失败" in msg

    def test_resolve_path(self) -> None:
        import os

        checker = PermissionChecker(project_root=".")
        assert checker.resolve_path("rel/x.py") == os.path.normpath(os.path.abspath("rel/x.py"))
        # Windows 盘符路径：Windows 原样保留；POSIX 上 os.path.isabs 不认盘符
        # （resolve_path 走 project_root 拼接），断言"不丢盘符标识"语义
        resolved = checker.resolve_path("C:/abs/x.py")
        if os.name == "nt":
            assert resolved == os.path.normpath("C:/abs/x.py")
        else:
            assert resolved.endswith("C:/abs/x.py")

    def test_get_project_root(self) -> None:
        checker = PermissionChecker(project_root=str(Path().resolve()))
        assert checker.get_project_root() == str(Path().resolve())

    def test_module_level_check_write_permission_with_dict(self, tmp_path: Path) -> None:
        ok, _ = check_write_permission(
            "ws/x.py",
            "ws",
            {"name": "custom", "read": {"scope": "project"}, "write": {"scope": "workspace", "allow_outside": False}},
            project_root=".",
        )
        assert ok is True

    def test_module_level_check_write_permission_with_policy(self) -> None:
        ok, _ = check_write_permission("a.py", None, _policy(write_scope=PermissionScope.PROJECT))
        assert ok is True
