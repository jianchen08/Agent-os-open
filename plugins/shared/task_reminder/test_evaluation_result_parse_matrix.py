# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""evaluation_result 解析矩阵（fail-closed 契约锁）。

2026-11-12 审计：``_detect_evaluation_result_json`` 解析过宽——``bool("false")``
= True 会把失败评估判成通过（方向危险）；``float(None)`` / 非数值 score 抛异常
（评估子管道收束面炸掉）。解析单源已收敛到 ``_libs/evaluation_result.py``
（task_evaluate 回收兜底共用），本文件锁死其输入矩阵：

- passed：仅 bool 或大小写不敏感 "true"/"false"；其余类型（1/0/"yes"/null）
  不构成证据 → 无结论（且不回头采信更早的示例 JSON，防判通过）；
- score：int/float（bool 除外）或有限数值字符串；null/"N/A"/nan/inf → None；
- payload 形状：嵌套 evaluation_result dict / 顶层 passed；非 dict JSON 与
  散文包裹按候选扫描处理，坏 payload 返回 None 不抛。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/
for _d in [_DIR, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "task_reminder_plugin_parse_matrix_test", str(_DIR / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["task_reminder_plugin_parse_matrix_test"] = _mod
_spec.loader.exec_module(_mod)
TaskReminder = _mod.TaskReminder  # noqa: E402


def _detect(text: str) -> dict[str, Any] | None:
    return TaskReminder._detect_evaluation_result_json(text)


PASSED_MATRIX = [
    # (text, expected passed)
    ('{"passed": true}', True),
    ('{"passed": false}', False),
    ('{"passed": "true"}', True),
    ('{"passed": "false"}', False),
    ('{"passed": "FALSE"}', False),
    ('{"passed": " True "}', True),
    ('{"evaluation_result": {"passed": false, "score": 40}}', False),
    ('前缀说明 {"evaluation_result": {"passed": true}} 后缀说明', True),
    ('```json\n{"evaluation_result": {"passed": false, "score": 92}}\n```', False),
]


@pytest.mark.parametrize(("text", "expected"), PASSED_MATRIX)
def test_passed_accepted_forms(text: str, expected: bool) -> None:
    detected = _detect(text)
    assert detected is not None
    assert detected["passed"] is expected


INVALID_PASSED_MATRIX = [
    '{"passed": 1}',
    '{"passed": 0}',
    '{"passed": null}',
    '{"passed": "yes"}',
    '{"passed": "1"}',
    '{"evaluation_result": {"passed": 1, "score": 90}}',
    '{"evaluation_result": []}',
    '{"evaluation_result": "passed"}',
]


@pytest.mark.parametrize("text", INVALID_PASSED_MATRIX)
def test_passed_invalid_type_is_not_evidence(text: str) -> None:
    """非 bool / 非 "true"/"false" 字符串 → 不构成证据（绝不判通过）。"""
    assert _detect(text) is None


def test_invalid_trailing_conclusion_does_not_fall_back_to_example() -> None:
    """尾部结论 passed 类型非法 → 整体无证据，不得回头采信更早的示例 JSON。"""
    text = '示例 {"passed": true} ... 最终 {"evaluation_result": {"passed": "不确定"}}'
    assert _detect(text) is None


SCORE_MATRIX = [
    ('{"passed": true, "score": 92}', 92.0),
    ('{"passed": true, "score": 92.5}', 92.5),
    ('{"passed": true, "score": "92"}', 92.0),
    ('{"passed": true, "score": " 92.5 "}', 92.5),
    ('{"evaluation_result": {"passed": false, "score": "0"}}', 0.0),
    ('{"passed": true, "score": null}', None),
    ('{"passed": true, "score": "N/A"}', None),
    ('{"passed": true, "score": "abc"}', None),
    ('{"passed": true, "score": ""}', None),
    ('{"passed": true, "score": true}', None),
    ('{"passed": true, "score": [92]}', None),
    ('{"passed": true, "score": "nan"}', None),
    ('{"passed": true, "score": "inf"}', None),
    ('{"evaluation_result": {"passed": true}}', None),
]


@pytest.mark.parametrize(("text", "expected"), SCORE_MATRIX)
def test_score_normalization(text: str, expected: float | None) -> None:
    detected = _detect(text)
    assert detected is not None
    assert detected["score"] == expected


NON_DICT_MATRIX = [
    "[1, 2, 3]",
    "null",
    "42",
    '"evaluation_result"',
]


@pytest.mark.parametrize("text", NON_DICT_MATRIX)
def test_non_dict_payload_is_not_evidence(text: str) -> None:
    assert _detect(text) is None


def test_non_string_and_empty_input() -> None:
    assert _detect("") is None
    assert _detect("没有花括号的散文") is None
    assert _detect(None) is None  # type: ignore[arg-type]


def test_stray_right_brace_does_not_shift_balancing() -> None:
    """游离右括号不进负深度：其后的合法候选仍能配平并被解析。"""
    detected = _detect('} 游离右括号在前 {"passed": true, "score": 88}')
    assert detected is not None
    assert detected["passed"] is True
    assert detected["score"] == 88.0


def test_feedback_and_suggestions_normalized() -> None:
    detected = _detect(
        '{"evaluation_result": {"passed": false, "score": 40, "feedback": 123, '
        '"suggestions": "not-a-list"}}'
    )
    assert detected is not None
    assert detected["feedback"] == "123"
    assert detected["suggestions"] == []


def test_last_conclusion_wins_over_earlier_example() -> None:
    text = '示例 {"passed": true, "score": 100} 最终 {"evaluation_result": {"passed": "false"}}'
    detected = _detect(text)
    assert detected is not None
    assert detected["passed"] is False, "尾部失败结论必须压过更早的通过示例"


def test_execute_marks_failed_conclusion_as_not_passed() -> None:
    """execute 收束面：passed 字符串 false 落 detected_result=False（不再被
    bool("false") 判成通过），子管道照常收束（结论已交付）。"""
    plugin = TaskReminder()
    ctx = PluginContext(
        state={
            "core_type": "llm_call",
            "iteration": 2,
            "task.id": "task-eval-1",
            "task.status": "running",
            "agent_level": "L3",
            "raw_tool_calls": [],
            "raw_result": (
                "评估完成。\n```json\n"
                '{"evaluation_result": {"passed": "false", "score": "N/A", '
                '"feedback": "未达标"}}\n```'
            ),
            "messages": [],
            "evaluation.metric_id": "semantic_check",
        },
        config={},
    )
    import asyncio

    result = asyncio.run(plugin.execute(ctx))
    detected = result.state_updates["evaluation.detected_result"]
    assert detected["passed"] is False
    assert detected["score"] is None
    assert detected["feedback"] == "未达标"
    assert result.state_updates.get("ended") is True
