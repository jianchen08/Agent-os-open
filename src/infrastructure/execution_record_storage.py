"""执行记录存储 — YAML 多文件持久化。

记录管道每轮迭代的执行详情，包括 LLM 输出摘要、工具结果摘要、
token 用量、耗时和错误信息。按 pipeline_run_id 拆分为独立 YAML 文件。

存储模式：
- 按 pipeline_run_id 拆分为独立 YAML 文件
- 目录结构：data/pipelines/{pipeline_run_id}.yaml
- 每个 YAML 文件包含 summary 和 records 两部分
- 内存缓存 + 文件持久化
- 同步 API（单管道顺序写入）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_SUMMARY_MAX_LEN = 500


@dataclass
class ExecutionRecordData:
    """L0 原始执行记录（压缩体系的 L0 层）。

    保存每个原子动作的原始消息内容，
    作为 L0→L1→L2 压缩链路的输入。

    一次 LLM 输出 = 一条 type=ai 记录（保存完整输出）
    一次工具调用 = 一条 type=tool 记录（保存输入+输出）
    """

    record_id: str = ""
    pipeline_run_id: str = ""

    type: str = "ai"
    name: str | None = None

    sequence: int = 0
    iteration: int = 0

    role: str = ""
    content: str = ""
    tool_call_id: str | None = None
    tool_input: dict[str, Any] | None = None
    thinking_content: str | None = None

    error: str | None = None

    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.record_id:
            self.record_id = uuid.uuid4().hex[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class PipelineRunSummary:
    """管道运行摘要（每次 pipeline_run 产生一条）。

    由 PipelineEngine 在运行结束时写入，
    用于成本统计、运行日志、/cost 命令等场景。
    """

    run_id: str = ""

    total_iterations: int = 0
    total_tokens: dict[str, int] = field(default_factory=dict)
    total_seconds: float = 0.0
    total_records: int = 0

    status: str = ""
    final_output: str = ""
    error: str | None = None

    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class ExecutionRecordStorage:
    """执行记录存储 — 内存缓存 + YAML 多文件持久化。

    按 pipeline_run_id 拆分为独立 YAML 文件，每个文件包含 summary 和 records。
    内存缓存按 pipeline_run_id 分组的嵌套结构。

    Attributes:
        _records: 内存中的记录缓存（record_id -> ExecutionRecordData）
        _summaries: 内存中的摘要缓存（run_id -> PipelineRunSummary）
        _pipelines: 按 pipeline_run_id 分组的管道数据（run_id -> {summary, records}）
        _data_dir: YAML 文件目录路径
    """

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._records: dict[str, ExecutionRecordData] = {}
        self._summaries: dict[str, PipelineRunSummary] = {}
        self._data_dir = Path(data_dir) if data_dir else None
        if self._data_dir:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._load_all()

    def _load_all(self) -> None:
        if not self._data_dir:
            return
        for yaml_file in self._data_dir.glob("*.yaml"):
            self._load_pipeline_file(yaml_file)

    def _load_pipeline_file(self, yaml_file: Path) -> None:
        try:
            text = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                return
            summary_dict = data.get("summary")
            if summary_dict and isinstance(summary_dict, dict):
                summary = self._dict_to_summary(summary_dict)
                self._summaries[summary.run_id] = summary
            records_list = data.get("records")
            if records_list and isinstance(records_list, list):
                for record_dict in records_list:
                    if isinstance(record_dict, dict):
                        record = self._dict_to_record(record_dict)
                        self._records[record.record_id] = record
        except Exception:
            logger.warning("管道文件损坏，跳过: %s", yaml_file.name)

    def _get_pipeline_file(self, pipeline_run_id: str) -> Path | None:
        if not self._data_dir:
            return None
        return self._data_dir / f"{pipeline_run_id}.yaml"

    def _persist_pipeline(self, pipeline_run_id: str) -> None:
        if not self._data_dir:
            return
        file_path = self._get_pipeline_file(pipeline_run_id)
        if file_path is None:
            return
        self._data_dir.mkdir(parents=True, exist_ok=True)
        summary = self._summaries.get(pipeline_run_id)
        records = [
            r for r in self._records.values()
            if r.pipeline_run_id == pipeline_run_id
        ]
        records.sort(key=lambda r: r.sequence)
        data: dict[str, Any] = {}
        if summary:
            data["summary"] = self._summary_to_dict(summary)
        else:
            data["summary"] = None
        data["records"] = [self._record_to_dict(r) for r in records]
        file_path.write_text(
            yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _record_to_dict(record: ExecutionRecordData) -> dict[str, Any]:
        return asdict(record)

    @staticmethod
    def _dict_to_record(data: dict[str, Any]) -> ExecutionRecordData:
        return ExecutionRecordData(**data)

    @staticmethod
    def _summary_to_dict(summary: PipelineRunSummary) -> dict[str, Any]:
        return asdict(summary)

    @staticmethod
    def _dict_to_summary(data: dict[str, Any]) -> PipelineRunSummary:
        return PipelineRunSummary(**data)

    def save(self, record: ExecutionRecordData) -> str:
        if not record.record_id:
            record.record_id = uuid.uuid4().hex[:12]
        if not record.created_at:
            record.created_at = datetime.now().isoformat()
        self._records[record.record_id] = record
        if record.pipeline_run_id:
            self._persist_pipeline(record.pipeline_run_id)
        logger.debug("保存执行记录: %s (pipeline=%s, iteration=%d)",
                      record.record_id, record.pipeline_run_id, record.iteration)
        return record.record_id

    def get(self, record_id: str) -> ExecutionRecordData | None:
        return self._records.get(record_id)

    def list_by_session(
        self, session_id: str, limit: int = 50
    ) -> list[ExecutionRecordData]:
        records = [
            r for r in self._records.values() if r.pipeline_run_id == session_id
        ]
        records.sort(key=lambda r: r.iteration)
        return records[:limit]

    def count_by_session(self, session_id: str) -> int:
        return sum(
            1 for r in self._records.values() if r.pipeline_run_id == session_id
        )

    def delete_by_session(self, session_id: str) -> int:
        to_delete = [
            rid for rid, r in self._records.items()
            if r.pipeline_run_id == session_id
        ]
        for rid in to_delete:
            del self._records[rid]
        if session_id in self._summaries:
            del self._summaries[session_id]
        if to_delete and self._data_dir:
            file_path = self._get_pipeline_file(session_id)
            if file_path and file_path.exists():
                file_path.unlink()
        logger.debug("删除会话 %s 的执行记录: %d 条", session_id, len(to_delete))
        return len(to_delete)

    def list_by_pipeline(
        self, pipeline_run_id: str
    ) -> list[ExecutionRecordData]:
        records = [
            r for r in self._records.values()
            if r.pipeline_run_id == pipeline_run_id
        ]
        records.sort(key=lambda r: r.sequence)
        return records

    def save_summary(self, summary: PipelineRunSummary) -> str:
        if not summary.run_id:
            summary.run_id = uuid.uuid4().hex[:12]
        if not summary.created_at:
            summary.created_at = datetime.now().isoformat()
        self._summaries[summary.run_id] = summary
        self._persist_pipeline(summary.run_id)
        logger.debug("保存管道摘要: %s (iterations=%d, status=%s)",
                      summary.run_id, summary.total_iterations, summary.status)
        return summary.run_id

    def get_summary(self, run_id: str) -> PipelineRunSummary | None:
        return self._summaries.get(run_id)

    def list_summaries(
        self, limit: int = 50
    ) -> list[PipelineRunSummary]:
        summaries = sorted(
            self._summaries.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )
        return summaries[:limit]

    def get_total_tokens(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for summary in self._summaries.values():
            for key, value in summary.total_tokens.items():
                total[key] = total.get(key, 0) + value
        return total


def summarize_text(text: Any, max_len: int = _DEFAULT_SUMMARY_MAX_LEN) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= max_len:
        return s
    return s[:max_len] + "...(truncated)"
