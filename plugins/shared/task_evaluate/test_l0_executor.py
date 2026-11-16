# @feature: FP-0.2.CFG 配置系统与插件配置注入 | @vision: V3 可嵌入 | @ci: python-coverage
"""L0 检查规则执行面单测（ADR 2026-10-27 步骤 2：L0 指标化观察模式）。

覆盖 task_evaluate 执行器为三条 L0 通用指标新增的两条执行路径：

- matches_schema（artifact_schema_check 的执行载体）：产物 YAML/JSON 结构校验
  ——必填字段存在且非空、字段类型、数组最小长度；违规聚合报。
- scope 一致性（scope_consistency 的执行载体）：git 改动文件清单 ⊆ 改动面绑定表
  （allowed_paths），越界清单进 message；缺参/非 git 仓库诚实失败。

意图：观察模式的误报率数据必须来自真实执行——执行路径不存在时指标引用必须
诚实失败（错误可辨），存在时按契约判定；本文件即该契约的行为锚。

执行器经 importlib 单名加载（防平铺测试互相污染 sys.modules，与
test_eval_core_executor.py 同模式）；scope 用例走真实 git 子进程（外部依赖
不 mock——关键路径至少一条真实依赖断言）。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent

if str(_TE_DIR) not in sys.path:
    sys.path.insert(0, str(_TE_DIR))


def _load_executor() -> Any:
    """按唯一模块名加载 _executor（防与其它平铺测试互相污染 sys.modules）。"""
    name = "task_eval_l0_executor_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TE_DIR / "_executor.py")
    assert spec is not None and spec.loader is not None, "cannot load _executor.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def executor() -> Any:
    """被测执行器模块。"""
    return _load_executor()


def _run_tool_check(executor: Any, metric_id: str, params: dict[str, Any]) -> Any:
    """走 tool 型本地执行公共入口（含路由语义，行为级断言）。"""
    return executor.PipelineEvaluationExecutor._evaluate_tool_local(metric_id, params)


# ── matches_schema（artifact_schema_check 执行载体）───────────────────────────


class TestMatchesSchemaCheck:
    """产物结构校验契约：必填字段存在且非空 + 类型 + min_items，违规聚合报。"""

    def _write_report(self, base: Path, payload: dict[str, Any]) -> Path:
        target = base / "execution_report.yaml"
        target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
        return target

    def test_passes_when_required_fields_present_and_nonempty(
        self, executor: Any, tmp_path: Path
    ) -> None:
        # 意图：结构合法的执行报告必须通过——AC 清单非空是闸门证据的最低要求
        report = self._write_report(tmp_path, {"summary": "done", "ac_list": ["AC1", "AC2"]})
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {"required": ["summary", "ac_list"]},
        })
        assert result.passed is True, result.message or result.error

    def test_passes_with_second_input_shape(
        self, executor: Any, tmp_path: Path
    ) -> None:
        # 第二组有区分度输入：不同字段结构 + min_items 满足（防单点拟合）
        report = self._write_report(
            tmp_path, {"report": "x", "metrics": [{"id": 1}, {"id": 2}]}
        )
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {
                "required": ["metrics"],
                "properties": {"metrics": {"type": "array", "min_items": 2}},
            },
        })
        assert result.passed is True, result.message or result.error

    def test_fails_and_names_missing_required_field(
        self, executor: Any, tmp_path: Path
    ) -> None:
        # 意图：失败信息必须点名缺失字段（观察期误报归因依赖可读 message）
        report = self._write_report(tmp_path, {"summary": "done"})
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {"required": ["summary", "ac_list"]},
        })
        assert result.passed is False
        assert "ac_list" in (result.message or ""), f"message 应指名缺失字段: {result.message!r}"

    def test_fails_when_array_below_min_items(
        self, executor: Any, tmp_path: Path
    ) -> None:
        # AC 清单为空 = 无验收证据，必须红（min_items 契约）
        report = self._write_report(tmp_path, {"summary": "done", "ac_list": []})
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {
                "required": ["ac_list"],
                "properties": {"ac_list": {"type": "array", "min_items": 1}},
            },
        })
        assert result.passed is False
        assert "ac_list" in (result.message or "")

    def test_fails_when_file_missing(self, executor: Any, tmp_path: Path) -> None:
        # 执行报告不存在 = 证据缺失，诚实失败且错误可辨（非"未知检查类型"）
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(tmp_path / "no_such_report.yaml"),
            "check": "matches_schema",
            "schema": {"required": ["summary"]},
        })
        assert result.passed is False
        assert "不存在" in (result.error or result.message or "")

    def test_fails_on_type_mismatch(self, executor: Any, tmp_path: Path) -> None:
        report = self._write_report(tmp_path, {"summary": "done", "ac_list": "not-a-list"})
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {
                "required": ["ac_list"],
                "properties": {"ac_list": {"type": "array"}},
            },
        })
        assert result.passed is False
        assert "ac_list" in (result.message or "")

    def test_stricter_schema_monotonically_shrinks_pass_set(
        self, executor: Any, tmp_path: Path
    ) -> None:
        # 性质断言：同一产物，schema 严格度提升不得使通过集合扩大（单调性）
        report = self._write_report(tmp_path, {"summary": "done", "ac_list": []})
        loose = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {"required": ["summary"]},
        })
        strict = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {
                "required": ["summary", "ac_list"],
                "properties": {"ac_list": {"type": "array", "min_items": 1}},
            },
        })
        assert loose.passed is True
        assert strict.passed is False  # 严格通过集 ⊆ 宽松通过集

    def test_aggregates_all_violations_in_message(
        self, executor: Any, tmp_path: Path
    ) -> None:
        # 意图：聚合语义——一次报出全部违规，观察期不用多轮往返归因
        report = self._write_report(tmp_path, {})
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {"required": ["summary", "ac_list"]},
        })
        assert result.passed is False
        msg = result.message or ""
        assert "summary" in msg and "ac_list" in msg

    def test_json_product_supported(self, executor: Any, tmp_path: Path) -> None:
        # JSON 产物同契约（yaml 解析器是 JSON 超集）
        report = tmp_path / "execution_report.json"
        report.write_text('{"summary": "done", "ac_list": ["AC1"]}', encoding="utf-8")
        result = _run_tool_check(executor, "artifact_schema_check", {
            "path": str(report),
            "check": "matches_schema",
            "schema": {"required": ["summary", "ac_list"]},
        })
        assert result.passed is True, result.message or result.error


# ── scope 一致性（scope_consistency 执行载体）────────────────────────────────


class TestScopeConsistencyCheck:
    """改动面一致性契约：改动文件清单 ⊆ allowed_paths，越界报清单；缺参诚实失败。"""

    @pytest.fixture
    def git_repo(self, tmp_path: Path) -> Path:
        """真实 git 仓库（外部依赖不 mock——关键路径真实依赖断言）。"""
        repo = tmp_path / "ws"
        repo.mkdir()

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(repo), *args],
                check=True, capture_output=True, text=True,
            )

        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "tester")
        (repo / "README.md").write_text("base\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-qm", "init")
        return repo

    def test_passes_when_change_within_allowed_dir(
        self, executor: Any, git_repo: Path
    ) -> None:
        # 允许目录内新增文件 → 通过（目录前缀匹配语义）
        src = git_repo / "src"
        src.mkdir()
        (src / "auth.py").write_text("ok = True\n", encoding="utf-8")
        result = _run_tool_check(executor, "scope_consistency", {
            "workspace": str(git_repo),
            "allowed_paths": ["src"],
        })
        assert result.passed is True, result.message or result.error

    def test_passes_with_exact_file_binding(
        self, executor: Any, git_repo: Path
    ) -> None:
        # 第二组有区分度输入：绑定表精确到文件（文件级精确匹配语义）
        (git_repo / "README.md").write_text("changed\n", encoding="utf-8")
        result = _run_tool_check(executor, "scope_consistency", {
            "workspace": str(git_repo),
            "allowed_paths": ["README.md"],
        })
        assert result.passed is True, result.message or result.error

    def test_fails_and_lists_outside_change(
        self, executor: Any, git_repo: Path
    ) -> None:
        # 越界改动必须失败且 message 点名越界文件（观察期归因）
        docs = git_repo / "docs"
        docs.mkdir()
        (docs / "outside.md").write_text("x\n", encoding="utf-8")
        result = _run_tool_check(executor, "scope_consistency", {
            "workspace": str(git_repo),
            "allowed_paths": ["src"],
        })
        assert result.passed is False
        assert "docs/outside.md" in (result.message or "")

    def test_violation_set_is_exact_difference(
        self, executor: Any, git_repo: Path
    ) -> None:
        # 性质断言：越界集 = 改动集 − 允许集（恰含外部、不含内部）
        src = git_repo / "src"
        src.mkdir()
        (src / "inside.py").write_text("a\n", encoding="utf-8")
        other = git_repo / "other"
        other.mkdir()
        (other / "outside.py").write_text("b\n", encoding="utf-8")
        result = _run_tool_check(executor, "scope_consistency", {
            "workspace": str(git_repo),
            "allowed_paths": ["src"],
        })
        assert result.passed is False
        msg = result.message or ""
        assert "other/outside.py" in msg
        assert "src/inside.py" not in msg

    def test_fails_outside_git_repo(self, executor: Any, tmp_path: Path) -> None:
        # 非 git 目录 = 无法取证，诚实失败（错误可辨，不是"不存在"这类误报源）
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "x.txt").write_text("x\n", encoding="utf-8")
        result = _run_tool_check(executor, "scope_consistency", {
            "workspace": str(plain),
            "allowed_paths": ["."],
        })
        assert result.passed is False
        assert "git" in (result.error or "").lower()

    def test_fails_when_allowed_paths_missing(
        self, executor: Any, git_repo: Path
    ) -> None:
        # 无绑定表 = 无授权面，不放行（诚实语义：缺失 ≠ 全部允许）
        (git_repo / "a.py").write_text("x\n", encoding="utf-8")
        result = _run_tool_check(executor, "scope_consistency", {
            "workspace": str(git_repo),
        })
        assert result.passed is False
        assert "allowed_paths" in (result.error or "")
