# @feature: FP-PROMPT 物料整改 | @ci: python-coverage
"""task_evaluate 占位符注入测试（{title} 派发契约 + glob 唯一匹配 + 双花括号回归）。

审查报告 docs/working/提示词Skill物料审查报告_20261018.md §3 P0-2：{title}
按派发侧契约取任务名原文（task.title），评估期中央解析；标题为空不替换
（保留原样可见定位）。{novel_title}/{chapter_number}/{chapter_title}/
{agent_id} 等执行期定值占位符由工作区 glob 唯一匹配解析（0/多命中保留
字面量可见定位）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[0] / "tasks"
_SHARED_DIR = _TE_DIR.parents[0]

for _d in (str(_TE_DIR), str(_TASKS_DIR), str(_SHARED_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)


def _load_module() -> Any:
    mod_name = "task_eval_tool_placeholder_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "tool.py")
    assert spec is not None, "cannot load tool.py"
    assert spec.loader is not None, "cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> Any:
    return _load_module().TaskEvaluateTool()


class TestTitlePlaceholder:
    def test_title_replaced_from_task_title(self, tool: Any) -> None:
        params = {"file_check": {"path": "docs/working/{title}_research_report.md"}}
        tool._inject_workspace_and_placeholders(params, set(params), SimpleNamespace(id="t1", title="智能体调研"), None)
        assert params["file_check"]["path"] == "docs/working/智能体调研_research_report.md"

    def test_title_replaced_everywhere_in_value(self, tool: Any) -> None:
        """同一值内多处 {title} 全替换（幂等完备性）。"""
        params = {"file_check": {"path": "{title}/out/{title}.md"}}
        tool._inject_workspace_and_placeholders(params, set(params), SimpleNamespace(id="t1", title="novel"), None)
        assert params["file_check"]["path"] == "novel/out/novel.md"

    def test_empty_title_leaves_placeholder_visible(self, tool: Any) -> None:
        """标题为空不替换——字面量保留在 file_check 反馈里可定位（不做假值）。"""
        params = {"file_check": {"path": "docs/{title}.md"}}
        tool._inject_workspace_and_placeholders(params, set(params), SimpleNamespace(id="t1", title=""), None)
        assert params["file_check"]["path"] == "docs/{title}.md"

    def test_missing_title_attr_leaves_placeholder(self, tool: Any) -> None:
        """无 title 属性的任务对象（旧形态）不炸、不替换。"""
        params = {"file_check": {"path": "docs/{title}.md"}}
        tool._inject_workspace_and_placeholders(params, set(params), SimpleNamespace(id="t1"), None)
        assert params["file_check"]["path"] == "docs/{title}.md"

    def test_double_brace_still_resolved(self, tool: Any) -> None:
        """回归：{{task_id}}/{{workspace}} 既有解析不受影响。"""
        params = {"file_check": {"path": "{{workspace}}/out/{{task_id}}.md"}}
        tool._inject_workspace_and_placeholders(params, set(params), SimpleNamespace(id="task-9", title="x"), "/ws/abs")
        assert params["file_check"]["path"] == "/ws/abs/out/task-9.md"
        assert params["file_check"]["workspace"] == "/ws/abs"

    def test_title_and_double_brace_compose(self, tool: Any) -> None:
        """混用形态各自解析（组合正确性）。"""
        params = {"file_check": {"path": "{{workspace}}/{title}_{{task_id}}.md"}}
        tool._inject_workspace_and_placeholders(params, set(params), SimpleNamespace(id="tk", title="调研"), "/ws")
        assert params["file_check"]["path"] == "/ws/调研_tk.md"


class TestGlobPlaceholder:
    """{token} 占位符的工作区 glob 唯一匹配解析（P0-2 尾：四种执行期定值）。"""

    def test_relative_unique_match_adopted(self, tool: Any, tmp_path: Path) -> None:
        (tmp_path / "alpha_agent.yaml").write_text("x", encoding="utf-8")
        params = {"file_check": {"path": "{agent_id}.yaml"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == "alpha_agent.yaml"

    def test_absolute_unique_match_adopted(self, tool: Any, tmp_path: Path) -> None:
        hit = tmp_path / "星际战甲_plot_planning.md"
        hit.write_text("x", encoding="utf-8")
        params = {"file_check": {"path": str(tmp_path) + "/{novel_title}_plot_planning.md"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == str(hit)

    def test_nested_dir_pattern_relative_form_preserved(self, tool: Any, tmp_path: Path) -> None:
        reviews = tmp_path / "reviews"
        reviews.mkdir()
        (reviews / "第12章_style_review.md").write_text("x", encoding="utf-8")
        params = {"file_check": {"path": "reviews/{chapter_title}_style_review.md"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == "reviews/第12章_style_review.md"

    def test_multi_match_leaves_literal(self, tool: Any, tmp_path: Path) -> None:
        reviews = tmp_path / "reviews"
        reviews.mkdir()
        (reviews / "a_style_review.md").write_text("x", encoding="utf-8")
        (reviews / "b_style_review.md").write_text("x", encoding="utf-8")
        params = {"file_check": {"path": "reviews/{chapter_title}_style_review.md"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == "reviews/{chapter_title}_style_review.md"

    def test_no_match_leaves_literal(self, tool: Any, tmp_path: Path) -> None:
        params = {"file_check": {"path": "coherence_review_{chapter_number}.md"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == "coherence_review_{chapter_number}.md"

    def test_without_workspace_noop(self, tool: Any) -> None:
        params = {"file_check": {"path": "{agent_id}.yaml"}}
        tool._apply_glob_templates(params, set(params), None)
        assert params["file_check"]["path"] == "{agent_id}.yaml"

    def test_absolute_outside_workspace_untouched(self, tool: Any, tmp_path: Path) -> None:
        params = {"file_check": {"path": "C:/elsewhere/{agent_id}.yaml"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == "C:/elsewhere/{agent_id}.yaml"

    def test_double_brace_value_skipped(self, tool: Any, tmp_path: Path) -> None:
        """含 {{…}} 的值属另一注入面，不进入 glob 解析。"""
        (tmp_path / "a.yaml").write_text("x", encoding="utf-8")
        params = {"file_check": {"path": "{{unknown}}/{agent_id}.yaml"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == "{{unknown}}/{agent_id}.yaml"

    def test_non_path_keys_untouched(self, tool: Any, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text("x", encoding="utf-8")
        params = {"file_check": {"path": "{agent_id}.yaml", "note": "{agent_id} 产出"}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["note"] == "{agent_id} 产出"

    def test_non_string_value_no_crash(self, tool: Any, tmp_path: Path) -> None:
        params = {"file_check": {"path": 5}}
        tool._apply_glob_templates(params, set(params), str(tmp_path))
        assert params["file_check"]["path"] == 5

    def test_metric_not_in_params_not_created(self, tool: Any, tmp_path: Path) -> None:
        params: dict[str, dict[str, Any]] = {}
        tool._apply_glob_templates(params, {"file_check"}, str(tmp_path))
        assert params == {}

    def test_wired_into_get_input_params(self, tool: Any, tmp_path: Path) -> None:
        """端到端接线：acceptance_criteria 占位符经 glob 解析为实际命中路径。"""
        (tmp_path / "beta_agent.yaml").write_text("x", encoding="utf-8")
        task = SimpleNamespace(
            id="t9",
            title="",
            metadata={
                "acceptance_criteria": {"file_check": {"path": "{agent_id}.yaml"}},
                "ws_meta": {"path": str(tmp_path)},
            },
        )
        params = tool._get_input_params(task)
        assert params["file_check"]["path"] == "beta_agent.yaml"
        assert params["file_check"]["workspace"] == str(tmp_path)
