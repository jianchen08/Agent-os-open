# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""评估结论回收的健壮性锁：截止前最后一次读取 + raw_result 兜底 + 严格归一。

2026-11-12 审计三条：

1. 轮询到点即判超时——结论恰在最后一次轮询与截止时刻之间落库会被丢
   （现：截止后再读一次 state 行）；
2. 检测面缺席（task_reminder 未执行/未命中）时无任何回退——结论明明已在
   ``raw_result`` 文本里，却只能诚实失败（现：用 ``_libs.evaluation_result``
   同源解析做最终兜底）；
3. ``bool(detected["passed"])`` 会把历史行的 ``"passed": "false"`` 判成通过
   （现：``normalize_passed`` 严格归一，非法形态按未决处理）。

无证据时不臆造：state 行不可读 / 终态非失败但无结论 → 显式失败并在 error 里
写明实际数据面。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
for _d in (_TE_DIR, _TE_DIR.parents[0] / "tasks"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_executor() -> Any:
    name = "task_evaluate_recovery_fallback_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _TE_DIR / "_executor.py")
    assert spec is not None, "cannot load _executor.py"
    assert spec.loader is not None, "cannot load _executor.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _executor(mod: Any, state_rows: Any) -> Any:
    return mod.PipelineEvaluationExecutor(
        chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}),
        state_rows=state_rows,
        metrics_config_path="unused-for-recovery.yaml",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def exec_mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    """执行器模块：回收上限压到 0（跳过轮询体，专测截止后那次读取）。"""
    m = _load_executor()
    monkeypatch.setattr(m, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(m, "_AGENT_RECOVER_TIMEOUT_S", 0.0)
    return m


class TestDeadlineFinalRead:
    def test_result_landed_at_deadline_is_not_lost(self, exec_mod: Any) -> None:
        """轮询体零轮次（超时 0s）时，结论仍靠截止后那次读取回收。"""
        rows = [
            {
                "pipeline_id": "evalPipe1",
                "evaluation.detected_result": {
                    "passed": True,
                    "score": 92,
                    "feedback": "达标",
                },
            }
        ]
        result = _run(_executor(exec_mod, lambda: rows)._recover_result("evalPipe1", "m1"))
        assert result.passed is True
        assert result.score == 92.0
        assert result.message == "达标"

    def test_reader_result_used_when_polling_body_skipped(self, exec_mod: Any) -> None:
        """轮询体被跳过时读取仍发生（截止后读取是独立数据面，不是轮询副产物）。"""
        calls: list[int] = []
        rows_final = [
            {
                "pipeline_id": "evalPipe1",
                "evaluation.detected_result": {"passed": False, "score": 40},
            }
        ]

        def reader() -> list[dict[str, Any]]:
            calls.append(1)
            return rows_final

        result = _run(_executor(exec_mod, reader)._recover_result("evalPipe1", "m1"))
        assert result.passed is False
        assert result.score == 40.0
        assert calls, "回收必须真的读过 state 行"


class TestRawResultFallback:
    def test_detected_result_missing_falls_back_to_raw_result_json(
        self, exec_mod: Any
    ) -> None:
        """检测面缺席但结论在 raw_result 文本里 → 兜底回收，不判超时。"""
        rows = [
            {
                "pipeline_id": "evalPipe1",
                "task.status": "running",
                "raw_result": (
                    "评估完成。\n```json\n"
                    '{"evaluation_result": {"passed": false, "score": "40", '
                    '"feedback": "未达标", "suggestions": ["补测试"]}}\n```'
                ),
            }
        ]
        result = _run(_executor(exec_mod, lambda: rows)._recover_result("evalPipe1", "semantic_check"))
        assert result.passed is False
        assert result.score == 40.0
        assert result.message == "未达标"
        assert result.evaluator_output["suggestions"] == ["补测试"]
        assert result.error is None

    def test_legacy_detected_result_string_passed_is_not_truthy(
        self, exec_mod: Any
    ) -> None:
        """历史行 ``"passed": "false"`` 不得被 bool() 判成通过（方向危险）。"""
        rows = [
            {
                "pipeline_id": "evalPipe1",
                "evaluation.detected_result": {"passed": "false", "score": "N/A"},
            }
        ]
        result = _run(_executor(exec_mod, lambda: rows)._recover_result("evalPipe1", "m1"))
        assert result.passed is False
        assert result.score == -1.0

    def test_invalid_detected_payload_falls_through_to_text(self, exec_mod: Any) -> None:
        """detected_result.passed 形态非法（1）→ 按未决处理，交 raw_result 兜底。"""
        rows = [
            {
                "pipeline_id": "evalPipe1",
                "evaluation.detected_result": {"passed": 1, "score": 90},
                "raw_result": '{"evaluation_result": {"passed": true, "score": 90}}',
            }
        ]
        result = _run(_executor(exec_mod, lambda: rows)._recover_result("evalPipe1", "m1"))
        assert result.passed is True
        assert result.score == 90.0


class TestNoEvidenceHonestFailure:
    def test_running_without_conclusion_reports_status(self, exec_mod: Any) -> None:
        """终态非失败且无结论 → 显式失败，error 写实际数据面（不臆造通过）。"""
        rows = [
            {"pipeline_id": "evalPipe1", "task.status": "running", "raw_result": "还在跑"}
        ]
        result = _run(_executor(exec_mod, lambda: rows)._recover_result("evalPipe1", "m1"))
        assert result.passed is False
        error = result.error or ""
        assert "回收超时" in error
        assert "running" in error
        assert "无结论证据" in error

    def test_missing_state_row_reports_unreadable(self, exec_mod: Any) -> None:
        result = _run(_executor(exec_mod, lambda: [])._recover_result("evalPipe1", "m1"))
        assert result.passed is False
        assert "state 行不可读" in (result.error or "")

    def test_failed_status_beats_text_fallback(self, exec_mod: Any) -> None:
        """子管道已失败 → 结论不采信（fail-closed），错误带真终态词。"""
        rows = [
            {
                "pipeline_id": "evalPipe1",
                "task.status": "failed",
                "raw_result": '{"evaluation_result": {"passed": true, "score": 100}}',
            }
        ]
        result = _run(_executor(exec_mod, lambda: rows)._recover_result("evalPipe1", "m1"))
        assert result.passed is False
        assert "failed" in (result.error or "")
