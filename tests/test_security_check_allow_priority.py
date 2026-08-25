# @feature: FP-MIGR 0.1→0.2迁移 | @ci: python-coverage
"""allow 白名单优先单元测试 — _match_rules 的 allow 规则优先于 block/needs_approval。

验证契约：`wc -l ... 2>/dev/null` 这类安全命令命中 dangerous_commands
（含 2>/dev/null 关键词）时，因 safe_commands(allow) 优先而正确放行。

1. allow 规则命中时立即放行，即使前面有 needs_approval/block 规则也匹配
2. 无 allow 命中时返回首个拦截/审批规则
3. 无任何规则匹配返回空

0.2 加载方式：被测插件的 _load_rules 通过 config.config_center 加载规则，
config_center 已不在 0.2 装配中（仅 reference/0.1_src 留存），规则加载失败
会导致 _match_rules 恒返回空。本测试改为直接从仓库根的
config/isolation/security_rules.yaml 读取规则列表，经 config={"rules": ...}
注入插件，使 _match_rules 有真实规则可匹配。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "security_check")
from plugin import SecurityCheckPlugin

# 直接从仓库根读取安全规则，绕开已删除的 config.config_center。
_REPO_ROOT = Path(__file__).resolve().parent.parent
_RULES_FILE = _REPO_ROOT / "config" / "isolation" / "security_rules.yaml"
try:
    _SECURITY_RULES: list[dict] = (
        yaml.safe_load(_RULES_FILE.read_text(encoding="utf-8")) or {}
    ).get("rules", [])
except (FileNotFoundError, OSError, yaml.YAMLError):
    _SECURITY_RULES = []


def _make_plugin() -> SecurityCheckPlugin:
    """构造带真实安全规则的 SecurityCheckPlugin（规则直接注入，不依赖 config_center）。"""
    return SecurityCheckPlugin(config={"rules": _SECURITY_RULES})


class TestMatchRulesAllowPriority:
    """_match_rules 应让 action=allow 优先于 block/needs_approval。"""

    def test_safe_command_with_danger_keyword_allowed(self) -> None:
        """allow 规则命中时优先于 needs_approval，即使命令含危险关键词。

        验证 allow 优先逻辑本身：构造 wc 开头命令（命中 safe_commands allow），
        同时命令含 curl（命中 dangerous_commands needs_approval）。
        allow 必须赢——这就是 _match_rules 修复的核心契约。
        """
        plugin = _make_plugin()
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
        plugin = _make_plugin()
        action, _ = plugin._match_rules(
            "bash_execute",
            {"command": "cd /workspace && wc -l src/*.js 2>/dev/null"},
        )
        # 删除 2>/dev/null 关键词后，复合命令（cd 开头）不命中危险规则也不命中白名单
        assert action == ""

    def test_plain_safe_command_allowed(self) -> None:
        """纯安全命令（ls）→ allow。"""
        plugin = _make_plugin()
        action, _ = plugin._match_rules("bash_execute", {"command": "ls -la"})
        assert action == "allow"

    def test_dangerous_command_not_allowed(self) -> None:
        """真正危险命令（rm -rf）→ needs_approval（无 allow 命中）。"""
        plugin = _make_plugin()
        action, _ = plugin._match_rules("bash_execute", {"command": "rm -rf /tmp/x"})
        assert action == "needs_approval"

    def test_unknown_command_returns_approval(self) -> None:
        """未知命令（不匹配 allow 也不匹配危险规则）→ 空字符串。"""
        plugin = _make_plugin()
        action, _ = plugin._match_rules("bash_execute", {"command": "some-unknown-cmd"})
        assert action == ""

    def test_no_params_no_match(self) -> None:
        """无相关参数 → 空字符串。"""
        plugin = _make_plugin()
        action, _ = plugin._match_rules("bash_execute", {})
        assert action == ""
