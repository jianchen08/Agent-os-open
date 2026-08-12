# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-plugins-test
"""工具名模糊匹配的单测。

验证 SecurityCheckPlugin 的 _tool_matches_rule / _fuzzy_tool_eq / _levenshtein：
    1. 默认关闭 fuzzy_tool_matching：精确匹配（向后兼容）
    2. 开启 fuzzy_tool_matching：大小写不敏感
    3. 开启：下划线/连字符等价（bash-execute ≡ bash_execute）
    4. 开启：Levenshtein <= 1 容忍单字符拼写错误
    5. ["*"] 通配始终匹配
    6. _match_rules 在开启模糊匹配后，规则能匹配到漂移的工具名

不依赖 src/ 或 plugins.input 旧路径，通过 sys.path 注入直接导入本地 plugin 模块。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 复制 server.py 的 sys.path 机制：
#   plugins/shared/（pipeline 等包）+ 插件目录（本地 plugin.py）
# 注意：多个插件目录的 plugin.py 同名，用 importlib 显式按路径加载本目录的，
# 不依赖 sys.path 里的 'plugin' 名字（避免被其他插件的 plugin.py 污染）。
_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_sc_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sc_mod)
SecurityCheckPlugin = _sc_mod.SecurityCheckPlugin


# ══════════════════════════════════════════════════
# 1. 默认关闭：精确匹配（向后兼容）
# ══════════════════════════════════════════════════


class TestFuzzyMatchingDisabled:
    """fuzzy_tool_matching=False（默认）时严格精确匹配。"""

    def test_default_disabled(self) -> None:
        """默认实例 fuzzy_tool_matching 关闭。"""
        plugin = SecurityCheckPlugin()
        assert plugin._fuzzy_tool_matching is False

    def test_exact_match(self) -> None:
        """精确匹配命中。"""
        plugin = SecurityCheckPlugin()
        assert plugin._tool_matches_rule("bash_execute", ["bash_execute", "file_read"]) is True

    def test_case_sensitive_when_disabled(self) -> None:
        """关闭时大小写敏感：BashExecute ≠ bash_execute。"""
        plugin = SecurityCheckPlugin()
        assert plugin._tool_matches_rule("BashExecute", ["bash_execute"]) is False

    def test_separator_sensitive_when_disabled(self) -> None:
        """关闭时连字符/下划线敏感：bash-execute ≠ bash_execute。"""
        plugin = SecurityCheckPlugin()
        assert plugin._tool_matches_rule("bash-execute", ["bash_execute"]) is False


# ══════════════════════════════════════════════════
# 2. ["*"] 通配始终匹配
# ══════════════════════════════════════════════════


class TestWildcardMatch:
    """["*"] 通配无论 fuzzy 开关都匹配。"""

    def test_wildcard_matches_disabled(self) -> None:
        plugin = SecurityCheckPlugin()
        assert plugin._tool_matches_rule("any_tool", ["*"]) is True

    def test_wildcard_matches_enabled(self) -> None:
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("any_tool", ["*"]) is True


# ══════════════════════════════════════════════════
# 3. 开启模糊匹配：大小写不敏感
# ══════════════════════════════════════════════════


class TestFuzzyMatchingCaseInsensitive:
    """开启 fuzzy_tool_matching 后大小写不敏感。"""

    def test_case_insensitive(self) -> None:
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("BashExecute", ["bash_execute"]) is True

    def test_all_upper(self) -> None:
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("FILE_READ", ["file_read"]) is True


# ══════════════════════════════════════════════════
# 4. 开启模糊匹配：下划线/连字符等价
# ══════════════════════════════════════════════════


class TestFuzzyMatchingSeparator:
    """开启后下划线/连字符等价（防 bash-execute vs bash_execute 漂移）。"""

    def test_hyphen_to_underscore(self) -> None:
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("bash-execute", ["bash_execute"]) is True

    def test_underscore_to_hyphen(self) -> None:
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("bash_execute", ["bash-execute"]) is True

    def test_mixed_case_and_separator(self) -> None:
        """Bash-Execute ≡ bash_execute（大小写+分隔符同时漂移）。"""
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("Bash-Execute", ["bash_execute"]) is True


# ══════════════════════════════════════════════════
# 5. Levenshtein <= 1 容忍单字符拼写错误
# ══════════════════════════════════════════════════


class TestFuzzyMatchingLevenshtein:
    """开启后容忍单字符差异（拼写错误）。"""

    def test_one_char_typo(self) -> None:
        """file_read vs file_read2（多一字符）应匹配。"""
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("file_read2", ["file_read"]) is True

    def test_two_char_diff_not_matched(self) -> None:
        """距离 > 1 不匹配（避免过度容错误伤）。"""
        plugin = SecurityCheckPlugin(config={"fuzzy_tool_matching": True})
        assert plugin._tool_matches_rule("file_write", ["file_read"]) is False

    def test_levenshtein_identical(self) -> None:
        assert SecurityCheckPlugin._levenshtein("abc", "abc") == 0

    def test_levenshtein_one_edit(self) -> None:
        assert SecurityCheckPlugin._levenshtein("abc", "abd") == 1
        assert SecurityCheckPlugin._levenshtein("abc", "ab") == 1
        assert SecurityCheckPlugin._levenshtein("abc", "abcd") == 1

    def test_levenshtein_two_edits(self) -> None:
        assert SecurityCheckPlugin._levenshtein("abc", "xyz") == 3

    def test_levenshtein_empty(self) -> None:
        assert SecurityCheckPlugin._levenshtein("", "abc") == 3
        assert SecurityCheckPlugin._levenshtein("abc", "") == 3
        assert SecurityCheckPlugin._levenshtein("", "") == 0


# ══════════════════════════════════════════════════
# 6. _match_rules 集成：开启模糊匹配后规则能命中漂移工具名
# ══════════════════════════════════════════════════


class TestMatchRulesIntegration:
    """_match_rules 在开启模糊匹配后，规则 tools 能匹配漂移的工具名。"""

    def test_rule_matches_when_tool_name_drifts(self) -> None:
        """规则声明 tools=["bash_execute"]，实际工具名 bash-execute，开启模糊匹配应命中。"""
        plugin = SecurityCheckPlugin(
            config={
                "fuzzy_tool_matching": True,
                "rules": [
                    {
                        "name": "test_rule",
                        "tools": ["bash_execute"],
                        "params": ["command"],
                        "action": "block",
                        "patterns": [{"type": "keyword", "value": "rm -rf"}],
                    }
                ],
            }
        )
        action, rule = plugin._match_rules("bash-execute", {"command": "rm -rf /tmp"})
        assert action == "block"
        assert rule == "test_rule"

    def test_rule_does_not_match_when_disabled(self) -> None:
        """关闭模糊匹配时，bash-execute 不命中 tools=["bash_execute"] 的规则。"""
        plugin = SecurityCheckPlugin(
            config={
                "fuzzy_tool_matching": False,
                "rules": [
                    {
                        "name": "test_rule",
                        "tools": ["bash_execute"],
                        "params": ["command"],
                        "action": "block",
                        "patterns": [{"type": "keyword", "value": "rm -rf"}],
                    }
                ],
            }
        )
        action, _rule = plugin._match_rules("bash-execute", {"command": "rm -rf /tmp"})
        assert action == ""  # 工具不匹配 → 规则不适用
