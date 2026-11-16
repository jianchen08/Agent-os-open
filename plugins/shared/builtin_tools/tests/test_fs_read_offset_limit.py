# @feature: FP-0.2.spill_guard 任务 2 | @ci: python-coverage
"""file_read 业界分页别名（offset/limit）——对齐 DSH read / Claude Code Read 先验。

实证（agentos_kernel.db 语料）：2026-10-12 单日 5 次 `limit` 自造键被静默丢弃
——模型按业界先验发分页参数，声明面只有 start_line/end_line。本刀收编：
offset=1-based 起始行（= start_line 别名），limit=返回行数（换算
end_line = 起始行 + limit - 1）。显式声明值优先，别名只补缺——存量调用零行为变化。
"""

from __future__ import annotations

import asyncio

import pytest

from agentos_builtin_tools.fs_tools import file_read

pytestmark = pytest.mark.unit


def _write_numbered(tmp_path, n=10):
    f = tmp_path / "lines.txt"
    f.write_text("\n".join(f"L{i}" for i in range(1, n + 1)), encoding="utf-8")
    return f


def test_offset_limit_reads_window(tmp_path):
    """offset+limit：从 offset 行起读 limit 行（业界 Read 先验形态）。"""
    f = _write_numbered(tmp_path)
    r = asyncio.run(file_read(str(f), offset=3, limit=2, workspace=str(tmp_path)))
    assert r.success, r.error
    assert r.output["content"] == "L3\nL4"


def test_limit_without_offset_starts_at_line_1(tmp_path):
    f = _write_numbered(tmp_path)
    r = asyncio.run(file_read(str(f), limit=2, workspace=str(tmp_path)))
    assert r.success, r.error
    assert r.output["content"] == "L1\nL2"


def test_explicit_start_end_unaffected(tmp_path):
    """回归守卫：存量 start_line/end_line 调用零行为变化。"""
    f = _write_numbered(tmp_path)
    r = asyncio.run(file_read(str(f), start_line=2, end_line=3, workspace=str(tmp_path)))
    assert r.success, r.error
    assert r.output["content"] == "L2\nL3"


def test_end_line_wins_over_limit(tmp_path):
    """显式 end_line 优先：limit 只补缺，不覆盖。"""
    f = _write_numbered(tmp_path)
    r = asyncio.run(
        file_read(str(f), start_line=2, end_line=3, limit=5, workspace=str(tmp_path))
    )
    assert r.success, r.error
    assert r.output["content"] == "L2\nL3"


def test_start_line_wins_over_offset(tmp_path):
    """显式 start_line 优先：offset 只补缺。"""
    f = _write_numbered(tmp_path)
    r = asyncio.run(
        file_read(str(f), start_line=4, offset=2, limit=2, workspace=str(tmp_path))
    )
    assert r.success, r.error
    assert r.output["content"] == "L4\nL5"
