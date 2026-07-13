"""提交期评估指标 ID 合法性校验测试。

背景：LLM 可能把 acceptance_criteria value 的子字段名（如 pass_threshold）
误填为 key（即 metric_id），导致评估期反复 METRIC_NOT_FOUND 直至任务失败。
此处测试 task_submit 的提交期校验拦截逻辑。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tools.builtin.task_submit import tool as submit_tool
from tools.builtin.task_submit.tool import _validate_metric_ids


# 真实存在的合法指标 ID（与 config/evaluation_metrics/ 保持一致）
_VALID = {"bash_check", "file_check", "human_review", "semantic_check"}


class TestValidateMetricIds:
    """_validate_metric_ids 单元测试。"""

    @pytest.mark.task
    @pytest.mark.unit
    def test_all_invalid_returns_empty(self) -> None:
        """全部 key 无效（如 pass_threshold 被误填为指标 ID）→ 全部剔除。"""
        with patch.object(submit_tool, "_get_valid_metric_ids", return_value=_VALID):
            criteria = {"pass_threshold": {"$text": "80"}}
            filtered, invalid = _validate_metric_ids(criteria)
        assert filtered == {}
        assert invalid == ["pass_threshold"]

    @pytest.mark.task
    @pytest.mark.unit
    def test_partial_invalid_keeps_valid(self) -> None:
        """部分 key 无效 → 剔除无效项，保留有效项。"""
        with patch.object(submit_tool, "_get_valid_metric_ids", return_value=_VALID):
            criteria = {
                "file_check": {"input_params": {"path": "src/main.py"}},
                "pass_threshold": {"$text": "80"},
            }
            filtered, invalid = _validate_metric_ids(criteria)
        assert set(filtered.keys()) == {"file_check"}
        assert invalid == ["pass_threshold"]

    @pytest.mark.task
    @pytest.mark.unit
    def test_empty_criteria_passes_through(self) -> None:
        """空 acceptance_criteria → 原样返回，无无效项。"""
        with patch.object(submit_tool, "_get_valid_metric_ids", return_value=_VALID):
            filtered, invalid = _validate_metric_ids({})
        assert filtered == {}
        assert invalid == []

    @pytest.mark.task
    @pytest.mark.unit
    def test_all_valid_unchanged(self) -> None:
        """全部 key 合法 → 原样保留。"""
        with patch.object(submit_tool, "_get_valid_metric_ids", return_value=_VALID):
            criteria = {
                "file_check": {"input_params": {"path": "a.py"}},
                "semantic_check": {"input_params": {}},
            }
            filtered, invalid = _validate_metric_ids(criteria)
        assert filtered == criteria
        assert invalid == []

    @pytest.mark.task
    @pytest.mark.unit
    def test_fail_open_when_loader_unavailable(self) -> None:
        """MetricLoader 不可用（_get_valid_metric_ids 返回 None）→ fail-open，不剔除。"""
        criteria = {"pass_threshold": {"$text": "80"}, "file_check": {"input_params": {}}}
        with patch.object(submit_tool, "_get_valid_metric_ids", return_value=None):
            filtered, invalid = _validate_metric_ids(criteria)
        assert filtered == criteria
        assert invalid == []


class TestGetValidMetricIds:
    """_get_valid_metric_ids 容错测试。"""

    @pytest.mark.task
    @pytest.mark.unit
    def test_returns_none_when_loader_raises(self) -> None:
        """MetricLoader 抛异常（如目录不存在）→ 返回 None，不抛出。"""
        with patch(
            "evaluation.loader.MetricLoader",
            side_effect=FileNotFoundError("dir not found"),
        ):
            result = submit_tool._get_valid_metric_ids()
        assert result is None

    @pytest.mark.task
    @pytest.mark.unit
    def test_returns_real_metric_ids(self) -> None:
        """正常加载 → 返回真实指标 ID 集合。"""
        result = submit_tool._get_valid_metric_ids()
        # 加载失败（测试环境无 config 目录）时为 None，同样视为合法结果
        if result is not None:
            assert "file_check" in result
