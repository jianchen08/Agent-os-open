# @feature: FP-0.2.spill_guard 任务 2 | @vision: V1 可进化 | @ci: python-coverage
"""fs_tools file_read 截断清理（spill_guard 兜底就绪后）——TDD 规格。

设计原则（task_spill_guard.md 任务 2）：read_file 不再"大文件（>2MB）直接拒绝"
——大输出兜底由 pipeline 的 spill_guard 统一负责（原文存档 + 提取 + 定位符），
工具只负责"读文件 + 返回内容"。行范围/tail 是用户显式指定的查询窗口
（与 start_line/end_line 同类），不属于静默截断，保留。

验证：
1. test_large_file_no_longer_rejected —— 超过旧 2MB 上限的文件正常读回全文
2. test_max_file_size_constant_removed —— MAX_FILE_SIZE 拒绝常量已删除
3. test_full_content_returned —— 常规读取返回完整内容（含多字节字符）
4. test_tail_param_still_works —— tail 显式窗口查询保留（用户意图，非截断）
"""

from __future__ import annotations

import asyncio

import pytest

from agentos_builtin_tools.fs_tools import file_read

pytestmark = pytest.mark.unit


def test_large_file_no_longer_rejected(tmp_path):
    """超过旧 2MB 上限的文本文件：正常读取，不再拒绝（spill_guard 兜底）。"""
    big = tmp_path / "big.log"
    # 高冗余文本写到 ~2.5MB（超旧 MAX_FILE_SIZE = 2MB）
    line = "x" * 128 + "\n"
    big.write_text(line * 20_000, encoding="utf-8")
    assert big.stat().st_size > 2 * 1024 * 1024

    result = asyncio.run(file_read(str(big), workspace=str(tmp_path)))
    assert result.success, f"大文件不得拒绝: {result.error}"
    # 写入以 \n 收尾（split+join 既有语义原样保留），长度对上即证明全文读回
    assert result.output["content"].endswith("\n"), "读到文件末尾"
    assert result.output["size"] == big.stat().st_size


def test_max_file_size_constant_removed():
    """MAX_FILE_SIZE 拒绝常量已删除（职责移交 spill_guard）。"""
    import agentos_builtin_tools.fs_tools as fs_mod

    assert not hasattr(fs_mod, "MAX_FILE_SIZE")
    source = __import__("inspect").getsource(fs_mod)
    assert "File too large" not in source, "大文件拒绝逻辑应删除"


def test_full_content_returned(tmp_path):
    """常规读取返回完整内容（UTF-8 多字节字符不损坏）。"""
    f = tmp_path / "note.txt"
    f.write_text("第一行\nsecond line 🙂\n", encoding="utf-8")
    result = asyncio.run(file_read(str(f), workspace=str(tmp_path)))
    assert result.success
    # split+join 的既有语义：末尾 \n 产生空串元素 → content 以 \n 收尾（无损往返）
    assert result.output["content"] == "第一行\nsecond line 🙂\n"


def test_tail_param_still_works(tmp_path):
    """tail= 显式窗口查询保留（用户指定的读取范围，非静默截断）。"""
    f = tmp_path / "lines.txt"
    f.write_text("\n".join(f"row-{i}" for i in range(100)), encoding="utf-8")
    result = asyncio.run(file_read(str(f), tail=5, workspace=str(tmp_path)))
    assert result.success
    assert result.output["content"].splitlines()[0] == "row-95"
    assert result.output["lines"] == 5
