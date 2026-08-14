# @feature: FP-0.2.spill_guard 任务 2 | @vision: V1 可进化 | @ci: python-unit
"""bash 工具截断清理（spill_guard 兜底就绪后）——TDD 规格。

设计原则（task_spill_guard.md 任务 2）：工具只负责"执行 + 语义提取"，
截断兜底是 spill_guard 输出插件的统一职责。bash 改为返回完整 output，
由 pipeline 的 pipeline_spill_guard 同 step 兜底（原文存档 + 提取 + 定位符）。

验证：
1. test_long_output_returns_full_output —— 长输出不再被 tail_output 截尾
2. test_compact_keeps_semantic_extraction —— 长输出的 summary/error_lines/
   latest_message/progress 语义提取保留
3. test_failure_message_extraction_only —— 失败消息 = 信号 + 提取行，无原始尾部
4. test_truncation_helpers_removed —— tail_output / clip_read_log_output /
   TAIL_CHARS_ON_FAILURE / READ_LOG_MAX_CHARS 均已删除
5. test_read_log_returns_full_output —— read_log 路径不再 clip
"""

from __future__ import annotations

import pytest

from tool import BashTool

pytestmark = pytest.mark.unit


def test_long_output_returns_full_output():
    """长输出：data["output"] 必须是完整原文（spill_guard 负责兜底）。"""
    output = "\n".join(f"line {i:05d}: build output" for i in range(500))
    data = BashTool._compact_result_data(
        pid=1,
        output=output,
        summary_obj={"warnings": 0, "errors": 0, "summary": ["[500行]"]},
        exit_code=0,
    )
    assert data["output"] == output, "长输出不得在工具内截断（spill_guard 兜底）"


def test_compact_keeps_semantic_extraction():
    """长输出的语义提取字段保留（log_compressor 专用层职责不变）。"""
    output = "x" * (BashTool.SHORT_OUTPUT_CHAR_THRESHOLD + 100)
    data = BashTool._compact_result_data(
        pid=2,
        output=output,
        summary_obj={
            "warnings": 1,
            "errors": 2,
            "summary": ["[长输出]", "警告: 1, 错误: 2"],
            "error_lines": ["error: boom"],
            "latest_message": "still running",
            "progress": "50%",
        },
        exit_code=0,
    )
    assert data["summary"] == ["[长输出]", "警告: 1, 错误: 2"]
    assert data["error_lines"] == ["error: boom"]
    assert data["latest_message"] == "still running"
    assert data["progress"] == "50%"


def test_failure_message_extraction_only():
    """失败消息：信号 + LogCompressor 提取行；不再拼原始输出尾部。"""
    output = "\n".join(f"noise {i}" for i in range(2000))
    error_msg, meta = BashTool._build_failure_message(
        1,
        output,
        {"error_lines": ["error: real cause"], "latest_message": "last line"},
    )
    assert "退出码: 1" in error_msg
    assert "error: real cause" in error_msg, "提取的错误行保留"
    assert "last line" in error_msg, "latest_message 保留"
    assert "末尾输出" not in error_msg, "原始尾部截取已删除"
    assert len(error_msg) < 2000, "失败消息有界（提取组装，非原文拼接）"


def test_failure_message_signal_termination():
    """信号终止描述保留（137/SIGKILL 类归因）。"""
    error_msg, meta = BashTool._build_failure_message(
        137, "Killed", {"error_lines": ["Killed"], "latest_message": "Killed"}
    )
    assert "信号" in error_msg and "137" in error_msg
    assert meta["terminated_by_signal"]


def test_truncation_helpers_removed():
    """截断辅助函数/常量全部删除（职责移交 spill_guard）。"""
    assert not hasattr(BashTool, "tail_output")
    assert not hasattr(BashTool, "TAIL_CHARS_ON_FAILURE")
    import tool as tool_mod

    assert not hasattr(tool_mod, "tail_output")
    assert not hasattr(tool_mod, "clip_read_log_output")
    assert not hasattr(tool_mod, "TAIL_CHARS_ON_FAILURE")
    assert not hasattr(tool_mod, "READ_LOG_MAX_CHARS")


def test_read_log_no_clip():
    """read_log 路径不再 clip：辅助函数已删（调用点回归由 clip_read_log_output
    缺失 + 单元层覆盖保证；真实进程路径依赖 WSL 环境，与本改动解耦）。"""
    import inspect
    import tool as tool_mod

    source = inspect.getsource(tool_mod)
    assert "clip_read_log_output" not in source
    assert "READ_LOG_MAX_CHARS" not in source
