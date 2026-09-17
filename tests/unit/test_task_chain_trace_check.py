# @feature: FP-0.2.二 任务链追溯完整性 | @ci: python-coverage
"""task-chain-viewer 追溯完整性检查测试（ADR 2026-09-17 决策 3）。

任务级 traces_to 对账方案级 AC 总表：断链 = 红旗（HTML 区块 + --check-traces
退出码 1）。方案总纲缺失/无 AC 定义 = 无从对账不产红旗（缺失由 plan_workflow
定稿自检把关，本检查只管已声明 AC 的追溯断链）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "skills" / "skill-task-chain-viewer" / "scripts" / "generate_task_chain_html.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("generate_task_chain_html_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mk_task(mod: Any, task_id: str, traces: list[str]):
    return mod.Task(task_id=task_id, ac_traces=traces)


@pytest.fixture
def mod():
    return _load()


class TestNormalizeAndLoad:
    def test_norm_ac_equivalence(self, mod) -> None:
        assert mod._norm_ac("AC-1") == mod._norm_ac("ac1") == "AC1"

    def test_load_solution_ac_ids_missing_file(self, mod, tmp_path: Path) -> None:
        assert mod.load_solution_ac_ids(tmp_path / "nope.md") == {}

    def test_load_solution_ac_ids_parses_yaml_table(self, mod, tmp_path: Path) -> None:
        doc = tmp_path / "demo_solution.md"
        doc.write_text(
            "# 方案总纲\n\n```yaml\nacceptance_criteria:\n"
            "  - id: AC-1\n    title: 可二值判定\n    must: true\n"
            "  - id: AC-2\n    title: 另一条\n    must: false\n```\n",
            encoding="utf-8",
        )
        ids = mod.load_solution_ac_ids(doc)
        assert set(ids) == {"AC1", "AC2"}
        assert ids["AC1"] == "demo_solution.md"


class TestCheckTraceIntegrity:
    def test_broken_trace_flagged(self, mod) -> None:
        tasks = [_mk_task(mod, "task_01", ["AC-1"]), _mk_task(mod, "task_02", ["AC-9"])]
        broken = mod.check_trace_integrity(tasks, {"AC1": "s.md"})
        assert broken == [{"task_id": "task_02", "trace": "AC-9"}]

    def test_dashless_trace_normalized_match(self, mod) -> None:
        tasks = [_mk_task(mod, "task_01", ["AC2"])]
        assert mod.check_trace_integrity(tasks, {"AC2": "s.md"}) == []

    def test_no_solution_ids_no_flags(self, mod) -> None:
        tasks = [_mk_task(mod, "task_01", ["AC-9"])]
        assert mod.check_trace_integrity(tasks, {}) == []


class TestCliCheckMode:
    def test_check_traces_exit_codes(
        self, mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tasks_dir = tmp_path / "docs" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task_01_ok.md").write_text(
            "---\ntask_id: task_01\nstatus: pending\n---\n\n验收：AC-1（traces_to: AC-1）\n",
            encoding="utf-8",
        )
        (tasks_dir / "task_02_broken.md").write_text(
            "---\ntask_id: task_02\nstatus: pending\n---\n\n验收：AC-9（traces_to: AC-9）\n",
            encoding="utf-8",
        )
        solution = tmp_path / "docs" / "demo_solution.md"
        solution.write_text(
            "```yaml\nacceptance_criteria:\n  - id: AC-1\n```", encoding="utf-8"
        )
        # argv 注入跑 CLI：断链 → 1
        argv = [
            "gen", "--tasks-dir", str(tasks_dir), "--solution-doc", str(solution),
            "--check-traces", "--output", str(tmp_path / "out.html"),
        ]
        monkeypatch.setattr(sys, "argv", argv)
        assert mod.main() == 1
        # 只留可追溯任务 → 0
        (tasks_dir / "task_02_broken.md").unlink()
        assert mod.main() == 0

    def test_render_trace_section_red_and_green(self, mod) -> None:
        green = mod._render_trace_section([])
        assert "✅" in green and "trace-ok" in green
        red = mod._render_trace_section([{"task_id": "task_02", "trace": "AC-9"}])
        assert "🚩" in red and "trace-broken" in red and "task_02" in red
