"""read_execution_detail 骨架层（skeleton）单测。

覆盖骨架层对三类记录的处理契约：
1. user 记录：保留 content 原文（不截断），复盘需看到用户原始指令/纠正。
2. human_interaction 工具记录：完整保留输入（tool_input）和输出（content），
   content 为 dict 时安全序列化、不崩。
3. 普通记录（ai/普通 tool）：content 前 50 字预览。
4. 记录缺失：返回 RECORDS_NOT_FOUND 失败。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from tools.builtin.read_execution_detail.tool import ReadExecutionDetailTool


@dataclass
class _FakeRecord:
    """模拟 ExecutionRecordData，只填 skeleton 路径用到的字段。"""
    iteration: int = 0
    type: str = "ai"
    name: str | None = None
    content: Any = ""
    tool_input: dict[str, Any] | None = None
    error: str | None = None


class _FakeStorage:
    """duck typing：_get_skeleton 只调用 list_by_pipeline(pid)[0]。"""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records

    def list_by_pipeline(self, pid: str) -> tuple[list[_FakeRecord], None]:
        return self._records, None


@pytest.fixture
def tool() -> ReadExecutionDetailTool:
    return ReadExecutionDetailTool()


def test_user_record_keeps_full_content(tool: ReadExecutionDetailTool) -> None:
    """user 记录的 content 必须原文保留，不截断。"""
    long_msg = "请帮我重构整个认证模块并接入单点登录" * 5  # 远超 50 字
    storage = _FakeStorage([_FakeRecord(iteration=0, type="user", content=long_msg)])

    res = tool._get_skeleton(storage, "pid")

    assert res.success
    line = res.output["lines"][0]
    assert line.startswith("[iter 0] user: ")
    assert long_msg in line  # 原文完整出现，未被截断


def test_human_interaction_keeps_input_and_output(tool: ReadExecutionDetailTool) -> None:
    """human_interaction 记录完整保留 tool_input（提问）和 content（用户回答）。

    content 为 dict 形态（生产中真实结构 {'output': {...}}）时必须安全序列化、不抛异常。
    """
    storage = _FakeStorage([_FakeRecord(
        iteration=3,
        type="tool",
        name="human_interaction",
        tool_input={"name": "human_interaction", "args": {"mode": "choice", "title": "选择部署方案"}},
        content={"output": {"response_type": "approved", "selected_option": "blue_green"}},
    )])

    res = tool._get_skeleton(storage, "pid")

    assert res.success
    line = res.output["lines"][0]
    assert "[iter 3] tool human_interaction" in line
    assert "输入:" in line
    assert "选择部署方案" in line  # tool_input（提问）内容保留
    assert "输出:" in line
    assert "approved" in line      # content（用户决策）内容保留
    assert "blue_green" in line


def test_normal_record_uses_50char_preview(tool: ReadExecutionDetailTool) -> None:
    """ai/普通 tool 记录维持 content 前 50 字预览（不全文输出）。"""
    long_text = "x" * 200
    storage = _FakeStorage([_FakeRecord(iteration=1, type="ai", content=long_text)])

    res = tool._get_skeleton(storage, "pid")

    assert res.success
    line = res.output["lines"][0]
    assert line.startswith("[iter 1] ai ")
    # 预览最多 50 字（200 字原文不应全量出现）
    assert "x" * 51 not in line
    assert "x" * 50 in line


def test_empty_records_returns_not_found(tool: ReadExecutionDetailTool) -> None:
    """无执行记录时返回 RECORDS_NOT_FOUND 失败。"""
    storage = _FakeStorage([])

    res = tool._get_skeleton(storage, "pid")

    assert not res.success
    assert res.error_code == "RECORDS_NOT_FOUND"
