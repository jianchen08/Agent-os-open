# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 评估核心（_eval_core.py）与评估执行器（_executor.py）补充单测。

与 plugins/shared/tools/tests/test_task_evaluate_executor.py 互补：既有测试覆盖
tool 型四种 check / R2 派发契约 / 结论回收主路径；本文件补齐未覆盖分支——

_eval_core：
- sanitize_eval_paths 的 win/posix 绝对路径脱敏、跨盘 relpath ValueError 降级、
  非字符串标量透传；
- EvaluationResult.compute_overall 空/全过/部分通过三种汇总语义。

_executor：
- 指标配置加载的缓存/坏文件/坏条目降级；
- run_evaluation 的空指标列表、单指标异常不连坐；
- tool 型本地执行的缺失 path/超小 not_empty/contains 读取失败/未知 check；
- agent 型派发的无工作区守卫、派发异常/无 pipeline_id、非 dict 响应、
  回收超时、state 行读取异常与非列表行。

执行器全部外部依赖（chat_send/state_rows）注入替身，指标配置用临时 yaml——
零真实内核。
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[1] / "system" / "tasks"

for _d in (_TE_DIR, _TASKS_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _assert_no_abs_path_prefix(s: str) -> None:
    """脱敏后不得残留独立绝对路径（如 /tmp/、/home/、D:\\）。

    相对化结果形如 ../../tmp/leaked.txt：斜杠前已有路径成分，与"独立绝对路径"
    （斜杠前是空白/冒号/行首）可区分——用前向断言而不是字面量存在性。
    """
    assert re.search(r"(?<![./])\s/(?:tmp|home|var|root|opt|usr)/", s) is None, f"仍含绝对路径前缀: {s!r}"
    assert not re.search(r"^[A-Za-z]:[\\/]", s), f"仍含盘符绝对路径: {s!r}"


def _load_module(name: str, file_name: str) -> Any:
    """按唯一模块名加载（防与其它平铺测试互相污染 sys.modules）。"""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TE_DIR / file_name)
    assert spec is not None and spec.loader is not None, f"cannot load {file_name}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def core() -> Any:
    """_eval_core 模块（评估核心类型面）。"""
    return _load_module("task_eval_core_extra_under_test", "_eval_core.py")


@pytest.fixture
def exec_mod() -> Any:
    """_executor 模块（时间常量已压短，避免无结论用例空转）。"""
    m = _load_module("task_eval_executor_extra_under_test", "_executor.py")
    m._POLL_INTERVAL_S = 0.01
    m._AGENT_RECOVER_TIMEOUT_S = 0.3
    return m


def _metrics_yaml(*names: str, extra: list[Any] | None = None) -> str:
    lines = ["metrics:"]
    for n in names:
        if n == "semantic_check":
            lines.append(f"  - name: {n}\n    evaluator_type: agent")
        else:
            lines.append(f"  - name: {n}\n    evaluator_type: tool")
    for item in extra or []:
        lines.append("  - " + str(item))
    return "\n".join(lines)


@pytest.fixture
def metrics_path(tmp_path: Path) -> str:
    p = tmp_path / "evaluation_metrics.yaml"
    p.write_text(_metrics_yaml("file_check", "semantic_check"), encoding="utf-8")
    return str(p)


def _make_executor(mod: Any, metrics: str | None = None, chat_send: Any = None, state_rows: Any = None) -> Any:
    return mod.PipelineEvaluationExecutor(
        chat_send=chat_send or AsyncMock(return_value={"pipeline_id": "evalPipe1"}),
        state_rows=state_rows or (lambda: []),
        metrics_config_path=metrics,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── _eval_core：sanitize_eval_paths 脱敏语义 ─────────────────


class TestSanitizeEvalPaths:
    """递归脱敏：字典/列表/字符串，防服务器内部路径泄漏。"""

    def test_dict_list_scalars_passthrough(self, core: Any) -> None:
        data = {"a": ["x", 1, None], "b": {"c": 3.5, "d": True}}
        out = core.sanitize_eval_paths(data)
        # 原地修改并返回同一对象
        assert out is data
        assert out == {"a": ["x", 1, None], "b": {"c": 3.5, "d": True}}

    def test_cwd_abs_win_replaced(self, core: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(core, "_CWD_ABS_WIN", "D:\\proj\\")
        s = core.sanitize_eval_paths("base D:\\proj\\a\\b.txt tail")
        assert "D:\\proj\\" not in s
        assert s.startswith("base ") and s.endswith(" tail")

    def test_cwd_abs_posix_replaced(self, core: Any, monkeypatch: Any) -> None:
        monkeypatch.setattr(core, "_CWD_ABS", "D:/proj/")
        s = core.sanitize_eval_paths("base D:/proj/a/b.txt tail")
        assert "D:/proj/" not in s
        assert s.startswith("base ") and s.endswith(" tail")

    def test_posix_abs_paths_made_relative(self, core: Any) -> None:
        s = core.sanitize_eval_paths("log: /tmp/leaked.txt done")
        # 绝对路径前缀必须被替换为相对路径（防泄漏），保留文件名供定位
        _assert_no_abs_path_prefix(s)
        assert "leaked.txt" in s
        assert s.startswith("log: ") and s.endswith(" done")

    def test_home_and_var_abs_paths_made_relative(self, core: Any) -> None:
        s = core.sanitize_eval_paths("a /home/u/f.txt b /var/log/x c")
        _assert_no_abs_path_prefix(s)
        assert "home/u/f.txt" in s and "var/log/x" in s

    def test_cross_drive_win_path_valueerror_degrade(self, core: Any) -> None:
        """跨盘符相对化抛 ValueError → 原样保留（不炸评估调用）。"""
        s = core.sanitize_eval_paths(r"x E:\tmp\leaked.txt y")
        assert s == "x E:\\tmp\\leaked.txt y"

    def test_same_drive_win_path_relativized(self, core: Any, monkeypatch: Any) -> None:
        """同盘符 Windows 绝对路径成功相对化（relpath 成功分支）。"""
        # 用不存在的哨兵前缀绕过 cwd 脱敏分支，保证走盘符正则分支
        monkeypatch.setattr(core, "_CWD_ABS_WIN", "Z:\\no_such\\")
        monkeypatch.setattr(core, "_CWD_ABS", "Z:/no_such/")
        s = core.sanitize_eval_paths(r"x D:\proj\a\b.txt y")
        assert r"D:\proj\a\b.txt" not in s
        assert "b.txt" in s
        assert s.startswith("x ") and s.endswith(" y")

    def test_posix_relpath_valueerror_degrade(self, core: Any, monkeypatch: Any) -> None:
        """posix 相对化抛 ValueError → 原样保留（不炸评估调用）。"""

        def _boom(path: str, start: str | None = None) -> str:
            raise ValueError("cross device")

        monkeypatch.setattr(core.os.path, "relpath", _boom)
        s = core.sanitize_eval_paths("p /tmp/x.txt q")
        assert s == "p /tmp/x.txt q"

    def test_nested_structs_recursed(self, core: Any) -> None:
        out = core.sanitize_eval_paths({"p1": ["/tmp/leaked.txt", 42], "p2": {"k": "/home/u/f.txt"}})
        _assert_no_abs_path_prefix(str(out["p1"]))
        _assert_no_abs_path_prefix(str(out["p2"]))
        assert 42 in out["p1"]
        # 相对化结果仍含定位信息（不是被清空）
        assert "tmp/leaked.txt" in str(out["p1"])

    def test_scalar_values_returned_as_is(self, core: Any) -> None:
        assert core.sanitize_eval_paths(None) is None
        assert core.sanitize_eval_paths(42) == 42
        assert core.sanitize_eval_paths(3.14) == 3.14


# ── _eval_core：评估结果汇总语义 ─────────────────────────────


class TestEvaluationResultCompute:
    """compute_overall：无指标/全过/部分通过三种汇总。"""

    def test_empty_results_marks_failed(self, core: Any) -> None:
        r = core.EvaluationResult(task_id="t1")
        r.compute_overall()
        assert r.overall_passed is False
        assert r.summary == "无评估指标"

    def test_all_passed_summary(self, core: Any) -> None:
        r = core.EvaluationResult(
            task_id="t1",
            results=[core.MetricResult(metric_id="a", passed=True), core.MetricResult(metric_id="b", passed=True)],
        )
        r.compute_overall()
        assert r.overall_passed is True
        assert r.summary == "全部 2 项指标通过"

    def test_partial_passed_summary(self, core: Any) -> None:
        r = core.EvaluationResult(
            task_id="t1",
            results=[core.MetricResult(metric_id="a", passed=True), core.MetricResult(metric_id="b", passed=False)],
        )
        r.compute_overall()
        assert r.overall_passed is False
        assert r.summary == "1/2 项指标通过"

    def test_metric_result_defaults(self, core: Any) -> None:
        m = core.MetricResult(metric_id="m")
        assert m.passed is False
        assert m.score == -1.0
        assert m.details == {} and m.evaluator_input == {} and m.evaluator_output == {}
        assert m.error is None and m.pipeline_run_id is None and m.message == ""


# ── _executor：指标配置加载 ──────────────────────────────────


class TestMetricLoading:
    def test_default_metrics_path_points_to_config(self, exec_mod: Any) -> None:
        p = exec_mod.PipelineEvaluationExecutor._default_metrics_path()
        assert p.endswith("evaluation_metrics.yaml")

    def test_cache_keeps_loaded_metrics(self, exec_mod: Any, tmp_path: Path) -> None:
        """加载一次后缓存：配置文件消失不影响后续评估（指标定义仍可用）。"""
        cfg = tmp_path / "m.yaml"
        cfg.write_text(_metrics_yaml("file_check"), encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=str(cfg))
        r1 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "x"}}))
        cfg.unlink()
        r2 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "x"}}))
        assert r1.results[0].passed is False
        assert r2.results[0].passed is False
        assert "未在 evaluation_metrics.yaml 定义" not in (r2.results[0].error or "")

    def test_missing_config_degrades_to_empty_table(self, exec_mod: Any, tmp_path: Path) -> None:
        ex = _make_executor(exec_mod, metrics=str(tmp_path / "gone.yaml"))
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "x"}}))
        assert r.results[0].passed is False
        assert "未在 evaluation_metrics.yaml 定义" in (r.results[0].error or "")

    def test_bad_yaml_degrades_to_empty_table(self, exec_mod: Any, tmp_path: Path) -> None:
        cfg = tmp_path / "m.yaml"
        cfg.write_text("::: not yaml :::", encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=str(cfg))
        r = _run(ex.run_evaluation("t1", ["file_check"], {}))
        assert r.results[0].passed is False
        assert "未在 evaluation_metrics.yaml 定义" in (r.results[0].error or "")

    def test_invalid_metric_entries_skipped(self, exec_mod: Any, tmp_path: Path) -> None:
        cfg = tmp_path / "m.yaml"
        cfg.write_text(
            _metrics_yaml("file_check", extra=["plain_string", 42]),
            encoding="utf-8",
        )
        ex = _make_executor(exec_mod, metrics=str(cfg))
        # 合法条目正常评估
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "x"}}))
        assert "未在 evaluation_metrics.yaml 定义" not in (r.results[0].error or "")
        # 非 dict 条目不进表 → 诚实失败
        r2 = _run(ex.run_evaluation("t1", ["plain_string"], {}))
        assert r2.results[0].passed is False
        assert "未在 evaluation_metrics.yaml 定义" in (r2.results[0].error or "")


# ── _executor：run_evaluation 编排 ───────────────────────────


class TestRunEvaluationOrchestration:
    def test_no_metric_ids_empty_result(self, exec_mod: Any, metrics_path: str) -> None:
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(ex.run_evaluation("t1", None, None))
        assert r.results == []
        assert r.overall_passed is False
        assert r.summary == "无评估指标"

    def test_multiple_undefined_metrics_no_collateral(self, exec_mod: Any, metrics_path: str) -> None:
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(ex.run_evaluation("t1", ["ghost_a", "ghost_b"], {}))
        assert len(r.results) == 2
        assert all(not m.passed for m in r.results)
        assert all("未在 evaluation_metrics.yaml 定义" in (m.error or "") for m in r.results)

    def test_metric_exception_does_not_collapse_run(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        """单指标评估炸（min_size 非数字 → int() ValueError）不连坐其它指标。"""
        f = tmp_path / "log.txt"
        f.write_text("hello", encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(
            ex.run_evaluation(
                "t1",
                ["file_check", "file_check"],
                {"file_check": {"path": str(f), "check": "not_empty", "min_size": "abc"}},
            )
        )
        assert len(r.results) == 2
        assert r.results[0].passed is False
        assert "评估异常" in (r.results[0].error or "")


# ── _executor：tool 型本地执行 ───────────────────────────────


class TestToolMetricLocalExtra:
    def test_missing_path_param(self, exec_mod: Any, metrics_path: str) -> None:
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {}}))
        assert r.results[0].passed is False
        assert "path 参数缺失" in (r.results[0].error or "")

    def test_exists_ok_and_missing(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        f = tmp_path / "out.txt"
        f.write_text("hi", encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r1 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f)}}))
        r2 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(tmp_path / "nope")}}))
        assert r1.results[0].passed is True
        assert "存在" in r1.results[0].message
        assert r2.results[0].passed is False
        assert "不存在" in r2.results[0].message

    def test_is_directory_ok_and_not(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r1 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(tmp_path / "sub"), "check": "is_directory"}}))
        r2 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(tmp_path / "out.txt"), "check": "is_directory"}}))
        assert r1.results[0].passed is True
        assert r2.results[0].passed is False

    def test_relative_path_resolved_by_workspace(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        (tmp_path / "sub").mkdir()
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(
            ex.run_evaluation(
                "t1",
                ["file_check"],
                {"file_check": {"path": "sub", "check": "is_directory", "workspace": str(tmp_path)}},
            )
        )
        assert r.results[0].passed is True

    def test_not_empty_min_size_ok_and_too_small(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("hello world", encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r1 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f), "check": "not_empty", "min_size": 5}}))
        r2 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f), "check": "not_empty", "min_size": 999}}))
        assert r1.results[0].passed is True
        assert r2.results[0].passed is False

    def test_contains_hit_and_miss(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        f = tmp_path / "log.txt"
        f.write_text("hello world", encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r1 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f), "check": "contains", "pattern": "world"}}))
        r2 = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f), "check": "contains", "pattern": "absent"}}))
        assert r1.results[0].passed is True
        assert r2.results[0].passed is False
        assert "未包含模式" in r2.results[0].message

    def test_contains_unreadable_fails_cleanly(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        """contains 目标为目录 → 读失败 → 诚实错误（不崩）。"""
        (tmp_path / "adir").mkdir()
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(tmp_path / "adir"), "check": "contains", "pattern": "x"}}))
        assert r.results[0].passed is False
        assert "读取失败" in (r.results[0].error or "")

    def test_unknown_check_type(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(tmp_path), "check": "magic"}}))
        assert r.results[0].passed is False
        assert "未知检查类型" in (r.results[0].error or "")

    def test_default_check_is_exists(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        ex = _make_executor(exec_mod, metrics=metrics_path)
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f)}}))
        assert r.results[0].passed is True


# ── _executor：agent 型评估子管道 ────────────────────────────


class TestAgentMetricPipelineExtra:
    def _task_rows(self, ws_path: str | None) -> list[dict[str, Any]]:
        row: dict[str, Any] = {"pipeline_id": "taskP1"}
        if ws_path is not None:
            row["ws_meta"] = {"path": ws_path}
            row["lineage.origin_session_id"] = "sessRoot1"
        return [row]

    def test_no_ws_meta_fails_r2_guard(self, exec_mod: Any, metrics_path: str) -> None:
        ex = _make_executor(exec_mod, state_rows=(lambda: [{"pipeline_id": "taskP1"}]), metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "无工作区坐标" in (r.results[0].error or "")

    def test_non_dict_ws_meta_fails_r2_guard(self, exec_mod: Any, metrics_path: str) -> None:
        ex = _make_executor(exec_mod, state_rows=(lambda: [{"pipeline_id": "taskP1", "ws_meta": "flat"}]), metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "无工作区坐标" in (r.results[0].error or "")

    def test_dispatch_raise_fails_metric(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        async def boom(_params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("chat down")

        ex = _make_executor(exec_mod, chat_send=boom, state_rows=(lambda: self._task_rows(str(ws))), metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "派发失败" in (r.results[0].error or "")

    def test_dispatch_non_dict_response_fails(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        async def non_dict(_params: dict[str, Any]) -> Any:
            return ["not", "dict"]

        ex = _make_executor(exec_mod, chat_send=non_dict, state_rows=(lambda: self._task_rows(str(ws))), metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "未返回 pipeline_id" in (r.results[0].error or "")

    def test_dispatch_empty_pipeline_id_fails(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        ex = _make_executor(exec_mod, chat_send=AsyncMock(return_value={}), state_rows=(lambda: self._task_rows(str(ws))), metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "未返回 pipeline_id" in (r.results[0].error or "")

    def test_recover_detected_result_first_poll(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        async def state_rows() -> list[dict[str, Any]]:
            return [
                *self._task_rows(str(ws)),
                {
                    "pipeline_id": "evalPipe1",
                    "evaluation.detected_result": {"passed": True, "score": 75, "feedback": "产出完整"},
                },
            ]

        ex = _make_executor(exec_mod, chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}), state_rows=state_rows, metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        m = r.results[0]
        assert m.passed is True
        assert m.score == 75.0
        assert m.message == "产出完整"
        assert m.pipeline_run_id == "evalPipe1"
        assert m.evaluator_output == {"passed": True, "score": 75, "feedback": "产出完整"}

    def test_recover_non_numeric_score_defaults_minus_one(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        async def state_rows() -> list[dict[str, Any]]:
            return [
                *self._task_rows(str(ws)),
                {"pipeline_id": "evalPipe1", "evaluation.detected_result": {"passed": False, "score": "high", "feedback": ""}},
            ]

        ex = _make_executor(exec_mod, chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}), state_rows=state_rows, metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert r.results[0].score == -1.0

    def test_recover_cancelled_terminal(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        async def state_rows() -> list[dict[str, Any]]:
            return [*self._task_rows(str(ws)), {"pipeline_id": "evalPipe1", "task.status": "cancelled"}]

        ex = _make_executor(exec_mod, chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}), state_rows=state_rows, metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "cancelled" in (r.results[0].error or "")

    def test_recover_timeout_honest_failure(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        # 无结论也无终态 → 轮询到内部回收上限 → 诚实失败
        ex = _make_executor(exec_mod, chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}), state_rows=(lambda: self._task_rows(str(ws))), metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "回收超时" in (r.results[0].error or "")
        assert r.results[0].pipeline_run_id == "evalPipe1"

    def test_state_rows_raise_and_non_list(self, exec_mod: Any, metrics_path: str, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()

        async def boom() -> Any:
            raise RuntimeError("state down")

        ex = _make_executor(exec_mod, chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}), state_rows=boom, metrics=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "无工作区坐标" in (r.results[0].error or "")

        ex2 = _make_executor(exec_mod, chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}), state_rows=(lambda: "not-a-list"), metrics=metrics_path)
        r2 = _run(ex2.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r2.results[0].passed is False
        assert "无工作区坐标" in (r2.results[0].error or "")
