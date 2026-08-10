"""命令指纹单元测试 — security_check 的 _make_signature 行为。

验证「本管道内同命令免批」记忆的指纹计算逻辑：

1. 单数 path 参数能算出指纹（非 None）
2. 路径内容不同 → 不同指纹（精确匹配，不误放行）
3. 相同参数 → 相同指纹（可稳定记忆）
4. 无可归一化参数 → 返回 None（退化为仅本次，保守）
5. list 类型参数参与指纹（内容差异可区分）

注：批量复数参数（paths/copies/moves/task_ids）已从各工具移除，
本测试改为验证 _make_signature 的通用参数处理行为，不再绑定特定工具的已删字段。
"""

from __future__ import annotations

from plugins.input.security_check.plugin import SecurityCheckPlugin


class TestSignature:
    """_make_signature 对各类型参数的指纹计算行为。"""

    def test_single_path_produces_signature(self) -> None:
        """单数 path 参数能算出指纹（非 None）。"""
        plugin = SecurityCheckPlugin()
        sig = plugin._make_signature("file_write", {"path": "src/x.py"})
        assert sig is not None
        assert sig.startswith("file_write:")

    def test_different_path_different_signature(self) -> None:
        """路径内容不同 → 不同指纹（精确匹配，不误放行）。"""
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature("file_write", {"path": "docs/a.md"})
        sig2 = plugin._make_signature("file_write", {"path": "docs/b.md"})
        assert sig1 != sig2

    def test_same_args_same_signature(self) -> None:
        """相同参数 → 相同指纹（可稳定记忆，允许同管道内免重审）。"""
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature("file_write", {"path": "src/x.py", "content": "hello"})
        sig2 = plugin._make_signature("file_write", {"path": "src/x.py", "content": "hello"})
        assert sig1 == sig2

    def test_no_normalizable_args_returns_none(self) -> None:
        """无可归一化参数 → 返回 None（退化为仅本次，保守）。"""
        plugin = SecurityCheckPlugin()
        sig = plugin._make_signature("file_write", {})
        assert sig is None

    def test_empty_string_values_ignored(self) -> None:
        """空字符串参数被忽略，若无其他有效参数则返回 None。"""
        plugin = SecurityCheckPlugin()
        sig = plugin._make_signature("file_write", {"path": ""})
        assert sig is None

    def test_list_value_participates_in_signature(self) -> None:
        """list 类型参数参与指纹（内容差异可区分）。

        _make_signature 对 list 走有序 repr，故顺序不同 → 指纹不同。
        这是「宁可多问」的保守策略：批量场景下顺序差异也触发重审。
        """
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature("example_tool", {"items": ["a", "b", "c"]})
        sig2 = plugin._make_signature("example_tool", {"items": ["a", "b", "c"]})
        sig3 = plugin._make_signature("example_tool", {"items": ["c", "a", "b"]})
        # 相同内容相同顺序 → 相同指纹
        assert sig1 == sig2
        # 内容相同顺序不同 → 不同指纹（list 有序）
        assert sig1 != sig3
        assert sig1 is not None

    def test_different_list_content_different_signature(self) -> None:
        """list 内容不同 → 不同指纹。"""
        plugin = SecurityCheckPlugin()
        sig1 = plugin._make_signature("example_tool", {"items": ["a"]})
        sig2 = plugin._make_signature("example_tool", {"items": ["b"]})
        assert sig1 != sig2
