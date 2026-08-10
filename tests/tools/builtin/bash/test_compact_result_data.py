"""_compact_result_data 一致性测试。

验证 result data 字段的保留策略：
- pid/output/status/exit_code 始终保留
- errors/warnings 非空时始终保留（高价值，不分长短）
- summary 仅长输出（>SHORT_OUTPUT_CHAR_THRESHOLD 字符）时保留
- terminated_by_signal 信号终止时附加
"""

from __future__ import annotations

from tools.builtin.bash.tool import BashTool


def test_short_output_no_summary_field():
    """短输出（<=SHORT_OUTPUT_CHAR_THRESHOLD 字符）不带 summary 字段。"""
    output = "line1\nline2\nline3"
    data = BashTool._compact_result_data(
        pid=123,
        output=output,
        summary_obj={"warnings": 0, "errors": 0, "error_lines": []},
        exit_code=0,
    )

    assert data["pid"] == 123
    assert data["output"] == "line1\nline2\nline3"
    assert data["status"] == "completed"
    assert data["exit_code"] == 0
    assert "summary" not in data, "短输出不应有 summary"


def test_long_output_includes_summary_field():
    """长输出（>SHORT_OUTPUT_CHAR_THRESHOLD 字符）应带 summary 字段。"""
    # 生成超过字符阈值的输出（阈值 2000，这里 ~3000 字符）
    output = "\n".join(f"line {i}: some padding text to reach the threshold" for i in range(100))
    data = BashTool._compact_result_data(
        pid=456,
        output=output,
        summary_obj={
            "warnings": 0,
            "errors": 0,
            "error_lines": [],
            "summary": ["[100行]", "类型: 通用命令", "警告: 0, 错误: 0"],
        },
        exit_code=0,
    )

    assert "summary" in data
    assert "[100行]" in data["summary"]


def test_empty_warnings_errors_omitted():
    """空 warnings/errors 不应出现在 data 中。"""
    data = BashTool._compact_result_data(
        pid=1,
        output="x",
        summary_obj={"warnings": 0, "errors": 0, "error_lines": []},
        exit_code=0,
    )
    assert "warnings" not in data
    assert "errors" not in data
    assert "error_lines" not in data


def test_nonzero_warnings_count_included():
    """非零 warnings 计数应保留（int 计数，让 LLM 知道有几条警告）。"""
    data = BashTool._compact_result_data(
        pid=2,
        output="x",
        summary_obj={"warnings": 1, "errors": 0, "error_lines": []},
        exit_code=0,
    )
    assert data["warnings"] == 1
    assert "errors" not in data


def test_nonzero_errors_count_and_lines_included():
    """非零 errors 计数 + error_lines 行列表都应保留。

    errors 是计数（int），error_lines 是具体错误行（list[str]），两者互补：
    LLM 既知道有几条错误，又能直接看到内容。
    """
    data = BashTool._compact_result_data(
        pid=3,
        output="x",
        summary_obj={"warnings": 0, "errors": 1, "error_lines": ["error: fail"]},
        exit_code=1,
    )
    assert data["errors"] == 1
    assert data["error_lines"] == ["error: fail"]
    assert data["exit_code"] == 1


def test_none_output_handled():
    """output 为 None 时不应报错（get_output 在某些场景可能返回 None）。"""
    data = BashTool._compact_result_data(
        pid=4,
        output=None,
        summary_obj={"warnings": 0, "errors": 0, "error_lines": []},
        exit_code=0,
    )
    assert data["output"] is None
