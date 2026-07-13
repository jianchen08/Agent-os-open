"""敏感系统目录黑名单单元测试。

验证 src/isolation/sensitive_paths.py 的 is_sensitive_path：
- Windows 敏感目录（C:/Windows/System32 等）命中
- Linux 敏感目录（/etc /proc 等）命中
- 普通项目路径不命中
- 相等与前缀两种匹配
- 空路径 / 无效路径安全处理
"""

from __future__ import annotations

import os

import pytest

from isolation.sensitive_paths import is_sensitive_path


class TestIsSensitivePath:
    """is_sensitive_path 行为测试。"""

    def test_empty_path_not_sensitive(self) -> None:
        """空路径不命中。"""
        hit, matched = is_sensitive_path("")
        assert hit is False
        assert matched == ""

    def test_normal_project_path_not_sensitive(self, tmp_path) -> None:
        """普通项目路径不命中。"""
        hit, matched = is_sensitive_path(str(tmp_path / "src" / "main.py"))
        assert hit is False
        assert matched == ""

    @pytest.mark.skipif(os.name != "nt", reason="Windows 敏感目录仅 Windows 测试")
    def test_windows_system32_hit(self) -> None:
        """Windows System32 路径命中黑名单（命中更宽的 c:/windows 前缀）。"""
        hit, matched = is_sensitive_path("C:\\Windows\\System32\\drivers\\etc\\hosts")
        assert hit is True
        assert "windows" in matched

    @pytest.mark.skipif(os.name != "nt", reason="Windows 敏感目录仅 Windows 测试")
    def test_windows_exact_boundary_hit(self) -> None:
        """Windows 敏感目录边界本身命中（相等匹配）。"""
        hit, matched = is_sensitive_path("C:\\Windows")
        assert hit is True

    @pytest.mark.skipif(os.name != "nt", reason="Windows 敏感目录仅 Windows 测试")
    def test_windows_program_files_hit(self) -> None:
        """Program Files 命中。"""
        hit, matched = is_sensitive_path("C:\\Program Files\\SomeApp\\app.exe")
        assert hit is True

    @pytest.mark.skipif(os.name == "nt", reason="Linux 敏感目录仅非 Windows 测试")
    def test_linux_etc_hit(self) -> None:
        """Linux /etc 命中黑名单。"""
        hit, matched = is_sensitive_path("/etc/passwd")
        assert hit is True
        assert matched == "/etc"

    @pytest.mark.skipif(os.name == "nt", reason="Linux 敏感目录仅非 Windows 测试")
    def test_linux_proc_hit(self) -> None:
        """Linux /proc 命中。"""
        hit, matched = is_sensitive_path("/proc/self/status")
        assert hit is True

    def test_relative_path_resolved(self, tmp_path) -> None:
        """相对路径会被 resolve，普通目录不命中。"""
        hit, matched = is_sensitive_path("./src/app.py")
        assert hit is False

    def test_path_with_traversal_not_crash(self) -> None:
        """含路径遍历的路径不崩溃（路径遍历由 security_check 另行拦截）。"""
        # 这里只测 is_sensitive_path 不崩溃，遍历检测在别处
        hit, matched = is_sensitive_path("../../../etc/passwd")
        # resolve 后可能是绝对路径，结果取决于平台
        assert isinstance(hit, bool)
