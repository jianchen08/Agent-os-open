"""Pipeline 日志解析模块。

从 pipeline_*.log 日志文件中解析 Pipeline 执行记录和错误信息。
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from src.memory.maintenance.review_engine import ErrorRecord, Pipeline

__all__ = ["PipelineLogParser"]


class PipelineLogParser:
    """Pipeline 日志解析器，负责从日志文件中提取 Pipeline 和错误信息。"""

    @staticmethod
    def _compile_patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
        """编译日志解析所需的正则表达式。

        Returns:
            (error_line_re, pipeline_id_re) 元组。
            - error_line_re: 匹配 ERROR 行，提取时间戳、pipeline_id、error_type、error 消息。
            - pipeline_id_re: 从任意行提取 pipeline_id。
        """
        error_line_re = re.compile(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)"
            r"\s+\[pipeline_([^\]]+)\]"
            r"\s+ERROR\b"
            r".*?error_type=(\S+)"
            r"\s+error=\"([^\"]+)\""
        )
        pipeline_id_re = re.compile(r"\[pipeline_([^\]]+)\]")
        return error_line_re, pipeline_id_re

    @staticmethod
    def _parse_single_log(
        log_file: Path,
        error_line_re: re.Pattern[str],
        pipeline_id_re: re.Pattern[str],
        pipeline_errors: dict[str, list[ErrorRecord]],
    ) -> None:
        """解析单个日志文件，将错误追加到 pipeline_errors。

        Args:
            log_file: 日志文件路径。
            error_line_re: ERROR 行匹配正则。
            pipeline_id_re: pipeline_id 提取正则。
            pipeline_errors: 就地修改的 {pipeline_id: [ErrorRecord]} 字典。
        """
        try:
            text = log_file.read_text(encoding="utf-8")
        except OSError:
            return

        file_pipeline_id: str | None = None
        for line in text.splitlines():
            id_match = pipeline_id_re.search(line)
            if id_match:
                file_pipeline_id = id_match.group(1)

            m = error_line_re.match(line)
            if m:
                error_type = m.group(3)
                error_msg = m.group(4)
                timestamp = m.group(1)

                if file_pipeline_id is None:
                    continue

                if file_pipeline_id not in pipeline_errors:
                    pipeline_errors[file_pipeline_id] = []

                pipeline_errors[file_pipeline_id].append(ErrorRecord(
                    error_id=f"err-{uuid.uuid4().hex[:8]}",
                    error_type=error_type,
                    message=error_msg,
                    timestamp=timestamp,
                ))

        if file_pipeline_id and file_pipeline_id not in pipeline_errors:
            pipeline_errors[file_pipeline_id] = []

    @classmethod
    def parse_pipeline_logs(cls, log_dir: str | Path) -> list[Pipeline]:
        """扫描 log_dir 下的 pipeline_*.log 文件，解析为 Pipeline 列表。

        日志文件命名：pipeline_<id>.log
        日志行格式：
            <timestamp> [pipeline_<id>] <LEVEL> <message> ...

        ERROR 行需包含 error_type=<type> 和 error="<msg>" 字段：
            2026-05-28 08:00:33.012 [pipeline_abc] ERROR ... error_type=timeout error="..."

        非日志文件和不匹配的行会被静默跳过。

        Args:
            log_dir: 日志目录路径。

        Returns:
            解析出的 Pipeline 列表，可直接传给 ReviewEngine.register_pipelines()。
        """
        log_path = Path(log_dir)
        if not log_path.is_dir():
            return []

        error_line_re, pipeline_id_re = cls._compile_patterns()
        pipeline_errors: dict[str, list[ErrorRecord]] = {}

        for log_file in sorted(log_path.glob("pipeline_*.log")):
            cls._parse_single_log(log_file, error_line_re, pipeline_id_re, pipeline_errors)

        return [
            Pipeline(pipeline_id=pid, errors=errors)
            for pid, errors in pipeline_errors.items()
        ]
