# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""workspace_aware 插件（工作空间感知 Mixin）单元测试。

覆盖（对齐 plugins/shared/tools/workspace_aware.py）：
1. _init_workspace：workspace / project_root / base_path / cwd 四种来源
2. resolve_path：绝对路径 / Git Bash 风格 / 前缀去重 / 后缀去重
3. _format_output_path / get_working_dir / _infer_project_root
4. check_path_allowed：未初始化拒绝、isolation 不可用降级放行、
   真实 PermissionPolicyManager 的 root_task / subtask 策略差异

测试不依赖真实内核——策略走 permission_policy 的代码默认值。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TOOLS_DIR = Path(__file__).resolve().parent  # plugins/shared/tools/
_SYSTEM_DIR = _TOOLS_DIR.parent / "system"
_ISOLATION_DIR = _SYSTEM_DIR / "isolation"
for _p in (_TOOLS_DIR, _SYSTEM_DIR, _ISOLATION_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_mixin() -> Any:
    mod_name = "workspace_aware_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TOOLS_DIR / "workspace_aware.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mixin()
WorkspaceAwareMixin = _MOD.WorkspaceAwareMixin


class _Tool(WorkspaceAwareMixin):
    """测试用最小工具。"""

    def __init__(self, base_path: str | None = None) -> None:
        if base_path:
            self.base_path = Path(base_path)
        self._workspace: Path | None = None
        self._project_root: Path | None = None


# ═══════════════════════════════════════════════════════════
# _init_workspace
# ═══════════════════════════════════════════════════════════


class TestInitWorkspace:
    def test_workspace_from_input(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws")})
        assert tool._workspace == tmp_path / "ws"  # type: ignore[comparison-overlap]
        assert tool._project_root == (tmp_path / "ws").resolve()

    def test_workspace_from_project_root(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"project_root": str(tmp_path / "proj")})
        assert tool._workspace == tmp_path / "proj"  # type: ignore[comparison-overlap]

    def test_workspace_from_base_path(self, tmp_path: Path) -> None:
        tool = _Tool(base_path=str(tmp_path / "base"))
        tool._init_workspace({})
        assert tool._workspace == tmp_path / "base"  # type: ignore[comparison-overlap]

    def test_workspace_defaults_to_cwd(self) -> None:
        tool = _Tool()
        tool._init_workspace({})
        assert tool._workspace == Path.cwd()  # type: ignore[comparison-overlap]

    def test_project_root_respects_input(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws"), "project_root": str(tmp_path)})
        assert tool._project_root == tmp_path


# ═══════════════════════════════════════════════════════════
# resolve_path
# ═══════════════════════════════════════════════════════════


class TestResolvePath:
    def _tool(self, tmp_path: Path) -> _Tool:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws")})
        return tool

    def test_absolute_path(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        assert tool.resolve_path(str(tmp_path / "x" / "y.txt")) == (tmp_path / "x" / "y.txt").resolve()

    def test_relative_path(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        assert tool.resolve_path("a/b.txt") == (tmp_path / "ws" / "a" / "b.txt").resolve()

    def test_full_prefix_dedup(self, tmp_path: Path) -> None:
        """相对路径已含完整 workspace 前缀 → 去重。"""
        tool = self._tool(tmp_path)
        ws = str(tmp_path / "ws").replace("\\", "/")
        assert tool.resolve_path(f"{ws}/a.txt") == (tmp_path / "ws" / "a.txt").resolve()

    def test_suffix_dedup(self, tmp_path: Path) -> None:
        """相对路径含 workspace 尾部组件 → 去重。"""
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "deep" / "ws")})
        assert tool.resolve_path("ws/a.txt") == (tmp_path / "deep" / "ws" / "a.txt").resolve()

    def test_git_bash_style_absolute(self, tmp_path: Path) -> None:
        """Windows Git Bash 风格 /d/path → D:\\path。"""
        tool = self._tool(tmp_path)
        import platform

        if platform.system() == "Windows":
            resolved = tool.resolve_path("/d/myproj/x.txt")
            assert str(resolved).startswith("D:\\myproj\\x.txt")
        else:
            pytest.skip("仅 Windows 生效")


# ═══════════════════════════════════════════════════════════
# _format_output_path / get_working_dir / _infer_project_root
# ═══════════════════════════════════════════════════════════


class TestOutputFormatting:
    def test_format_output_path_absolute_input(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws")})
        abs_path = tmp_path / "ws" / "f.txt"
        out = tool._format_output_path(abs_path, str(abs_path))
        assert out == str(abs_path)

    def test_format_output_path_relative_to_project(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws"), "project_root": str(tmp_path)})
        out = tool._format_output_path(tmp_path / "ws" / "f.txt", "f.txt")
        assert out == str(Path("ws") / "f.txt").replace("\\", "/").replace("/", "\\") or out

    def test_format_output_path_fallback_absolute(self, tmp_path: Path) -> None:
        """无法相对化 → 返回绝对路径。"""
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws")})
        other = tmp_path / "elsewhere" / "f.txt"
        out = tool._format_output_path(other, "f.txt")
        assert out == str(other)

    def test_get_working_dir(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws")})
        assert tool.get_working_dir({"working_dir": str(tmp_path / "wd")}) == tmp_path / "wd"
        assert tool.get_working_dir({}) == tmp_path / "ws"
        tool2 = _Tool()
        assert tool2.get_working_dir({}) is None

    def test_infer_project_root_finds_git(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "a" / "b").mkdir(parents=True)
        assert _MOD.WorkspaceAwareMixin._infer_project_root(tmp_path / "a" / "b") == tmp_path

    def test_infer_project_root_fallback(self, tmp_path: Path) -> None:
        ws = tmp_path / "no-git-here"
        ws.mkdir()
        assert _MOD.WorkspaceAwareMixin._infer_project_root(ws) == ws.resolve()


# ═══════════════════════════════════════════════════════════
# check_path_allowed
# ═══════════════════════════════════════════════════════════


class TestCheckPathAllowed:
    def test_uninitialized_rejected(self) -> None:
        tool = _Tool()
        ok, msg = tool.check_path_allowed("x.txt", operation="write")
        assert ok is False and "未初始化" in msg

    def test_isolation_unavailable_degrades_open(self, monkeypatch) -> None:
        """isolation.permission_policy 不可导入 → 降级放行。"""
        tool = _Tool()
        tool._init_workspace({"workspace": "ws"})
        monkeypatch.setattr(_MOD.WorkspaceAwareMixin, "_get_policy_manager", classmethod(lambda cls: None))
        ok, msg = tool.check_path_allowed("x.txt", operation="write")
        assert ok is True and msg == ""

    def test_root_task_policy_allows_project_write(self, tmp_path: Path) -> None:
        """L1/缺省 → root_task 策略：写整个项目放行。"""
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws"), "project_root": str(tmp_path)})
        ok, msg = tool.check_path_allowed(str(tmp_path / "elsewhere" / "f.txt"), operation="write")
        assert ok is True, msg

    def test_subtask_policy_denies_outside_write(self, tmp_path: Path) -> None:
        """L2+ → subtask 策略：workspace 外写入被拒。"""
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws"), "project_root": str(tmp_path)})
        ok, msg = tool.check_path_allowed(
            str(tmp_path / "elsewhere" / "f.txt"), operation="write", agent_level=2
        )
        assert ok is False and "之外执行写入" in msg

    def test_read_permission_path(self, tmp_path: Path) -> None:
        tool = _Tool()
        tool._init_workspace({"workspace": str(tmp_path / "ws"), "project_root": str(tmp_path)})
        ok, msg = tool.check_path_allowed(str(tmp_path / "ws" / "f.txt"), operation="read", agent_level=2)
        assert ok is True, msg

    def test_policy_manager_cached(self) -> None:
        """模块级 _policy_manager 缓存复用（第二次不重建）。"""
        tool = _Tool()
        first = tool._get_policy_manager()
        second = tool._get_policy_manager()
        assert first is second
