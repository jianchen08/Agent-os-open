"""测试 PipelineLogParser.parse_pipeline_logs 日志解析功能。"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.maintenance.log_parser import PipelineLogParser
from src.memory.maintenance.review_engine import Pipeline, ReviewEngine

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"


class TestParsePipelineLogs:
    """PipelineLogParser.parse_pipeline_logs 测试。"""

    def test_parse_real_logs_five_files_ten_errors(self):
        """正常解析 5 个日志文件，共 10 个错误（3+1+2+0+4）。"""
        pipelines = PipelineLogParser.parse_pipeline_logs(LOG_DIR)

        pipeline_map = {p.pipeline_id: p for p in pipelines}
        assert len(pipeline_map) == 5

        assert len(pipeline_map["013af21d0b04"].errors) == 3
        assert len(pipeline_map["07485ba22889"].errors) == 1
        assert len(pipeline_map["a3c5e7f9d012"].errors) == 2
        assert len(pipeline_map["b4d6f8e0a123"].errors) == 0
        assert len(pipeline_map["c5e7a9f1b234"].errors) == 4

        total_errors = sum(len(p.errors) for p in pipelines)
        assert total_errors == 10

    def test_empty_directory_returns_empty_list(self, tmp_path: Path):
        """空目录返回空列表。"""
        result = PipelineLogParser.parse_pipeline_logs(tmp_path)
        assert result == []

    def test_nonexistent_directory_returns_empty_list(self, tmp_path: Path):
        """不存在的目录返回空列表。"""
        result = PipelineLogParser.parse_pipeline_logs(tmp_path / "nonexistent")
        assert result == []

    def test_no_error_lines_produces_empty_errors_pipeline(self, tmp_path: Path):
        """无 ERROR 行的文件产生空 errors 的 Pipeline。"""
        log_file = tmp_path / "pipeline_noserr.log"
        log_file.write_text(
            "2026-05-28 08:00:00.123 [pipeline_noserr] INFO  All good\n",
            encoding="utf-8",
        )
        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)

        assert len(pipelines) == 1
        assert pipelines[0].pipeline_id == "noserr"
        assert pipelines[0].errors == []

    def test_malformed_lines_skipped_without_crash(self, tmp_path: Path):
        """格式异常行被跳过不崩溃。"""
        log_file = tmp_path / "pipeline_malformed.log"
        log_file.write_text(
            "this is not a valid log line\n"
            "another garbage line\n"
            "2026-05-28 08:00:00.123 [pipeline_malformed] INFO  OK\n",
            encoding="utf-8",
        )
        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)

        assert len(pipelines) == 1
        assert pipelines[0].pipeline_id == "malformed"
        assert pipelines[0].errors == []

    def test_multiple_files_merge_same_pipeline_id(self, tmp_path: Path):
        """多个文件合并到同一 pipeline_id。"""
        content_a = (
            "2026-05-28 08:00:00.123 [pipeline_shared] INFO  start\n"
            "2026-05-28 08:00:01.000 [pipeline_shared] ERROR X error_type=timeout error=\"t1\"\n"
        )
        content_b = (
            "2026-05-28 09:00:00.123 [pipeline_shared] INFO  start\n"
            "2026-05-28 09:00:01.000 [pipeline_shared] ERROR X error_type=connection error=\"c1\"\n"
        )
        (tmp_path / "pipeline_file_a.log").write_text(content_a, encoding="utf-8")
        (tmp_path / "pipeline_file_b.log").write_text(content_b, encoding="utf-8")

        pipelines = PipelineLogParser.parse_pipeline_logs(tmp_path)

        assert len(pipelines) == 1
        assert pipelines[0].pipeline_id == "shared"
        assert len(pipelines[0].errors) == 2

    def test_backward_compat_via_review_engine(self):
        """ReviewEngine.parse_pipeline_logs 委托后仍可用。"""
        pipelines = ReviewEngine.parse_pipeline_logs(LOG_DIR)
        assert len(pipelines) == 5
        total_errors = sum(len(p.errors) for p in pipelines)
        assert total_errors == 10
