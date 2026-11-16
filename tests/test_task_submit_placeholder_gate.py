# @feature: FP-PROMPT 物料整改 | @ci: python-coverage
"""task_submit 验收参数占位符闸门测试。

审查报告 docs/working/提示词Skill物料审查报告_20261018.md §3 P0-2：派发侧
把路径模板（{output_path}/{agent_id}/{novel_title}/{chapter_number}/
{chapter_title}）字面照抄进 acceptance_criteria 时，评估期无人替换、file_check
永久查字面量路径必败。闸门在提交期拒绝并明示改写。

被测行为：_reject_unresolvable_placeholders（模块级纯函数，递归扫描
input_params 字符串值）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOL_DIR = _REPO_ROOT / "plugins" / "shared" / "task_submit"

for _s in (str(_TOOL_DIR), str(_REPO_ROOT / "plugins" / "shared"), str(_REPO_ROOT / "plugins" / "sdk" / "src")):
    if _s not in sys.path:
        sys.path.insert(0, _s)


def _load_module() -> Any:
    mod_name = "task_submit_placeholder_gate_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TOOL_DIR / "tool.py")
    assert spec is not None, "cannot load tool.py"
    assert spec.loader is not None, "cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> Any:
    return _load_module()._reject_unresolvable_placeholders


class TestRejectsDeadPlaceholders:
    def test_output_path_rejected(self, gate: Any) -> None:
        """novel_writer 系死模板：path 自引用 {output_path} → 拒绝并列位。"""
        ac = {"file_check": {"input_params": {"path": "{output_path}"}}}
        result = gate(ac)
        assert result is not None
        assert result.error is not None
        assert "{output_path}" in result.error
        assert "file_check" in result.error  # 定位到指标
        assert result.error_code == "UNRESOLVED_PLACEHOLDER"

    def test_all_six_reported_with_locations(self, gate: Any) -> None:
        """六种死占位符一次全报（错误信息完备性——性质断言：报出集合=发现集合）。"""
        ac = {
            "file_check": {
                "input_params": {
                    "path": "reviews/{chapter_title}_style_review.md",
                    "extra": ["{novel_title}", {"nested": "ch{chapter_number}.md"}],
                }
            },
            "semantic_check": {"input_params": {"target": "{agent_id}.yaml"}},
        }
        result = gate(ac)
        assert result is not None
        for name in ("chapter_title", "novel_title", "chapter_number", "agent_id"):
            assert "{" + name + "}" in result.error

    def test_error_lists_all_metrics_not_only_first(self, gate: Any) -> None:
        """多指标多处出现 → 全部定位（非首错即止）。"""
        ac = {
            "file_check": {"input_params": {"path": "a/{output_path}"}},
            "bash_check": {"input_params": {"command": "cat {output_path}"}},
        }
        result = gate(ac)
        assert result is not None
        assert "file_check" in result.error
        assert "bash_check" in result.error


class TestWhitelistedAndBenign:
    def test_title_whitelisted(self, gate: Any) -> None:
        """{title} 评估期由 task.title 解析 → 放行。"""
        ac = {"file_check": {"input_params": {"path": "docs/working/{title}_research_report.md"}}}
        assert gate(ac) is None

    def test_tool_id_whitelisted(self, gate: Any) -> None:
        """{tool_id} 评估期由工作区唯一候选裁决解析 → 放行。"""
        ac = {"file_check": {"input_params": {"path": "src/tools/builtin/{tool_id}.py"}}}
        assert gate(ac) is None

    def test_regex_quantifier_not_matched(self, gate: Any) -> None:
        r"""正则量词 \d{3} / [a-z]{2,} 非标识符形态 → 放行（无误伤）。"""
        ac = {"semantic_check": {"input_params": {"pattern": r"\d{3}-\w+", "alt": "[a-z]{2,}"}}}
        assert gate(ac) is None

    def test_plain_paths_pass(self, gate: Any) -> None:
        """具体路径（含空格/中文/连字符）→ 放行。"""
        ac = {"file_check": {"input_params": {"path": "docs/working/智能体 调研-report.md"}}}
        assert gate(ac) is None

    def test_double_brace_supported_form_passes(self, gate: Any) -> None:
        """{{workspace}}/{{task_id}} 双花括号是评估期可解析形态 → 放行。"""
        ac = {"file_check": {"input_params": {"path": "{{workspace}}/out/{{task_id}}.md"}}}
        assert gate(ac) is None
