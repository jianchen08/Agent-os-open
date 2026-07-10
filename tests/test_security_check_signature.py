"""命令指纹单元测试 — security_check 的 _make_signature 支持 paths 批量参数。

验证「本管道内同命令免批」对批量删除/移动工具生效：

1. delete_file(paths=[...]) 能算出指纹（修复前 paths 复数被忽略返回 None）
2. 相同路径集合不同顺序 → 相同指纹（避免顺序差异导致重复审批）
3. 路径内容不同 → 不同指纹（精确匹配，不误放行）
4. 仅 paths 参数即可记忆（approved_remember 不再退化为仅本次）
"""

from __future__ import annotations

from plugins.input.security_check.plugin import SecurityCheckPlugin


class TestSignaturePaths:
    """_make_signature 应识别 delete_file 等工具的 paths 批量参数。"""

    def test_paths_array_produces_signature(self) -> None:
        """delete_file(paths=[...]) 能算出指纹（非 None）。"""
        plugin = SecurityCheckPlugin()
        sig = plugin._make_signature(
            "delete_file",
            {"paths": ["docs/a.md", "docs/b.md"]},
        )
        assert sig is not None
        assert sig.startswith("delete_file:")

    def test_same_paths_different_order_same_signature(self) -> None:
        """相同路径集合不同顺序 → 相同指纹（顺序无关）。"""
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature(
            "delete_file",
            {"paths": ["docs/a.md", "docs/b.md", "docs/c.md"]},
        )
        sig2 = plugin._make_signature(
            "delete_file",
            {"paths": ["docs/c.md", "docs/a.md", "docs/b.md"]},
        )
        assert sig1 == sig2

    def test_different_paths_different_signature(self) -> None:
        """路径内容不同 → 不同指纹（精确匹配，不误放行）。"""
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature(
            "delete_file", {"paths": ["docs/a.md"]}
        )
        sig2 = plugin._make_signature(
            "delete_file", {"paths": ["docs/b.md"]}
        )
        assert sig1 != sig2

    def test_single_path_still_works(self) -> None:
        """单数 path 参数的指纹计算不受影响（向后兼容）。"""
        plugin = SecurityCheckPlugin()
        sig = plugin._make_signature("file_write", {"path": "src/x.py"})
        assert sig is not None
        assert sig.startswith("file_write:")

    def test_empty_paths_no_signature(self) -> None:
        """空 paths 列表 → 无法计算指纹（退化为仅本次，保守）。"""
        plugin = SecurityCheckPlugin()
        sig = plugin._make_signature("delete_file", {"paths": []})
        assert sig is None
