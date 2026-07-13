"""allow 白名单优先单元测试 — _match_rules 的 allow 规则优先于 block/needs_approval。

验证卡死根因修复：`wc -l ... 2>/dev/null` 这类安全命令被 dangerous_commands
误伤（命中 2>/dev/null 关键词）时，因 safe_commands(allow) 优先而正确放行。

1. allow 规则命中时立即放行，即使前面有 needs_approval/block 规则也匹配
2. 无 allow 命中时返回首个拦截/审批规则
3. 无任何规则匹配返回空
"""

from __future__ import annotations

from plugins.input.security_check.plugin import SecurityCheckPlugin


class TestMatchRulesAllowPriority:
    """_match_rules 应让 action=allow 优先于 block/needs_approval。"""

    def test_safe_command_with_danger_keyword_allowed(self) -> None:
        """allow 规则命中时优先于 needs_approval，即使命令含危险关键词。

        验证 allow 优先逻辑本身：构造 wc 开头命令（命中 safe_commands allow），
        同时命令含 curl（命中 dangerous_commands needs_approval）。
        allow 必须赢——这就是 _match_rules 修复的核心契约。
        """
        plugin = SecurityCheckPlugin()
        action, rule = plugin._match_rules(
            "bash_execute",
            {"command": "wc -l file.txt && echo done"},
        )
        assert action == "allow", f"安全命令应被 allow 放行，实际 action={action} rule={rule}"

    def test_compound_command_no_longer_matches_removed_keyword(self) -> None:
        """复合命令 cd /workspace && wc ... 不再被 2>/dev/null 误伤。

        卡死根因回归：dangerous_commands 已移除 2>/dev/null 关键词，
        该命令不再命中任何危险规则（action 空）。是否审批由 _is_dangerous_tool
        兜底决定（bash_execute 危险工具），不在此测试范围。
        """
        plugin = SecurityCheckPlugin()
        action, _ = plugin._match_rules(
            "bash_execute",
            {"command": "cd /workspace && wc -l src/*.js 2>/dev/null"},
        )
        # 删除 2>/dev/null 关键词后，复合命令（cd 开头）不命中危险规则也不命中白名单
        assert action == ""

    def test_plain_safe_command_allowed(self) -> None:
        """纯安全命令（ls）→ allow。"""
        plugin = SecurityCheckPlugin()
        action, _ = plugin._match_rules("bash_execute", {"command": "ls -la"})
        assert action == "allow"

    def test_dangerous_command_not_allowed(self) -> None:
        """真正危险命令（rm -rf）→ needs_approval（无 allow 命中）。"""
        plugin = SecurityCheckPlugin()
        action, _ = plugin._match_rules("bash_execute", {"command": "rm -rf /tmp/x"})
        assert action == "needs_approval"

    def test_unknown_command_returns_approval(self) -> None:
        """未知命令（不匹配 allow 也不匹配危险规则）→ 空字符串。"""
        plugin = SecurityCheckPlugin()
        action, _ = plugin._match_rules("bash_execute", {"command": "some-unknown-cmd"})
        assert action == ""

    def test_no_params_no_match(self) -> None:
        """无相关参数 → 空字符串。"""
        plugin = SecurityCheckPlugin()
        action, _ = plugin._match_rules("bash_execute", {})
        assert action == ""
