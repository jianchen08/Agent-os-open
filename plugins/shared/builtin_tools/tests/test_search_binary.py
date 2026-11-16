# @feature: FP-0.2.spill_guard 二进制搜索闸 | @vision: V1 可进化 | @ci: python-coverage
"""enhanced_search 二进制文件闸测试（2026-09-14 用户裁定）。

二进制文件（NUL 探测）不展开内容：此前 errors="replace" 把二进制吞成
替换字符垃圾随命中行回传，单个文件即可撑出 MB 级工具结果（实测 13.5MB
blob 事故）。行为：命中只报路径与体量（细节由 agent 决定是否 file_read），
未命中跳过；文本文件行为不变。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos_builtin_tools.search_tool import enhanced_search

pytestmark = pytest.mark.unit


class TestBinaryFileGate:
    async def test_binary_hit_reports_path_only(self, tmp_path: Path) -> None:
        """二进制命中：只报路径与体量，不含任何二进制内容字节。"""
        blob = tmp_path / "blob.bin"
        blob.write_bytes(b"\x00\x01garbage\xff\xfe needle \x00\x02tail\x00")

        result = await enhanced_search(query="needle", path=str(blob), workspace=str(tmp_path))
        assert result.success
        results = result.output["results"]
        assert len(results) == 1, "二进制命中聚合为单条路径条目"
        entry = results[0]
        assert entry["line_number"] == 0
        assert entry["file_path"] == str(blob)
        assert "二进制文件命中" in entry["content"]
        assert str(blob.stat().st_size) in entry["content"], "条目含体量信息"
        assert "\x00" not in entry["content"] and "garbage" not in entry["content"], (
            "二进制内容字节不得进结果"
        )

    async def test_binary_without_match_skipped(self, tmp_path: Path) -> None:
        """二进制未命中查询：完全不出现在结果里。"""
        blob = tmp_path / "clean.bin"
        blob.write_bytes(b"\x00\x01\xff\xfe unrelated \x00")

        result = await enhanced_search(query="needle", path=str(blob), workspace=str(tmp_path))
        assert result.success
        assert result.output["results"] == []

    async def test_text_file_entries_unchanged(self, tmp_path: Path) -> None:
        """文本文件行为不变：命中行 + 上下文照常返回。"""
        f = tmp_path / "code.py"
        f.write_text("a = 1\nneedle = 2\nb = 3\n", encoding="utf-8")

        result = await enhanced_search(query="needle", path=str(f), workspace=str(tmp_path))
        assert result.success
        results = result.output["results"]
        assert len(results) == 1
        assert results[0]["line_number"] == 2
        assert results[0]["content"] == "needle = 2"

    async def test_scan_budget_bounded(self, tmp_path: Path) -> None:
        """超过扫描预算（4MB）之外的命中不报——扫描有界，不全文读入。"""
        from agentos_builtin_tools.search_tool import _BINARY_SCAN_BYTES

        blob = tmp_path / "huge.bin"
        payload = b"\x00" * _BINARY_SCAN_BYTES + b"needle-tail"
        blob.write_bytes(payload + b"\x00" * 16)
        assert blob.stat().st_size > _BINARY_SCAN_BYTES

        result = await enhanced_search(query="needle-tail", path=str(blob), workspace=str(tmp_path))
        assert result.success
        assert result.output["results"] == [], "预算外命中不展开（扫描有界）"
