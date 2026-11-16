# @feature: FP-0.2.〇 安全检查 敏感路径判定 | @ci: python-coverage
"""敏感系统目录黑名单单元测试。

验证 security_check 插件内 sensitive_paths.py（单源）的 is_sensitive_path：
- Windows 敏感目录（C:/Windows/System32 等）命中
- Linux 敏感目录（/etc /proc 等）命中
- 普通项目路径不命中
- 相等与前缀两种匹配
- 空路径 / 无效路径安全处理
"""

from __future__ import annotations

import os
import sys

import pytest

from sensitive_paths import (
    SENSITIVE_DIRS_LINUX,
    SENSITIVE_DIRS_WINDOWS,
    is_sensitive_path,
)


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


class TestResolveFailureFallback:
    """Path.resolve 失败（如内嵌 NUL）的回退通路。"""

    def test_embedded_null_does_not_crash(self) -> None:
        """Path.resolve 抛 ValueError（内嵌 NUL）→ 回退原字符串比较，判定不崩。"""
        hit, prefix = is_sensitive_path("/etc/passwd\x00evil")
        assert isinstance(hit, bool)
        assert isinstance(prefix, str)

    def test_fallback_raw_string_still_matches_blacklist(self) -> None:
        """回退字符串仍参与黑名单比对：含敏感前缀的坏路径命中（返回该平台前缀之一）。

        坏路径须带敏感前缀的子路径（前缀 + `/` 后再粘 NUL）——黑名单匹配
        是「相等或前缀 + /」，NUL 直接粘在前缀尾部不构成命中。
        """
        blacklist = SENSITIVE_DIRS_WINDOWS if sys.platform == "win32" else SENSITIVE_DIRS_LINUX
        bad = "c:/windows/system32\x00x" if sys.platform == "win32" else "/etc/passwd\x00x"
        hit, prefix = is_sensitive_path(bad)
        assert hit is True
        assert prefix in blacklist
        assert bad.lower().replace("\\", "/").startswith(prefix)

    def test_wellformed_paths_unaffected_by_fallback(self, tmp_path) -> None:
        """对照组：合法路径走 resolve 通路，空路径/项目内路径判定为不敏感。"""
        assert is_sensitive_path("") == (False, "")
        assert is_sensitive_path(str(tmp_path / "proj" / "src"))[0] is False
