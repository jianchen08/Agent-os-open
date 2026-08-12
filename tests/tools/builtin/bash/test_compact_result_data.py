"""_compact_result_data 一致性测试。

验证 result data 字段的保留策略：
- pid/output/status/exit_code 始终保留
- summary 仅长输出（>10 行）时保留
- warnings/errors 仅非空时保留
"""

from __future__ import annotations

import pytest

from tool import BashTool

pytestmark = pytest.mark.unit


def test_short_output_no_summary_field():
    """短输出（<10 行）不带 summary 字段。"""
    output = "line1\nline2\nline3"
    data = BashTool._compact_result_data(
        pid=123,
        output=output,
        summary_obj={"warnings": [], "errors": []},
        exit_code=0,
    )

    assert data["pid"] == 123
    assert data["output"] == "line1\nline2\nline3"
    assert data["status"] == "completed"
    assert data["exit_code"] == 0
    assert "summary" not in data, "短输出不应有 summary"


def test_long_output_includes_summary_field():
    """长输出（>SHORT_OUTPUT_CHAR_THRESHOLD 字符）应带 summary 字段。"""
    # 0.2 阈值改为按字符数（SHORT_OUTPUT_CHAR_THRESHOLD=2000），不再按行数。
    # 构造一段明确超过阈值的输出以触达"长输出"分支。
    output = "x" * (BashTool.SHORT_OUTPUT_CHAR_THRESHOLD + 100)
    data = BashTool._compact_result_data(
        pid=456,
        output=output,
        summary_obj={
            "warnings": [],
            "errors": [],
            "summary": ["[长输出]", "类型: 通用命令", "警告: 0, 错误: 0"],
        },
        exit_code=0,
    )

    assert "summary" in data
    assert "[长输出]" in data["summary"]


def test_empty_warnings_errors_omitted():
    """空 warnings/errors 不应出现在 data 中。"""
    data = BashTool._compact_result_data(
        pid=1,
        output="x",
        summary_obj={"warnings": [], "errors": []},
        exit_code=0,
    )
    assert "warnings" not in data
    assert "errors" not in data


def test_nonempty_warnings_included():
    """非空 warnings 应保留。"""
    data = BashTool._compact_result_data(
        pid=2,
        output="x",
        summary_obj={"warnings": ["caution: rm "], "errors": []},
        exit_code=0,
    )
    assert data["warnings"] == ["caution: rm "]
    assert "errors" not in data


def test_nonempty_errors_included():
    """非空 errors 应保留。"""
    data = BashTool._compact_result_data(
        pid=3,
        output="x",
        summary_obj={"warnings": [], "errors": ["error: fail"]},
        exit_code=1,
    )
    assert data["errors"] == ["error: fail"]
    assert data["exit_code"] == 1


def test_none_output_handled():
    """output 为 None 时不应报错（get_output 在某些场景可能返回 None）。"""
    data = BashTool._compact_result_data(
        pid=4,
        output=None,
        summary_obj={"warnings": [], "errors": []},
        exit_code=0,
    )
    assert data["output"] is None
