"""执行记录 API 端点测试。

覆盖 src/channels/api/routes_missing.py 中 /api/v1/execution/records* 系列端点，
验证它们从 ExecutionRecordStorage 读取真实数据并正确映射为前端接口字段。

背景：这些端点曾是返回空数据的 stub（前端调试中心「执行记录」/「调试会话」页面
因此永远显示「暂无数据」），现已接到真实存储层。本测试锁定字段映射契约。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 确保 src 在 sys.path 中（与 test_api_routes.py 一致）
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))

from channels.api.routes_missing import (  # noqa: E402
    _record_to_response,
    _summary_to_session_info,
    get_execution_record,
    get_execution_record_sessions,
    list_execution_records,
)
from infrastructure.execution_record_storage import (  # noqa: E402
    ExecutionRecordData,
    ExecutionRecordStorage,
    PipelineRunSummary,
)


def _make_record(
    record_id: str,
    pipeline_run_id: str,
    *,
    sequence: int = 1,
    iteration: int = 0,
    rtype: str = "ai",
    error: str | None = None,
) -> ExecutionRecordData:
    return ExecutionRecordData(
        record_id=record_id,
        pipeline_run_id=pipeline_run_id,
        type=rtype,
        sequence=sequence,
        iteration=iteration,
        role="assistant",
        content="test content",
        error=error,
    )


def _make_summary(run_id: str, *, total_records: int = 5, final_output: str = "done") -> PipelineRunSummary:
    return PipelineRunSummary(
        run_id=run_id,
        thread_id="thread-1",
        total_records=total_records,
        status="success",
        final_output=final_output,
    )


class TestRecordMapping:
    """_record_to_response 字段映射。"""

    def test_maps_core_fields(self):
        record = _make_record("rec-1", "pipe-1", sequence=7, iteration=3, rtype="tool")
        resp = _record_to_response(record)
        assert resp["id"] == "rec-1"
        assert resp["session_id"] == "pipe-1"
        assert resp["record_type"] == "tool"
        assert resp["sequence"] == 7
        assert resp["depth"] == 3
        assert resp["status"] == "completed"
        assert resp["created_at"]  # 非空
        assert isinstance(resp["message_data"], dict)
        assert resp["message_data"]["content"] == "test content"

    def test_status_failed_when_error_present(self):
        record = _make_record("rec-2", "pipe-1", error="boom")
        resp = _record_to_response(record)
        assert resp["status"] == "failed"

    def test_parent_record_id_always_none(self):
        # 存储层无 parent 概念，映射为 None
        resp = _record_to_response(_make_record("rec-3", "pipe-1"))
        assert resp["parent_record_id"] is None


class TestSummaryMapping:
    """_summary_to_session_info 字段映射。"""

    def test_maps_core_fields(self):
        summary = _make_summary("run-1", total_records=42, final_output="hello world")
        info = _summary_to_session_info(summary)
        assert info["id"] == "run-1"
        assert info["record_count"] == 42
        assert info["title"] == "hello world"
        assert info["created_at"] == summary.created_at
        assert info["updated_at"] == summary.created_at

    def test_title_truncated_for_long_output(self):
        long_output = "x" * 200
        summary = _make_summary("run-2", final_output=long_output)
        info = _summary_to_session_info(summary)
        # summarize_text(max_len=80) 截断后不应超过 80 + 省略号后缀
        assert len(info["title"]) <= 100
        assert info["title"].startswith("x" * 80)


class TestSessionsEndpoint:
    """/records/sessions 端点。"""

    def test_returns_empty_when_storage_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: None
        )
        result = asyncio.run(get_execution_record_sessions(_user={}))
        assert result == {"sessions": [], "total": 0}

    def test_returns_sessions_sorted_by_created_desc(self, tmp_path: Path, monkeypatch):
        storage = ExecutionRecordStorage(data_dir=tmp_path)
        old = _make_summary("run-old")
        old.created_at = "2026-01-01T00:00:00"
        new = _make_summary("run-new")
        new.created_at = "2026-06-01T00:00:00"
        storage.save_summary(old)
        storage.save_summary(new)
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: storage
        )

        result = asyncio.run(get_execution_record_sessions(_user={}))

        assert result["total"] == 2
        ids = [s["id"] for s in result["sessions"]]
        assert ids == ["run-new", "run-old"]  # 最新在前


class TestListRecordsEndpoint:
    """/records 端点。"""

    def test_returns_empty_when_storage_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: None
        )
        result = asyncio.run(
            list_execution_records(session_id=None, parent_record_id=None,
                                   limit=50, offset=0, _user={})
        )
        assert result["records"] == []
        assert result["total"] == 0

    def test_filters_by_session(self, tmp_path: Path, monkeypatch):
        storage = ExecutionRecordStorage(data_dir=tmp_path)
        storage.save(_make_record("a1", "pipe-A", sequence=1))
        storage.save(_make_record("a2", "pipe-A", sequence=2))
        storage.save(_make_record("b1", "pipe-B", sequence=1))
        storage.save_summary(_make_summary("pipe-A", total_records=2))
        storage.save_summary(_make_summary("pipe-B", total_records=1))
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: storage
        )

        result = asyncio.run(
            list_execution_records(session_id="pipe-A", parent_record_id=None,
                                   limit=50, offset=0, _user={})
        )

        assert result["total"] == 2
        ids = [r["id"] for r in result["records"]]
        assert set(ids) == {"a1", "a2"}
        assert all(r["session_id"] == "pipe-A" for r in result["records"])

    def test_all_sessions_aggregates_records(self, tmp_path: Path, monkeypatch):
        storage = ExecutionRecordStorage(data_dir=tmp_path)
        storage.save(_make_record("a1", "pipe-A", sequence=1))
        storage.save(_make_record("b1", "pipe-B", sequence=1))
        storage.save_summary(_make_summary("pipe-A", total_records=1))
        storage.save_summary(_make_summary("pipe-B", total_records=1))
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: storage
        )

        result = asyncio.run(
            list_execution_records(session_id=None, parent_record_id=None,
                                   limit=50, offset=0, _user={})
        )

        assert result["total"] == 2
        assert {r["id"] for r in result["records"]} == {"a1", "b1"}


class TestSingleRecordEndpoint:
    """/records/{record_id} 端点。"""

    def test_returns_empty_when_storage_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: None
        )
        result = asyncio.run(get_execution_record(record_id="x", _user={}))
        assert result["id"] == "x"
        assert result["message_data"] == {}

    def test_returns_record_when_found(self, tmp_path: Path, monkeypatch):
        storage = ExecutionRecordStorage(data_dir=tmp_path)
        storage.save(_make_record("rec-99", "pipe-1", sequence=1, rtype="tool"))
        monkeypatch.setattr(
            "channels.api.routes_missing._get_exec_storage", lambda: storage
        )

        result = asyncio.run(get_execution_record(record_id="rec-99", _user={}))

        assert result["id"] == "rec-99"
        assert result["session_id"] == "pipe-1"
        assert result["record_type"] == "tool"
        assert result["status"] == "completed"
